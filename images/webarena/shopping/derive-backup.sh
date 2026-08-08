#!/bin/bash
# Prepare step for the shopping (Magento 2 "OneStopShop") image: derive the
# pinned build inputs (mysqldump + pub/media + app/etc/env.php) from the
# upstream docker-save tar's own running container.
#
# Magento's setup:backup is deprecated upstream; the vendor-endorsed manual
# backup path is exactly these three artifacts (DB dump, media directory,
# env.php) — see Adobe's "Manual backup" docs. Deriving them from the
# upstream container (rather than shipping the 67G upstream tar verbatim)
# sheds the upstream image's own OS/package layers, which the final image
# does not reuse verbatim (see image.toml's base-image adjudication note).
#
# The derivation is cached in GHCR keyed by the upstream tar's pinned sha256,
# so a failed workflow never pays the (~20-30 min, mostly the 50G media tar)
# derivation twice — and a cache entry is only ever created by this script,
# so its own CI run documents where the bytes came from.
#
# Inputs (env, set by builder/manifest.py's run_prepare):
#   DATASETS_DIR  where the verified upstream tar lives and outputs must land
#   REGISTRY      e.g. ghcr.io/shkolnik
#   REPO_ROOT     to source the shared derive-cache library
set -euo pipefail

# builder/stage-lib/derive-cache.sh: read prefers an oras artifact, falls back
# to the legacy `FROM scratch` image, and pushes new entries as oras.
. "$REPO_ROOT/builder/stage-lib/derive-cache.sh"

UPSTREAM_TAR="$DATASETS_DIR/shopping_final_0712.tar"
UPSTREAM_TAG=shopping_final_0712:latest
# The pin comes from image.toml via run_prepare — never a second copy here.
# It is the cache tag AND part of the on-disk provenance stamp, so updating the
# manifest alone is enough to strand every artifact derived from the old tar.
: "${PREPARE_INPUT_SHA256:?run_prepare must export the pinned upstream sha256}"
UPSTREAM_SHA="$PREPARE_INPUT_SHA256"
# The cache key covers the RECIPE as well as the input. Keying on the upstream
# tar's sha alone is wrong twice over: this script decides HOW the artifacts are
# produced, so two different recipes over one identical input are two different
# artifacts. Without this component a recipe fix is inert — the builder's
# provenance stamp notices the script changed and re-runs it, the script then
# cache-hits on the unchanged key, extracts the artifacts the OLD recipe made,
# and exits 0 before deriving anything. run_prepare then stamps those stale
# bytes as freshly derived. Bump RECIPE whenever what this script emits
# changes; that also strands any bad entry from the previous revision.
RECIPE=r2  # r2: mysqldump/gzip split into two steps (the pipeline hid failures)
CACHE="$REGISTRY/webarena-shopping-derived:${UPSTREAM_SHA:0:12}-$RECIPE"

DB_NAME=magentodb
DB_USER=magentouser
DB_PASS=MyPassword

# Defined ABOVE the cache branch on purpose: the checks apply to cached
# artifacts too. A cache is an input like any other, and it is the one path
# that can serve a bad blob pushed by an older, buggier revision of this
# script — nothing arrives trusted.
assert_dump_complete() {
  if ! gzip -dc "$DATASETS_DIR/$1" | tail -c 200 | grep -q '^-- Dump completed'; then
    echo "derive: $1 carries no mysqldump completion trailer — the dump is truncated; refusing to use it" >&2
    exit 1
  fi
  echo "derive: $1 completion trailer present"
}

reassemble_outputs() {
  cat "$DATASETS_DIR"/shopping_media.tar.part-* > "$DATASETS_DIR/shopping_media.tar"
  rm -f "$DATASETS_DIR"/shopping_media.tar.part-*
}

echo "=== checking derived-inputs cache: $CACHE ==="
if dcache_pull "$CACHE" "$DATASETS_DIR" 'shopping_*'; then
  reassemble_outputs
  assert_dump_complete shopping_db.sql.gz
  echo "derive: cache hit ($DCACHE_HIT_FORMAT), outputs extracted"
  exit 0
fi
echo "cache miss — deriving from upstream tar"
# Fetch the upstream tar HERE, not in the download step. It is declared
# prepare_input, so `bin/build download` skipped it: on a cold runner whose
# derived cache is valid we exit above having pulled only the derived
# artifacts from GHCR, instead of pulling shopping_final_0712.tar from a ~3.6MB/s
# university mirror and never opening it.
"$REPO_ROOT/bin/build" download --prepare-inputs "$IMAGE" --datasets-dir "$DATASETS_DIR"

echo "=== load + boot upstream image ==="
docker load -i "$UPSTREAM_TAR"
# The upstream image is a docker-commit whose Cmd is the real service
# supervisor (verified via `docker inspect -f '{{.Config.Cmd}}'`), unlike
# gitlab's upstream tar (Cmd literally ["bash"]) — so the entrypoint's own
# Cmd is correct here and needs no override.
docker run -d --name shopping-derive --hostname localhost \
  "$UPSTREAM_TAG"
