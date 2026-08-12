#!/bin/bash
# Runs inside the "restore" build stage of the map-nominatim Dockerfile.
#
# Unpacks the upstream Nominatim Postgres cluster to the path the base image's
# postgresql.conf reads (/var/lib/postgresql/14/main), proves the cluster is
# real by STARTING it and querying the geocoding table, then partitions it into
# buckets small enough to COPY as <=10G layers.
set -euo pipefail

TAR=/tmp/nominatim_volumes.tar
DUMP_TAR=/tmp/osm_dump.tar
PGDATA=/var/lib/postgresql/14/main
BUCKET_LIMIT_KB=$((8 * 1024 * 1024))  # 8G target keeps layers under GHCR's ~10G comfort zone
BUCKET_COUNT=6  # must match the COPY --from lines in the Dockerfile's final stage

echo "=== extract the upstream Postgres cluster ==="
# --strip-components=7 removes
# projects/metis2/docker/docker/volumes/nominatim-data/_data/, which is 7
# components, so the cluster's own top level (PG_VERSION, base/, global/ …)
# lands directly in $PGDATA.
#
# NOT upstream's 5, and not the tile image's 4. Upstream strips 5 because it
# extracts into /var/lib/docker/volumes and wants the docker volume layout
# (nominatim-data/_data/…) reconstructed there; we extract into the cluster
# path itself, so the volume name and _data are two more components to drop.
# The three tars in this backend genuinely differ — count per tar, never copy
# the number from a sibling.
#
# The member argument is load-bearing, not cosmetic. This archive holds TWO
# volumes: nominatim-data (34.8G, the cluster) and nominatim-flatnode (87G,
# osm2pgsql's node cache). Naming the member extracts only the first — see
# README.md for why the flatnode file is deliberately not shipped. tar still
# reads all 116G to find the members, but writes only the 34.8G we keep.
#
# --numeric-owner is load-bearing too: the archive carries uid/gid 101:103,
# which is exactly postgres:postgres inside this image (verified with `id`).
# Resolving by NAME against the build host's /etc/passwd would remap it, and a
# cluster postgres cannot read is a cluster postgres will not start.
rm -rf "$PGDATA"
mkdir -p "$PGDATA"
tar --numeric-owner -C "$PGDATA" --strip-components=7 -xf "$TAR" \
    projects/metis2/docker/docker/volumes/nominatim-data
rm -f "$TAR"

[ -f "$PGDATA/PG_VERSION" ] || { echo "restore: $PGDATA/PG_VERSION missing — tar layout changed" >&2; exit 1; }

# The snapshot was taken from a volume whose container had run, so it carries a
# postmaster.pid naming a pid and shared-memory segment from that host. Postgres
# treats a pid file it cannot attribute as a reason to refuse to start. Removing
# it is the documented recovery for a copied data directory, and it is safe here
# precisely because nothing is running: this is a build stage with no postmaster.
rm -f "$PGDATA/postmaster.pid"

echo "=== unpack the Nominatim project data (osm_dump) ==="
# --strip-components=1 removes the archive's own osm_dump/ prefix, so the two
# files land at the paths upstream's env vars name: /nominatim/data/…
#
# Upstream extracts this tar with NO strip into /opt/osm_dump and then mounts
# that at /nominatim/data, which puts the files at /nominatim/data/osm_dump/…
# while PBF_PATH says /nominatim/data/us-northeast-latest.osm.pbf. That path
# does not exist upstream. It never bites, because the only consumer is
# init.sh, which never runs once the cluster is imported — but it means the
# upstream setting cannot be copied verbatim and called verified. Stripping the
# prefix makes the documented paths resolve. See README.md.
mkdir -p /nominatim/data
tar -C /nominatim/data --strip-components=1 -xf "$DUMP_TAR"
rm -f "$DUMP_TAR"
for f in us-northeast-latest.osm.pbf wikimedia-importance.sql.gz; do
  [ -f "/nominatim/data/$f" ] || { echo "restore: /nominatim/data/$f missing — osm_dump layout changed" >&2; exit 1; }
done

