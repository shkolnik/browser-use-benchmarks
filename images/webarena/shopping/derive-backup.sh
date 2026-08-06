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
set -euo pipefail

UPSTREAM_TAR="$DATASETS_DIR/shopping_final_0712.tar"
UPSTREAM_TAG=shopping_final_0712:latest
UPSTREAM_SHA=2052430ee930d18a0c362997f1ccd1c500041f720ece8794ed77a77ee99139b5
CACHE="$REGISTRY/webarena-shopping-derived:${UPSTREAM_SHA:0:12}"

DB_NAME=magentodb
DB_USER=magentouser
DB_PASS=MyPassword

extract_outputs_from_cache() {
  local cid
  cid=$(docker create "$CACHE" true)
  docker export "$cid" | tar -x -C "$DATASETS_DIR"
  docker rm "$cid" >/dev/null
  cat "$DATASETS_DIR"/shopping_media.tar.part-* > "$DATASETS_DIR/shopping_media.tar"
  rm -f "$DATASETS_DIR"/shopping_media.tar.part-*
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
docker exec shopping-derive sh -c \
  "mysqldump -u$DB_USER -p$DB_PASS --single-transaction --routines --triggers $DB_NAME | gzip > /tmp/shopping_db.sql.gz"
docker cp shopping-derive:/tmp/shopping_db.sql.gz "$DATASETS_DIR/"
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
{
  echo "FROM scratch"
  for f in "$work"/shopping_media.tar.part-*; do
    echo "COPY $(basename "$f") /"
  done
  echo "COPY shopping_db.sql.gz /"
  echo "COPY shopping_env.php /"
} > "$work/Dockerfile"
docker build -t "$CACHE" "$work"
rm -rf "$work"
docker push "$CACHE" || echo "warning: cache push failed (continuing; derivation succeeded)"
echo "derive complete"
