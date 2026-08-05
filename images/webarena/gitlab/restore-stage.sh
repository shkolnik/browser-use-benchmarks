#!/bin/bash
# Runs inside the "restore" build stage of the gitlab Dockerfile.
# Boots the omnibus services, restores the pinned gitlab-backup tar, shuts
# everything down cleanly, then partitions /var/opt/gitlab into /staging
# buckets small enough for the final stage to COPY as <=10G registry layers.
set -euo pipefail

BK=webarena
BUCKET_LIMIT_KB=$((8 * 1024 * 1024))  # 8G target keeps layers under GHCR's ~10G comfort zone
BUCKET_COUNT=6  # must match the COPY --from lines in the Dockerfile's final stage

echo "=== boot services ==="
/assets/wrapper &
WRAPPER_PID=$!
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8023/ || true)
  if [ "$code" = 302 ] || [ "$code" = 200 ]; then
    echo "up after ~$((i * 15))s"
    break
  fi
  [ "$i" = 90 ] && { echo "gitlab never came up" >&2; exit 1; }
  sleep 15
done

echo "=== restore ==="
gitlab-ctl stop puma
gitlab-ctl stop sidekiq
gitlab-backup restore BACKUP=$BK force=yes
gitlab-ctl reconfigure

echo "=== clean shutdown ==="
gitlab-ctl stop
# A quiesced Postgres matters: the final image must start from a cleanly
# shut-down data directory, not one that looks like a crash. gitlab-ctl
# status exits non-zero when the service is down (that's the state we WANT),
# so capture output first instead of piping under pipefail.
pg_status=$(gitlab-ctl status postgresql 2>/dev/null || true)
echo "$pg_status" | grep -q '^down' || { echo "postgresql still up: $pg_status" >&2; exit 1; }
kill "$WRAPPER_PID" 2>/dev/null || true

echo "=== trim state not worth shipping ==="
rm -f /var/opt/gitlab/backups/${BK}_gitlab_backup.tar
rm -rf /var/log/gitlab/*

echo "=== partition /var/opt/gitlab into staging buckets ==="
# Greedy first-fit by du size; a subtree larger than the limit is descended
# into rather than split blindly, so bucket contents stay whole directories.
mkdir -p /staging
for i in $(seq 0 $((BUCKET_COUNT - 1))); do mkdir -p "/staging/bucket-0$i"; done
python3 - "$BUCKET_LIMIT_KB" "$BUCKET_COUNT" <<'EOF'
import os
import subprocess
import sys

LIMIT_KB = int(sys.argv[1])
MAX_BUCKETS = int(sys.argv[2])
ROOT = '/var/opt/gitlab'


def du_kb(path):
    return int(subprocess.check_output(['du', '-sk', path]).split()[0])


def partition(path):
    """Yield (path, kb) pieces each <= LIMIT_KB, descending into oversized dirs."""
    kb = du_kb(path)
    if kb <= LIMIT_KB:
        yield path, kb
        return
    entries = sorted(os.path.join(path, e) for e in os.listdir(path))
    if not entries:
        raise SystemExit(f'{path} is {kb}K with no children to descend into')
    for entry in entries:
        if os.path.isdir(entry) and not os.path.islink(entry):
            yield from partition(entry)
        else:
            yield entry, du_kb(entry)


buckets = []  # list of (used_kb, index)
assignments = []
for piece, kb in partition(ROOT):
    if kb > LIMIT_KB:
        raise SystemExit(f'single file {piece} is {kb}K, over the layer limit')
    for b in buckets:
        if b[0] + kb <= LIMIT_KB:
            b[0] += kb
            assignments.append((piece, b[1]))
            break
    else:
        if len(buckets) == MAX_BUCKETS:
            raise SystemExit('state outgrew BUCKET_COUNT buckets; '
                             'raise it and add COPY lines in the Dockerfile')
        buckets.append([kb, len(buckets)])
        assignments.append((piece, len(buckets) - 1))

for piece, idx in assignments:
    rel = os.path.relpath(piece, ROOT)
    dest = os.path.join('/staging', f'bucket-{idx:02d}', rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.rename(piece, dest)

print(f'{len(buckets)} buckets:')
for used, idx in buckets:
    print(f'  bucket-{idx:02d}: {used / 2**20:.1f}G')
EOF

echo "restore stage complete"
