#!/bin/bash
# Prepare step for the reddit (Postmill) image: derive the populated DATA —
# and only the data — from the upstream docker-save tar's own running
# container. Every executable byte in the final image comes from Postmill's
# pinned source archive instead (see image.toml), which is the whole point:
# the upstream tar's top four layers are hand `docker commit`s whose history
# entries read literally `supervisord -n -j /supervisord.pid`, so nothing in
# them records how the code got there.
#
# Same mechanism as ../gitlab and ../shopping/derive-backup.sh; the artifacts
# here are far smaller (a ~477 MiB dump and ~2.4 MB of media, measured), so
# there is no ≤8G layer splitting and no staging-bucket partitioning.
#
# Inputs (env, set by builder/docker.py run_prepare):
#   DATASETS_DIR  where the verified upstream tar lives and outputs must land
#   REGISTRY      e.g. ghcr.io/shkolnik
set -euo pipefail

UPSTREAM_TAR="$DATASETS_DIR/postmill-populated-exposed-withimg.tar"
UPSTREAM_TAG=postmill-populated-exposed-withimg:latest
UPSTREAM_SHA=6ff70f73bc808b4cd9faf4e925dab2a4ae3cc5f9d0e9755360500607973a0dc5
CACHE="$REGISTRY/webarena-reddit-derived:${UPSTREAM_SHA:0:12}"

# Measured from the upstream container, not guessed: the app's .env carries the
# literal placeholder credentials. Its `?serverVersion=9.6` is a stale Doctrine
# hint and NOT the server version — `postgres --version` reports 14.7.
DB_NAME=db_name
DB_USER=db_user

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
docker exec reddit-derive sh -c \
  "pg_dump -U $DB_USER $DB_NAME | gzip > /tmp/reddit_db.sql.gz"
docker cp reddit-derive:/tmp/reddit_db.sql.gz "$DATASETS_DIR/"
# public/media holds the uploaded images ("withimg"); submission_images is in
# Postmill's write-dirs list and may or may not exist in this deployment, so
# tar whichever are actually present rather than failing on the absent one.
docker exec reddit-derive sh -c \
  'cd /var/www/html/public && tar cf /tmp/reddit_media.tar $(ls -d media submission_images 2>/dev/null)'
docker cp reddit-derive:/tmp/reddit_media.tar "$DATASETS_DIR/"
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
cp "$DATASETS_DIR/reddit_media.tar" "$work/"
{
  echo "FROM scratch"
  echo "COPY reddit_db.sql.gz /"
  echo "COPY reddit_media.tar /"
} > "$work/Dockerfile"
docker build -t "$CACHE" "$work"
docker push "$CACHE" || echo "warning: cache push failed (continuing; derivation succeeded)"
echo "derive complete"
