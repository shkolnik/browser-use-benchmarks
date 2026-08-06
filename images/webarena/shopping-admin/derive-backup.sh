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
UPSTREAM_SHA=ad607557a79f1bacf83c4661730802bbedc6bcbbf15078352ae59bcec74182b8
CACHE="$REGISTRY/webarena-shopping-admin-derived:${UPSTREAM_SHA:0:12}"

DB_NAME=magentodb
DB_USER=magentouser
DB_PASS=MyPassword

extract_outputs_from_cache() {
  local cid
  cid=$(docker create "$CACHE" true)
  docker export "$cid" | tar -x -C "$DATASETS_DIR"
  docker rm "$cid" >/dev/null
}

echo "=== checking derived-inputs cache: $CACHE ==="
if docker pull "$CACHE" 2>/dev/null; then
  extract_outputs_from_cache
  echo "derive: cache hit, outputs extracted"
  exit 0
fi
echo "cache miss — deriving from upstream tar"

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
docker exec shopping-admin-derive sh -c \
  "mysqldump -u$DB_USER -p$DB_PASS --single-transaction --routines --triggers $DB_NAME | gzip > /tmp/shopping_admin_db.sql.gz"
docker cp shopping-admin-derive:/tmp/shopping_admin_db.sql.gz "$DATASETS_DIR/"
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
docker push "$CACHE" || echo "warning: cache push failed (continuing; derivation succeeded)"
echo "derive complete"