trap 'docker rm -f shopping-derive >/dev/null 2>&1 || true' EXIT
for i in $(seq 1 60); do
  if [ "$(docker inspect -f '{{.State.Running}}' shopping-derive)" != true ]; then
    echo "shopping-derive exited (status: $(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}' shopping-derive)); last logs:" >&2
    docker logs --tail 60 shopping-derive >&2 || true
    exit 1
  fi
  code=$(docker exec shopping-derive curl -s -o /dev/null -w '%{http_code}' http://localhost:80/ || true)
  if [ "$code" = 200 ] || [ "$code" = 302 ]; then
    echo "up after ~$((i * 5))s"
    break
  fi
  [ "$i" = 60 ] && { echo "upstream shopping never came up" >&2; exit 1; }
  sleep 5
done

echo "=== mysqldump + pub/media + env.php ==="
# Dump and compress as TWO steps, not `mysqldump | gzip > f`. This script's
# own `set -o pipefail` does not reach into the pipeline: it runs inside the
# container's `sh -c`, whose shell has no pipefail, so `docker exec` reports
# only gzip's status. A mysqldump that dies mid-stream then yields a
# valid-looking short .gz and a zero exit. That is exactly how ../reddit's
# first derivation cached an empty dump and only failed 35 minutes later.
docker exec shopping-derive sh -c \
  "mysqldump -u$DB_USER -p$DB_PASS --single-transaction --routines --triggers $DB_NAME > /tmp/shopping_db.sql"
# mysqldump writes this trailer only after every table is dumped, so it
# asserts completeness at the source rather than inferring it from a size.
# A byte floor would be the wrong instrument here: two legitimate Magento
# dumps from this very pair of images measure 367,179,711 bytes (shopping)
# and 900,148 (shopping-admin) gzipped — both complete, both 369 tables —
# so no single threshold separates "sparse" from "truncated".
docker exec shopping-derive sh -c \
  "tail -c 200 /tmp/shopping_db.sql | grep -q '^-- Dump completed'" \
  || { echo "derive: /tmp/shopping_db.sql has no mysqldump completion trailer — the dump was truncated; refusing to cache it" >&2; exit 1; }
docker exec shopping-derive gzip -f /tmp/shopping_db.sql
docker cp shopping-derive:/tmp/shopping_db.sql.gz "$DATASETS_DIR/"
# Re-asserted on the host copy so both paths out of this script — fresh derive
# and cache hit — are gated by the identical predicate. The in-container check
# above fails faster (before gzipping ~2G); this one additionally covers the
# gzip and the docker cp.
assert_dump_complete shopping_db.sql.gz
docker exec shopping-derive tar cf /tmp/shopping_media.tar -C /var/www/magento2/pub media
docker cp shopping-derive:/tmp/shopping_media.tar "$DATASETS_DIR/"
docker cp shopping-derive:/var/www/magento2/app/etc/env.php "$DATASETS_DIR/shopping_env.php"
docker rm -f shopping-derive
trap - EXIT
# Free the loaded upstream image (67.6G) before the build stage needs disk.
docker image rm -f "$UPSTREAM_TAG"

echo "=== push derived-inputs cache ==="
# Registry layers over ~10G are refused, so the media tar ships split. The
# scratch dir sits NEXT TO the datasets rather than in /tmp: the split needs a
# second full copy of the 45G media tar, which /tmp on the CI runner cannot
# hold (`split: No space left on device`, run 31080808213). Check the room
# first so the failure names the resource instead of surfacing 9 minutes of
# download later as a cryptic split error.
need=$(stat -c %s "$DATASETS_DIR/shopping_media.tar")
avail=$(df -B1 --output=avail "$DATASETS_DIR" | tail -1)
if [ "$avail" -lt "$((need + need / 20))" ]; then
  echo "derive: splitting shopping_media.tar needs ~$((need >> 30))G beside the datasets in $DATASETS_DIR; only $((avail >> 30))G available" >&2
  exit 1
fi
work=$(mktemp -d "$DATASETS_DIR/.derive-work.XXXXXX")
# A leftover scratch dir would persist in the runner's datasets cache.
trap 'rm -rf "$work"' EXIT
split -b 8G -d "$DATASETS_DIR/shopping_media.tar" "$work/shopping_media.tar.part-"
cp "$DATASETS_DIR/shopping_db.sql.gz" "$work/"
cp "$DATASETS_DIR/shopping_env.php" "$work/"
files=(shopping_db.sql.gz shopping_env.php)
for f in "$work"/shopping_media.tar.part-*; do
  files+=("$(basename "$f")")
done
# dcache_push retries 3x then fails the build (#80) — see the library.
dcache_push "$CACHE" "$work" "${files[@]}"
echo "derive complete"
