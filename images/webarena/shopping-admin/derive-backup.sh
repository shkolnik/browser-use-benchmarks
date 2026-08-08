#!/bin/bash
# Prepare step for the shopping-admin (Magento 2 admin panel) image: derive
# the pinned build inputs (mysqldump + pub/media + app/etc/env.php) from the
# upstream docker-save tar's own running container.
#
# Same mechanism as images/webarena/shopping/derive-backup.sh (see that
# file's header for the "why manual backup, why derive not ship-verbatim"
# rationale) — shopping-admin is the same Magento 2 stack pointed at a much
# smaller populated dataset (85M pub/media vs. shopping's 49.7G).
#
# Inputs (env, set by builder/manifest.py's run_prepare):
#   DATASETS_DIR  where the verified upstream tar lives and outputs must land
#   REGISTRY      e.g. ghcr.io/shkolnik
set -euo pipefail

UPSTREAM_TAR="$DATASETS_DIR/shopping_admin_final_0719.tar"
UPSTREAM_TAG=shopping_admin_final_0719:latest
# The pin comes from image.toml via run_prepare — never a second copy here.
# It is the cache tag AND part of the on-disk provenance stamp, so updating the
# manifest alone is enough to strand every artifact derived from the old tar.
: "${PREPARE_INPUT_SHA256:?run_prepare must export the pinned upstream sha256}"
UPSTREAM_SHA="$PREPARE_INPUT_SHA256"
# See ../shopping/derive-backup.sh for why the key carries the recipe: without
# it a recipe fix is inert, because the builder's stamp re-runs this script and
# this script then cache-hits on an unchanged key and returns the OLD recipe's
# artifacts before deriving anything.
RECIPE=r2  # r2: mysqldump/gzip split into two steps (the pipeline hid failures)
CACHE="$REGISTRY/webarena-shopping-admin-derived:${UPSTREAM_SHA:0:12}-$RECIPE"

DB_NAME=magentodb
DB_USER=magentouser
DB_PASS=MyPassword

# Above the cache branch on purpose — cached artifacts are checked too.
assert_dump_complete() {
  if ! gzip -dc "$DATASETS_DIR/$1" | tail -c 200 | grep -q '^-- Dump completed'; then
    echo "derive: $1 carries no mysqldump completion trailer — the dump is truncated; refusing to use it" >&2
    exit 1
  fi
  echo "derive: $1 completion trailer present"
}

extract_outputs_from_cache() {
  local cid
  cid=$(docker create "$CACHE" true)
  # Filtered: `docker export` also carries the /dev, /etc, /proc, /sys and
  # /.dockerenv Docker injects into every container, which unfiltered land
  # in the shared datasets dir. ONE pattern — tar exits 2 on any pattern
  # that matches nothing, so a second shape would break every extract that
  # legitimately lacks it.
  docker export "$cid" | tar -x -C "$DATASETS_DIR" --wildcards 'shopping_admin_*'
  docker rm "$cid" >/dev/null
}

echo "=== checking derived-inputs cache: $CACHE ==="
if docker pull "$CACHE" 2>/dev/null; then
  extract_outputs_from_cache
  assert_dump_complete shopping_admin_db.sql.gz
  echo "derive: cache hit, outputs extracted"
  exit 0
fi
echo "cache miss — deriving from upstream tar"
# Fetch the upstream tar HERE, not in the download step. It is declared
# prepare_input, so `bin/build download` skipped it: on a cold runner whose
# derived cache is valid we exit above having pulled only the derived
# artifacts from GHCR, instead of pulling shopping_admin_final_0719.tar from a ~3.6MB/s
# university mirror and never opening it.
"$REPO_ROOT/bin/build" download --prepare-inputs "$IMAGE" --datasets-dir "$DATASETS_DIR"

echo "=== load + boot upstream image ==="
docker load -i "$UPSTREAM_TAR"
# Cmd verified via `docker inspect -f '{{.Config.Cmd}}'` to be the real
# service supervisor (same as shopping's upstream tar) — no override needed.
docker run -d --name shopping-admin-derive --hostname localhost \
  "$UPSTREAM_TAG"
