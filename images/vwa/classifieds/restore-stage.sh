#!/bin/bash
# Build-time restore: initialise a MySQL 8.4 datadir, load the seed dump, unpack
# the item photos, then partition the app tree into per-layer staging buckets.
# All of it happens ONCE at build time, so a container boot does no restore work.
set -euxo pipefail

DB_NAME=osclass
DB_PASS=password
APP=/usr/src/myapp

unzip -qq -j /tmp/compose.zip \
    classifieds_docker_compose/mysql/osclass_craigslist.sql -d /tmp
rm -f /tmp/compose.zip

mkdir -p /var/run/mysqld /var/lib/mysql
chown -R mysql:mysql /var/run/mysqld /var/lib/mysql

mysqld --initialize-insecure --user=mysql --datadir=/var/lib/mysql
mysqld --user=mysql --datadir=/var/lib/mysql --skip-networking &
mysqld_pid=$!
for i in $(seq 1 60); do
  mysqladmin --socket=/var/run/mysqld/mysqld.sock ping >/dev/null 2>&1 && break
  [ "$i" = 60 ] && { echo "restore: mysqld never accepted connections" >&2; exit 1; }
  sleep 2
done

# `--skip-networking` above and `root@localhost` here are the same decision:
# the appliance only ever talks to its own socket, so the server never opens a
# port and the only account is the local one config.php uses.
mysql --socket=/var/run/mysqld/mysqld.sock -uroot <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '$DB_PASS';
CREATE DATABASE $DB_NAME;
SQL
mysql --socket=/var/run/mysqld/mysqld.sock -uroot -p"$DB_PASS" "$DB_NAME" < /tmp/osclass_craigslist.sql
mysqladmin --socket=/var/run/mysqld/mysqld.sock -uroot -p"$DB_PASS" shutdown
wait "$mysqld_pid" || true
rm -f /tmp/osclass_craigslist.sql

# The item photos — 73G across 84,148 per-item directories. This is the payload
# that exists nowhere but the upstream image; see derive-backup.sh.
tar xf /tmp/classifieds_uploads.tar -C "$APP/oc-content"
rm -f /tmp/classifieds_uploads.tar

# ==========
# Partition the app tree into staging buckets, one per final-image layer, so no
# layer carries the whole 73G tree. Shared implementation:
# builder/stage-lib/partition-tree.py, reached through the `stagelib` build
# context.
# ==========
BUCKET_LIMIT_KB=$((8 * 1024 * 1024))  # 8G target, under GHCR's ~10G comfort zone
BUCKET_COUNT=12  # must match the COPY --from lines in the Dockerfile's final stage

# The datadir ships as its own single layer; assert it fits rather than
# discovering a 10G+ layer at push time.
dbkb=$(du -sk /var/lib/mysql | cut -f1)
if [ "$dbkb" -gt "$BUCKET_LIMIT_KB" ]; then
  echo "restore: mysql datadir is $((dbkb >> 20))G, over the $((BUCKET_LIMIT_KB >> 20))G layer target — it needs partitioning too" >&2
  exit 1
fi
echo "restore: mysql datadir = $((dbkb >> 20))G"

mkdir -p /staging
for i in $(seq 0 $((BUCKET_COUNT - 1))); do mkdir -p "$(printf '/staging/bucket-%02d' "$i")"; done
python3 /partition-tree.py "$BUCKET_LIMIT_KB" "$BUCKET_COUNT" "$APP" /staging

# makedirs creates the intermediate parents root-owned, so re-assert ownership
# to ship app-owned trees in every COPY layer.
chown -R www-data:www-data /staging
