#!/bin/bash
# Runs inside the "restore" build stage of the map-tile Dockerfile.
# Unpacks the upstream tile-server volumes to the paths the image actually
# reads, proves the cluster matches this image's Postgres major, then
# partitions /data into buckets small enough to COPY as <=10G layers.
set -euo pipefail

TAR=/tmp/osm_tile_server.tar
BUCKET_LIMIT_KB=$((8 * 1024 * 1024))  # 8G target keeps layers under GHCR's ~10G comfort zone
BUCKET_COUNT=8  # must match the COPY --from lines in the Dockerfile's final stage

echo "=== extract the upstream tile database ==="
# The base image ships its OWN initdb'd cluster at /data/database/postgres,
# created when the image was built. Extracting on top of it would MERGE two
# unrelated clusters: the files whose names collide get overwritten and every
# base-only file survives, including WAL segments stamped with a different
# system identifier. That is a corrupt data directory, not a restored one — and
# that a merged one happens to START proves nothing about what it holds. Wipe
# first, and recreate renderer-owned, which is the base image's ownership for
# /data/*; the cluster inside is postgres-owned and carries that from the tar.
rm -rf /data/database /data/tiles
install -d -o renderer -g renderer /data/database /data/tiles

# --strip-components=6 removes projects/ogma3/docker/volumes/osm-data/_data/,
# landing the volume's contents directly where upstream bind-mounts them
# (`--volume=osm-data:/data/database/`). Extracting to the destination beats
# unpacking to a staging path and moving: same result, one pass, no second
# traversal of 1,624 entries.
#
# Neither upstream's 5 nor an earlier version of this file's 4. Upstream strips
# 5 because it extracts into /var/lib/docker/volumes and wants docker's volume
# layout rebuilt there — but it justifies the number with the
# 'projects/ogma3/docker/volumes/' prefix, which is only 4 components, so
# upstream's own tile extraction eats the volume NAME. The three tars in this
# backend all differ: count per tar, never copy the number from a sibling.
#
# --numeric-owner is load-bearing. The archive carries uid/gid 101:103 for the
# postgres cluster and 1000:1000 for the volume root, which are exactly
# postgres:postgres and renderer:renderer in this image (verified with `id`).
# Resolving by NAME against the build host's /etc/passwd would remap both.
tar --numeric-owner -C /data/database --strip-components=6 -xf "$TAR"
rm -f "$TAR"

# There is NO osm-tiles volume in this archive — a full listing is 1,624 entries
# and all of them are under osm-data. Upstream's cloud-init mounts
# `--volume=osm-tiles:/data/tiles/`, and an earlier version of this script read
# that as evidence the tar carried two volumes; it does not, and the build
# failed here rather than shipping a wrong tree. Docker creates that volume
# empty at run time, which is correct: /data/tiles is the RENDERED TILE CACHE,
# built on demand from the database. An empty directory is the honest initial
# state, and the healthcheck's first zoom-0 request is what fills it.
[ -d /data/database/postgres ] || { echo "restore: /data/database/postgres missing — tar layout changed" >&2; exit 1; }

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
# Shared implementation: builder/stage-lib/partition-tree.py, reached through
# the `stagelib` build context. This image needs the ownership-preserving
# behaviour for the same reason gitlab does, if less dramatically: /data is
# split between postgres (the cluster) and renderer (the tiles), so a
# recreated parent directory that lands root-owned would leave Postgres unable
# to read its own cluster — asserted above, and worth not undoing here.
mkdir -p /staging
for i in $(seq 0 $((BUCKET_COUNT - 1))); do mkdir -p "$(printf '/staging/bucket-%02d' "$i")"; done
python3 /partition-tree.py "$BUCKET_LIMIT_KB" "$BUCKET_COUNT" /data /staging

echo "restore stage complete"
