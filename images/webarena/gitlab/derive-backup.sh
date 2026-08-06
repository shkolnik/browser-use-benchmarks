#!/bin/bash
# Prepare step for the gitlab image: derive the pinned build inputs
# (gitlab-backup tar + /etc/gitlab capture) from the upstream docker-save tar.
#
# The derivation is cached in GHCR keyed by the upstream tar's pinned sha256,
# so a failed workflow never pays the ~45-minute derivation twice — and a cache
# entry is only ever created by this script, so its own CI run documents where
# the bytes came from.
#
# Inputs (env, set by builder/docker.py run_prepare):
#   DATASETS_DIR  where the verified upstream tar lives and outputs must land
#   REGISTRY      e.g. ghcr.io/shkolnik
set -euo pipefail

UPSTREAM_TAR="$DATASETS_DIR/gitlab-populated-final-port8023.tar"
UPSTREAM_TAG=gitlab-populated-final-port8023:latest
UPSTREAM_SHA=6269a90527a63c12d86fa4a5da8c5404553b78e75c5123712818c1b6d9f0972d
CACHE="$REGISTRY/webarena-gitlab-derived:${UPSTREAM_SHA:0:12}"
BK=webarena  # gitlab-backup expects <prefix>_gitlab_backup.tar

extract_outputs_from_cache() {
  local cid
  cid=$(docker create "$CACHE" true)
  docker export "$cid" | tar -x -C "$DATASETS_DIR"
  docker rm "$cid" >/dev/null
  cat "$DATASETS_DIR"/${BK}_gitlab_backup.tar.part-* \
    > "$DATASETS_DIR/${BK}_gitlab_backup.tar"
  rm -f "$DATASETS_DIR"/${BK}_gitlab_backup.tar.part-*
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
docker run -d --name gitlab-derive --hostname localhost --shm-size 1g "$UPSTREAM_TAG"
trap 'docker rm -f gitlab-derive >/dev/null 2>&1 || true' EXIT
for i in $(seq 1 90); do
  if [ "$(docker inspect -f '{{.State.Running}}' gitlab-derive)" != true ]; then
    echo "gitlab-derive exited (status: $(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}' gitlab-derive)); last logs:" >&2
    docker logs --tail 60 gitlab-derive >&2 || true
    exit 1
  fi
  code=$(docker exec gitlab-derive curl -s -o /dev/null -w '%{http_code}' http://localhost:8023/ || true)
  if [ "$code" = 302 ] || [ "$code" = 200 ]; then
    echo "up after ~$((i * 15))s"
    break
  fi
  [ "$i" = 90 ] && { echo "upstream gitlab never came up" >&2; exit 1; }
  sleep 15
done

echo "=== create backup + capture /etc/gitlab ==="
docker exec gitlab-derive gitlab-backup create BACKUP=$BK
docker cp "gitlab-derive:/var/opt/gitlab/backups/${BK}_gitlab_backup.tar" "$DATASETS_DIR/"
docker exec gitlab-derive tar czf /tmp/etc-gitlab.tar.gz -C / etc/gitlab
docker cp gitlab-derive:/tmp/etc-gitlab.tar.gz "$DATASETS_DIR/"
docker rm -f gitlab-derive
trap - EXIT
# Free the ~156G loaded upstream image before the build stage needs disk.
docker image rm -f "$UPSTREAM_TAG"

echo "=== push derived-inputs cache ==="
# Registry layers over ~10G are refused, so the backup tar ships split.
work=$(mktemp -d)
split -b 8G -d "$DATASETS_DIR/${BK}_gitlab_backup.tar" "$work/${BK}_gitlab_backup.tar.part-"
cp "$DATASETS_DIR/etc-gitlab.tar.gz" "$work/"
{
  echo "FROM scratch"
  for f in "$work"/${BK}_gitlab_backup.tar.part-*; do
    echo "COPY $(basename "$f") /"
  done
  echo "COPY etc-gitlab.tar.gz /"
} > "$work/Dockerfile"
docker build -t "$CACHE" "$work"
rm -rf "$work"
docker push "$CACHE" || echo "warning: cache push failed (continuing; derivation succeeded)"
echo "derive complete"
