#!/bin/sh
# Build-time restore: initialise a postgres 14 cluster, load the derived dump,
# and unpack the uploaded media. All of this happens ONCE at build time, so a
# container boot does no restore work at all — the same property the Magento
# and gitlab images have.
set -eux

PGDATA=/var/lib/postgresql/data
PGBIN=/usr/libexec/postgresql14
DB_NAME=postmill
DB_USER=postmill
DB_PASS=secret

mkdir -p "$PGDATA" /run/postgresql
chown -R postgres:postgres "$PGDATA" /run/postgresql

su-exec postgres "$PGBIN/initdb" -D "$PGDATA" --encoding=UTF8 --locale=C
su-exec postgres "$PGBIN/pg_ctl" -D "$PGDATA" -o "-c listen_addresses=127.0.0.1" -w start

# The dump was taken from the upstream container's `postmill` database, owned by
# role `postmill` — verified by sampling pg_stat_activity under live requests,
# not by reading .env (whose db_user/db_name placeholders name a role that does
# not exist). Recreating the same role/database is what lets the dump restore
# unmodified, with its OWNER TO statements resolving.
su-exec postgres psql -v ON_ERROR_STOP=1 -c \
  "CREATE ROLE $DB_USER WITH LOGIN SUPERUSER PASSWORD '$DB_PASS';"
su-exec postgres createdb -O "$DB_USER" "$DB_NAME"

# Decompress and load as TWO steps, not `gzip -dc … | psql`. This script is
# `sh` with `set -eux` and no pipefail, so a pipeline reports only psql's
# status — and psql exits 0 on an empty stream. Run 31126802473 "loaded" a
# 499.7 MB dump in seven milliseconds that way and sailed on; the failure did
# not surface until the audit stage asked for a table. Same lesson the derive
# script learned about `pg_dump | gzip`, on the other end of the same file.
gzip -dc /tmp/reddit_db.sql.gz > /tmp/reddit_db.sql

# Third net, behind the builder's stamp and the derive floor: refuse to restore
# from a dump that is obviously not the one that was measured. Measured here,
# not estimated — 499,699,542 bytes gzipped expands to 1,333,264,689. The floor
# is ~60% of that: data may grow, it may not collapse.
sql_bytes=$(stat -c %s /tmp/reddit_db.sql)
if [ "$sql_bytes" -lt 800000000 ]; then
  echo "restore: /tmp/reddit_db.sql is $sql_bytes bytes — far below the measured dump; refusing to restore an empty database" >&2
  exit 1
fi
echo "restore: dump = $sql_bytes bytes"

su-exec postgres psql -v ON_ERROR_STOP=1 -q "$DB_NAME" -f /tmp/reddit_db.sql
rm -f /tmp/reddit_db.sql

su-exec postgres "$PGBIN/pg_ctl" -D "$PGDATA" -w stop

# Uploaded images — this is what "withimg" in the upstream tar's name means:
# public/media is 2.4 MB, but public/submission_images beside it is 38.5G.
mkdir -p /app/public
tar xf /tmp/reddit_media.tar -C /app/public
chown -R www-data:www-data /app/public

rm -f /tmp/reddit_db.sql.gz /tmp/reddit_media.tar

# ==========
# Partition /app into staging buckets, one per final-image layer, so no layer
# carries the whole 39G tree. Shared implementation:
# builder/stage-lib/partition-tree.py, reached through the `stagelib` build
# context.
# ==========
BUCKET_LIMIT_KB=$((8 * 1024 * 1024))  # 8G target, under GHCR's ~10G comfort zone
BUCKET_COUNT=7  # must match the COPY --from lines in the Dockerfile's final stage

# The postgres cluster ships as its own single layer; assert it fits rather
# than discovering a 10G+ layer at push time.
pgkb=$(du -sk /var/lib/postgresql/data | cut -f1)
if [ "$pgkb" -gt "$BUCKET_LIMIT_KB" ]; then
  echo "restore: postgres cluster is $((pgkb >> 20))G, over the $((BUCKET_LIMIT_KB >> 20))G layer target — it needs partitioning too" >&2
  exit 1
fi
echo "restore: postgres cluster = $((pgkb >> 20))G"

apk add --no-cache python3

mkdir -p /staging
for i in $(seq 0 $((BUCKET_COUNT - 1))); do mkdir -p "$(printf '/staging/bucket-%02d' "$i")"; done
python3 /partition-tree.py "$BUCKET_LIMIT_KB" "$BUCKET_COUNT" /app /staging

# The partitioner mirrors each recreated parent's owner and mode, so this is
# not repairing a loss — it is the same single-owner re-assertion ../../vwa/
# classifieds makes, cheap insurance on a tree that has exactly one owner.
chown -R www-data:www-data /staging