trap 'docker rm -f shopping-admin-derive >/dev/null 2>&1 || true' EXIT
for i in $(seq 1 60); do
  if [ "$(docker inspect -f '{{.State.Running}}' shopping-admin-derive)" != true ]; then
    echo "shopping-admin-derive exited (status: $(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}' shopping-admin-derive)); last logs:" >&2
    docker logs --tail 60 shopping-admin-derive >&2 || true
    exit 1
  fi
  code=$(docker exec shopping-admin-derive curl -s -o /dev/null -w '%{http_code}' http://localhost:80/admin || true)
  if [ "$code" = 200 ] || [ "$code" = 302 ]; then
    echo "up after ~$((i * 5))s"
    break
  fi
  [ "$i" = 60 ] && { echo "upstream shopping-admin never came up" >&2; exit 1; }
  sleep 5
done

echo "=== mysqldump + pub/media + env.php ==="
# Two steps, not `mysqldump | gzip > f` — see the same block in
# ../shopping/derive-backup.sh for why the pipeline's exit code lies. This
# image is the reason a byte floor cannot replace the trailer check: its
# complete dump is 900,148 gzipped bytes, ~400x smaller than ../shopping's
# from the same 369-table schema, because the admin benchmark seeds far less
# row data. Small here is normal; incomplete is what must be caught.
docker exec shopping-admin-derive sh -c \
  "mysqldump -u$DB_USER -p$DB_PASS --single-transaction --routines --triggers $DB_NAME > /tmp/shopping_admin_db.sql"
docker exec shopping-admin-derive sh -c \
  "tail -c 200 /tmp/shopping_admin_db.sql | grep -q '^-- Dump completed'" \
  || { echo "derive: /tmp/shopping_admin_db.sql has no mysqldump completion trailer — the dump was truncated; refusing to cache it" >&2; exit 1; }
docker exec shopping-admin-derive gzip -f /tmp/shopping_admin_db.sql
docker cp shopping-admin-derive:/tmp/shopping_admin_db.sql.gz "$DATASETS_DIR/"
# Same predicate as the cache-hit path, so both exits from this script are
# gated identically.
assert_dump_complete shopping_admin_db.sql.gz
docker exec shopping-admin-derive tar cf /tmp/shopping_admin_media.tar -C /var/www/magento2/pub media
docker cp shopping-admin-derive:/tmp/shopping_admin_media.tar "$DATASETS_DIR/"
docker cp shopping-admin-derive:/var/www/magento2/app/etc/env.php "$DATASETS_DIR/shopping_admin_env.php"
docker rm -f shopping-admin-derive
trap - EXIT
docker image rm -f "$UPSTREAM_TAG"

echo "=== push derived-inputs cache ==="
# shopping-admin's media tar is well under 8G, so no split needed here — but
# the scratch dir still needs to sit NEXT TO the datasets, not in /tmp: a
# runner's /tmp can be far smaller than the volume already holding the
# datasets (../shopping hit exactly this as `split: No space left on device`
# mid-derivation, run 31080808213, even though its media tar was already
# verified on disk — same class, adapted here since shopping-admin copies
# instead of splitting). Check the room first so a failure names the resource
# instead of surfacing after the (much shorter, ~85M) copy.
need=$(( $(stat -c %s "$DATASETS_DIR/shopping_admin_media.tar") \
       + $(stat -c %s "$DATASETS_DIR/shopping_admin_db.sql.gz") \
       + $(stat -c %s "$DATASETS_DIR/shopping_admin_env.php") ))
avail=$(df -B1 --output=avail "$DATASETS_DIR" | tail -1)
if [ "$avail" -lt "$((need + need / 20))" ]; then
  echo "derive: staging the derived-inputs cache needs ~$((need >> 20))M beside the datasets in $DATASETS_DIR; only $((avail >> 20))M available" >&2
  exit 1
fi
work=$(mktemp -d "$DATASETS_DIR/.derive-work.XXXXXX")
# A leftover scratch dir would persist in the runner's datasets cache.
trap 'rm -rf "$work"' EXIT
cp "$DATASETS_DIR/shopping_admin_media.tar" "$work/"
cp "$DATASETS_DIR/shopping_admin_db.sql.gz" "$work/"
cp "$DATASETS_DIR/shopping_admin_env.php" "$work/"
{
  echo "FROM scratch"
  echo "COPY shopping_admin_media.tar /"
  echo "COPY shopping_admin_db.sql.gz /"
  echo "COPY shopping_admin_env.php /"
} > "$work/Dockerfile"
docker build -t "$CACHE" "$work"
# Retry, then FAIL. builder/docker.py stamps a successful prepare and skips
# this script on every later run with matching inputs, so this is the only run
# that will ever push: a warning here leaves the cache empty permanently while
# later builds depend on it. Retries because a transient GHCR error must not
# throw away a finished derivation.
pushed=
for attempt in 1 2 3; do
  if docker push "$CACHE"; then
    pushed=yes
    break
  fi
  echo "cache push attempt $attempt/3 failed" >&2
  [ "$attempt" = 3 ] || sleep 30
done
if [ -z "$pushed" ]; then
  echo "derive: could not publish $CACHE after 3 attempts. Failing rather than" \
       "stamping: prepare_reuse_check would skip this script on the next run," \
       "so nothing would ever retry the push and the cache would stay empty" \
       "for good. The artifacts in $DATASETS_DIR are intact and correct." >&2
  exit 1
fi
echo "derive complete"
