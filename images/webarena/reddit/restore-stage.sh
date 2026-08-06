#!/bin/sh
# Build-time restore: initialise a postgres 14 cluster, load the derived dump,
# and unpack the uploaded media. All of this happens ONCE at build time, so a
# container boot does no restore work at all — the same property the Magento
# and gitlab images have.
set -eux

PGDATA=/var/lib/postgresql/data
PGBIN=/usr/libexec/postgresql14
DB_NAME=db_name
DB_USER=db_user
DB_PASS=db_password

mkdir -p "$PGDATA" /run/postgresql
chown -R postgres:postgres "$PGDATA" /run/postgresql

su-exec postgres "$PGBIN/initdb" -D "$PGDATA" --encoding=UTF8 --locale=C
su-exec postgres "$PGBIN/pg_ctl" -D "$PGDATA" -o "-c listen_addresses=127.0.0.1" -w start

# The dump was taken from the upstream container, which uses these literal
# placeholder credentials; recreating the same role/database is what lets the
# dump restore unmodified.
su-exec postgres psql -v ON_ERROR_STOP=1 -c \
  "CREATE ROLE $DB_USER WITH LOGIN SUPERUSER PASSWORD '$DB_PASS';"
su-exec postgres createdb -O "$DB_USER" "$DB_NAME"

gzip -dc /tmp/reddit_db.sql.gz | su-exec postgres psql -v ON_ERROR_STOP=1 -q "$DB_NAME"

su-exec postgres "$PGBIN/pg_ctl" -D "$PGDATA" -w stop

# Uploaded images ("withimg" in the upstream tar's name — all 2.4 MB of it).
mkdir -p /app/public
tar xf /tmp/reddit_media.tar -C /app/public
chown -R www-data:www-data /app/public

rm -f /tmp/reddit_db.sql.gz /tmp/reddit_media.tar
