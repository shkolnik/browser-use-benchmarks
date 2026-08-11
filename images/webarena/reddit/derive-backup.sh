#!/bin/bash
# Prepare step for the reddit (Postmill) image: derive the populated DATA —
# and only the data — from the upstream docker-save tar's own running
# container. Every executable byte in the final image comes from Postmill's
# pinned source archive instead (see image.toml), which is the whole point:
# the upstream tar's top four layers are hand `docker commit`s whose history
# entries read literally `supervisord -n -j /supervisord.pid`, so nothing in
# them records how the code got there.
#
# Same mechanism as ../gitlab and ../shopping/derive-backup.sh, and the same
# ≤8G splitting: measured, the artifacts are a 477 MiB gzipped dump and a ~41G
# media tar. An earlier revision of this comment said "~2.4 MB of media" — that
# was `public/media` alone (2.4 MB, 277 files) mistaken for the whole payload;
# `public/submission_images` next to it is 38.5G across 31,467 files, and it is
# the reason the upstream tar is called "withimg".
#
# Inputs (env, set by builder/docker.py run_prepare):
#   DATASETS_DIR  where the verified upstream tar lives and outputs must land
#   REGISTRY      e.g. ghcr.io/shkolnik
#   REPO_ROOT     to source the shared derive-cache library
set -euo pipefail

# builder/stage-lib/derive-cache.sh: reads and writes this fleet's derived-inputs
# cache entries as oras artifacts.
. "$REPO_ROOT/builder/stage-lib/derive-cache.sh"

UPSTREAM_TAR="$DATASETS_DIR/postmill-populated-exposed-withimg.tar"
UPSTREAM_TAG=postmill-populated-exposed-withimg:latest
# The pin comes from image.toml via run_prepare — never a second copy here.
# It is the cache tag AND part of the on-disk provenance stamp, so updating the
# manifest alone is enough to strand every artifact derived from the old tar.
: "${PREPARE_INPUT_SHA256:?run_prepare must export the pinned upstream sha256}"
UPSTREAM_SHA="$PREPARE_INPUT_SHA256"
# The cache key covers the RECIPE as well as the input. Keying on the upstream
# tar's sha alone is wrong twice over: this script decides which database it
# dumps (it dumped the wrong one at r1, and cached the empty result), so two
# different recipes over one identical input are two different artifacts. Bump
# RECIPE whenever what this script emits changes; that also strands any bad
# entry from the previous revision instead of silently re-serving it.
RECIPE=r2
CACHE="$REGISTRY/webarena-reddit-derived:${UPSTREAM_SHA:0:12}-$RECIPE"

# The role and database are `postmill`, established by sampling
# pg_stat_activity while the upstream container served real /forums requests —
# NOT by reading its .env, which is what went wrong the first time. That .env
# carries `pgsql://db_user:db_password@localhost/db_name`, but no such role
# exists in the cluster (`\du` lists only postgres and postmill); upstream's own
# docker-entrypoint.sh writes the real DSN, `pgsql://postmill:secret@db/postmill`.
# Its `?serverVersion=9.6` is a stale Doctrine hint and NOT the server version —
# `postgres --version` reports 14.7.
DB_NAME=postmill

# A derived artifact far below what was measured means the derivation
# half-worked. These are FLOORS chosen well under the measured sizes (499.7 MB
# gzipped dump, 41.3G media tar), not equality checks — the data may
# legitimately grow, it may not collapse.
assert_min() {
  actual=$(stat -c %s "$DATASETS_DIR/$1")
  if [ "$actual" -lt "$2" ]; then
    echo "derive: $1 is $actual bytes, below the $2-byte floor — truncated or empty artifact; refusing to use it" >&2
    exit 1
  fi
  echo "derive: $1 = $actual bytes (floor $2)"
}

check_floors() {
  assert_min reddit_db.sql.gz 300000000
  assert_min reddit_media.tar 30000000000
}

reassemble_outputs() {
  cat "$DATASETS_DIR"/reddit_media.tar.part-* > "$DATASETS_DIR/reddit_media.tar"
  rm -f "$DATASETS_DIR"/reddit_media.tar.part-*
}

echo "=== checking derived-inputs cache: $CACHE ==="
if dcache_pull "$CACHE" "$DATASETS_DIR"; then
  reassemble_outputs
  # The floors apply to the CACHE path too. They used to guard only fresh
  # derivation, so this branch returned unchecked artifacts — which is the one
  # path that can serve a bad blob pushed by an older, buggier revision of this
  # script. A cache is an input like any other; nothing arrives trusted.
  check_floors
  echo "derive: cache hit ($DCACHE_HIT_FORMAT), outputs extracted"
  exit 0
