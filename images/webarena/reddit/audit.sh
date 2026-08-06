#!/bin/sh
# Fail-loud audit of the restored database, in its OWN build stage on purpose.
#
# In ../shopping this check lived inside the ~11-minute restore RUN, and two
# builds were lost re-running the entire restore just to re-run the assertion
# after it failed. Keeping it in a separate layer means a failed audit costs
# only the audit.
#
# The counts are not invented: they were measured in the upstream container
# before any rebuild existed. If the rebuilt database disagrees with the image
# we are replacing, the restore silently lost or duplicated rows, and that must
# stop the build rather than ship.
set -eux

PGDATA=/var/lib/postgresql/data
PGBIN=/usr/libexec/postgresql14
DB_NAME=db_name

mkdir -p /run/postgresql
chown -R postgres:postgres /run/postgresql
su-exec postgres "$PGBIN/pg_ctl" -D "$PGDATA" -o "-c listen_addresses=127.0.0.1" -w start

fail=0
check() {
  actual=$(su-exec postgres psql -tAq -d "$DB_NAME" -c "select count(*) from $1;")
  if [ "$actual" != "$2" ]; then
    echo "AUDIT FAIL: $1 has $actual rows, expected $2" >&2
    fail=1
  else
    echo "audit ok: $1 = $actual"
  fi
}

check users 661782
check forums 95
check submissions 127391
check comments 2551513

su-exec postgres "$PGBIN/pg_ctl" -D "$PGDATA" -w stop
[ "$fail" = 0 ] || { echo "audit failed — refusing to ship this image" >&2; exit 1; }
echo "audit passed"