echo "=== validate: structure ==="
# A Postgres cluster only starts under its own major version, and this base
# image's postgresql.conf hard-codes the 14 path. If a future base bump moves
# to 15, fail here rather than ship an image whose database refuses to start.
DATA_PG=$(cat "$PGDATA/PG_VERSION")
IMAGE_PG=$(ls -d /usr/lib/postgresql/*/ | head -1 | sed 's#.*/postgresql/##; s#/##')
echo "restore: cluster PG_VERSION=$DATA_PG, image ships postgresql-$IMAGE_PG"
[ "$DATA_PG" = "$IMAGE_PG" ] || {
  echo "restore: PG MAJOR MISMATCH — baked cluster is $DATA_PG but this image ships $IMAGE_PG." >&2
  echo "restore: Postgres will not start. Pin a base image with postgresql-$DATA_PG." >&2
  exit 1; }

# /app/start.sh runs the FULL import (a multi-hour osm2pgsql run that would also
# need the pbf and a flatnode file) unless this marker exists. Upstream's own
# start.sh created it inside this volume after its import finished, so it must
# have arrived in the tar. Assert it: without the marker the shipped image would
# try to re-import on every boot, and the first thing anyone would see is a
# container that never becomes healthy.
[ -f "$PGDATA/import-finished" ] || {
  echo "restore: $PGDATA/import-finished missing — /app/start.sh would re-run the whole import." >&2
  echo "restore: direct children of the cluster directory:" >&2
  ls -la "$PGDATA" >&2
  exit 1; }

owner=$(stat -c %U:%G "$PGDATA")
[ "$owner" = "postgres:postgres" ] || {
  echo "restore: $PGDATA is owned by $owner, expected postgres:postgres" >&2; exit 1; }

db_kb=$(du -sk "$PGDATA" | cut -f1)
[ "$db_kb" -gt 20971520 ] || { echo "restore: $PGDATA is only ${db_kb}K — truncated" >&2; exit 1; }
echo "restore: cluster $((db_kb / 1048576))G"

echo "=== validate: the cluster actually starts and holds the gazetteer ==="
# Structural checks cannot tell a real gazetteer from an empty one that happens
# to have the right file names. This starts the cluster and asks it, which also
# gets the crash recovery this snapshot needs done ONCE at build time instead of
# on every boot of every pulled image: the volume was captured from a running
# container, so its WAL needs replaying, and postgres does that on first start.
chmod 700 "$PGDATA"
service postgresql start
trap 'service postgresql stop || true' EXIT

sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='nominatim'" | grep -q 1 || {
  echo "restore: no 'nominatim' database in the baked cluster" >&2
  sudo -u postgres psql -tAc "SELECT datname FROM pg_database" >&2
  exit 1; }

# Counted through a LIMIT rather than as a bare count(*): the floor is what
# matters, and this stops reading at a million and one rows instead of seq
# scanning a table of tens of millions. Parallel workers are disabled for it
# because they allocate through /dev/shm, which is 64M in a build container —
# an infrastructure failure that would read as a data failure.
places=$(sudo -u postgres psql -d nominatim -tAc \
  "SET max_parallel_workers_per_gather = 0; SELECT count(*) FROM (SELECT 1 FROM placex LIMIT 1000001) t")
echo "restore: placex holds at least $places rows"
# us-northeast is tens of millions of places; a floor this low only catches the
# catastrophic case (empty or partial import) without pinning a figure that a
# different extract would legitimately move.
[ "$places" -gt 1000000 ] || { echo "restore: placex has only $places rows — not a completed import" >&2; exit 1; }

service postgresql stop
trap - EXIT
# Stopped cleanly, so the shipped cluster is a clean shutdown rather than the
# crashed snapshot we started from.
rm -f "$PGDATA/postmaster.pid"

echo "=== partition the cluster into staging buckets ==="
# Shared implementation: builder/stage-lib/partition-tree.py, via the `stagelib`
# build context. Ownership preservation is the whole point here — every file
# under the cluster must reach the final image owned by postgres, and a
# recreated parent that landed root-owned would leave postgres unable to read
# its own data directory.
mkdir -p /staging
for i in $(seq 0 $((BUCKET_COUNT - 1))); do mkdir -p "$(printf '/staging/bucket-%02d' "$i")"; done
python3 /partition-tree.py "$BUCKET_LIMIT_KB" "$BUCKET_COUNT" "$PGDATA" /staging

echo "restore stage complete"