fi
# #42: distinguish "never cached" (fine, derive) from "was cached,
# now missing" (fatal unless explicitly waived) using the checked-in
# digest lock.
dcache_require "$CACHE"
echo "cache miss — deriving from upstream tar"
# Fetch the upstream tar HERE, not in the download step. It is declared
# prepare_input, so `bin/build download` skipped it: on a cold runner whose
# derived cache is valid we exit above having pulled only the derived
# artifacts from GHCR, instead of pulling 53.4G from a mirror and never
# opening it. Postmill's SOURCE tarball stays eager — the Dockerfile COPYs it.
"$REPO_ROOT/bin/build" download --prepare-inputs "$IMAGE" --datasets-dir "$DATASETS_DIR"

echo "=== load + boot upstream image ==="
docker load -i "$UPSTREAM_TAR"
# Cmd is ["supervisord","-n","-j","/supervisord.pid"], the real service
# supervisor, so no override is needed. Only nginx/php-fpm/postgres actually
# start (`supervisorctl status`); the base image's elasticsearch, redis,
# memcached and friends are never launched — which is why the rebuilt image
# does not ship them.
docker run -d --name reddit-derive --hostname localhost "$UPSTREAM_TAG"
trap 'docker rm -f reddit-derive >/dev/null 2>&1 || true' EXIT
for i in $(seq 1 60); do
  if [ "$(docker inspect -f '{{.State.Running}}' reddit-derive)" != true ]; then
    echo "reddit-derive exited (status: $(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}' reddit-derive)); last logs:" >&2
    docker logs --tail 60 reddit-derive >&2 || true
    exit 1
  fi
  code=$(docker exec reddit-derive curl -s -o /dev/null -w '%{http_code}' http://localhost:80/forums || true)
  if [ "$code" = 200 ] || [ "$code" = 302 ]; then
    echo "up after ~$((i * 5))s"
    break
  fi
  [ "$i" = 60 ] && { echo "upstream reddit never came up" >&2; exit 1; }
  sleep 5
done

echo "=== pg_dump + uploaded media ==="
# Dump and compress as two steps, NOT `pg_dump | gzip > f`. In a pipeline the
# shell reports gzip's status, so a pg_dump that dies on the very first line
# still "succeeds" — which is exactly how the first attempt at this image
# produced a valid-looking empty .gz, pushed it to the derived-inputs cache,
# and only failed 35 minutes later. The pipeline hid a real error
# (`role "db_user" does not exist`) behind a zero exit code.
docker exec reddit-derive sh -c "pg_dump -U postgres -d $DB_NAME > /tmp/reddit_db.sql"
docker exec reddit-derive gzip -f /tmp/reddit_db.sql
docker cp reddit-derive:/tmp/reddit_db.sql.gz "$DATASETS_DIR/"
# public/media holds the uploaded images ("withimg"); submission_images is in
# Postmill's write-dirs list and may or may not exist in this deployment, so
# tar whichever are actually present rather than failing on the absent one.
docker exec reddit-derive sh -c \
  'cd /var/www/html/public && tar cf /tmp/reddit_media.tar $(ls -d media submission_images 2>/dev/null)'
docker cp reddit-derive:/tmp/reddit_media.tar "$DATASETS_DIR/"

# Second net, behind the exit codes above (same floors the cache path checks).
check_floors

docker rm -f reddit-derive
trap - EXIT
# Free the ~53G loaded upstream image before the build stage needs disk.
docker image rm -f "$UPSTREAM_TAG"

echo "=== push derived-inputs cache ==="
# The scratch dir sits NEXT TO the datasets, never in /tmp: a runner's /tmp can
# be far smaller than the volume already holding the datasets, which is exactly
# how ../shopping died as `split: No space left on device` mid-derivation (run
# 31080808213) with its artifacts already verified on disk. Check the room
# first so a failure names the resource instead of surfacing after the copy.
need=$(( $(stat -c %s "$DATASETS_DIR/reddit_db.sql.gz") \
       + $(stat -c %s "$DATASETS_DIR/reddit_media.tar") ))
avail=$(df -B1 --output=avail "$DATASETS_DIR" | tail -1)
if [ "$avail" -lt "$((need + need / 20))" ]; then
  echo "derive: staging the derived-inputs cache needs ~$((need >> 20))M beside the datasets in $DATASETS_DIR; only $((avail >> 20))M available" >&2
  exit 1
fi
work=$(mktemp -d "$DATASETS_DIR/.derive-work.XXXXXX")
# A leftover scratch dir would persist in the runner's datasets cache.
trap 'rm -rf "$work"' EXIT
cp "$DATASETS_DIR/reddit_db.sql.gz" "$work/"
# The media tar is ~41G (submission_images alone is 38.5G), so it ships split
# for the same reason ../shopping's does: one blob that size is a bad layer even
# where a registry tolerates it, and the reassembly is a `cat`.
split -b 8G -d "$DATASETS_DIR/reddit_media.tar" "$work/reddit_media.tar.part-"
files=(reddit_db.sql.gz)
for f in "$work"/reddit_media.tar.part-*; do
  files+=("$(basename "$f")")
done
# dcache_push retries 3x then fails the build (#80) — see the library.
dcache_push "$CACHE" "$work" "${files[@]}"
echo "derive complete"
