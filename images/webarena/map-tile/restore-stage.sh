#!/bin/bash
# Runs inside the "restore" build stage of the map-tile Dockerfile.
# Unpacks the upstream tile-server volumes to the paths the image actually
# reads, proves the cluster matches this image's Postgres major, then
# partitions /data into buckets small enough to COPY as <=10G layers.
set -euo pipefail

TAR=/tmp/osm_tile_server.tar
BUCKET_LIMIT_KB=$((8 * 1024 * 1024))  # 8G target keeps layers under GHCR's ~10G comfort zone
BUCKET_COUNT=8  # must match the COPY --from lines in the Dockerfile's final stage

echo "=== extract upstream volumes ==="
# ONE pass over a 41G archive. --strip-components=4 removes
# projects/ogma3/docker/volumes/ and leaves osm-data/_data and osm-tiles/_data.
#
# 4, NOT the 5 upstream's cloud-init uses. Upstream justifies 5 with the
# 'projects/ogma3/docker/volumes/' prefix, but that prefix is only 4 components;
# 5 also eats the volume NAME, collapsing osm-data and osm-tiles onto the same
# path. (5 IS correct for the nominatim tar, whose prefix differs — count per
# tar, never copy the number.)
#
# --numeric-owner is load-bearing. The archive carries uid/gid 101:103 for the
# postgres cluster and 1000:1000 for the volume root, which are exactly
# postgres:postgres and renderer:renderer in this image (verified with `id`).
# Resolving by NAME against the build host's /etc/passwd would remap both.
mkdir -p /tmp/vol
tar --numeric-owner -C /tmp/vol --strip-components=4 -xf "$TAR"
rm -f "$TAR"

for v in osm-data osm-tiles; do
  [ -d "/tmp/vol/$v/_data" ] || { echo "restore: /tmp/vol/$v/_data missing — tar layout changed" >&2; exit 1; }
done

# Upstream bind-mounts osm-data at /data/database and osm-tiles at /data/tiles
# (webarena-map-backend-boot-init.yaml `docker run --volume=` lines). Same
# filesystem, so these are renames, not copies.
mkdir -p /data/database /data/tiles
mv /tmp/vol/osm-data/_data/. /data/database/
mv /tmp/vol/osm-tiles/_data/. /data/tiles/
rmdir /tmp/vol/osm-data/_data /tmp/vol/osm-data /tmp/vol/osm-tiles/_data /tmp/vol/osm-tiles /tmp/vol

echo "=== validate ==="
# A Postgres cluster only starts under its OWN major version. The baked cluster
# is PG15 (upstream ran an untagged :latest built from master minutes after
# PG15 support landed — see README). If a future base-image bump moves to PG16,
# this build must fail here rather than ship an image whose database silently
# refuses to start.
DATA_PG=$(cat /data/database/postgres/PG_VERSION)
IMAGE_PG=$(ls -d /usr/lib/postgresql/*/ | head -1 | sed 's#.*/postgresql/##; s#/##')
echo "restore: cluster PG_VERSION=$DATA_PG, image ships postgresql-$IMAGE_PG"
[ "$DATA_PG" = "$IMAGE_PG" ] || {
  echo "restore: PG MAJOR MISMATCH — baked cluster is $DATA_PG but this image ships $IMAGE_PG." >&2
  echo "restore: Postgres will not start. Pin a base image with postgresql-$DATA_PG." >&2
  exit 1; }

# The import marker is what the tile server checks to decide the DB is usable.
[ -f /data/database/planet-import-complete ] || {
  echo "restore: /data/database/planet-import-complete missing — the dump is not a completed import" >&2; exit 1; }

# Ownership must have survived the extract, or postgres cannot read its own
# cluster. Assert rather than chown: a blanket chown would erase the
# postgres/renderer split this image depends on.
owner=$(stat -c %U:%G /data/database/postgres)
[ "$owner" = "postgres:postgres" ] || {
  echo "restore: /data/database/postgres is owned by $owner, expected postgres:postgres" >&2; exit 1; }

# Size floor: an empty or truncated tile DB would otherwise build clean and
# serve blank tiles.
db_kb=$(du -sk /data/database | cut -f1)
[ "$db_kb" -gt 10485760 ] || { echo "restore: /data/database is only ${db_kb}K — truncated import" >&2; exit 1; }
echo "restore: database $((db_kb / 1048576))G, tiles $(du -sh /data/tiles | cut -f1)"

echo "=== partition /data into staging buckets ==="
# Greedy first-fit by du size; a subtree larger than the limit is descended
# into rather than split blindly, so bucket contents stay whole directories.
# Same algorithm as images/webarena/gitlab/restore-stage.sh (task #53 tracks
# lifting this into a shared partition-tree.py).
mkdir -p /staging
for i in $(seq 0 $((BUCKET_COUNT - 1))); do mkdir -p "/staging/bucket-0$i"; done
python3 - "$BUCKET_LIMIT_KB" "$BUCKET_COUNT" <<'EOF'
import os
import shutil
import subprocess
import sys

LIMIT_KB = int(sys.argv[1])
MAX_BUCKETS = int(sys.argv[2])
ROOT = '/data'


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
    # Not os.rename: paths from the base image live in a lower overlayfs
    # layer, and overlay returns EXDEV for cross-layer directory renames.
    shutil.move(piece, dest)

print(f'{len(buckets)} buckets:')
for used, idx in buckets:
    print(f'  bucket-{idx:02d}: {used / 2**20:.1f}G')
EOF

echo "restore stage complete"
