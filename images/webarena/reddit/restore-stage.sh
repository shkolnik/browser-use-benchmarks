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

gzip -dc /tmp/reddit_db.sql.gz | su-exec postgres psql -v ON_ERROR_STOP=1 -q "$DB_NAME"

su-exec postgres "$PGBIN/pg_ctl" -D "$PGDATA" -w stop

# Uploaded images — this is what "withimg" in the upstream tar's name means:
# public/media is 2.4 MB, but public/submission_images beside it is 38.5G.
mkdir -p /app/public
tar xf /tmp/reddit_media.tar -C /app/public
chown -R www-data:www-data /app/public

rm -f /tmp/reddit_db.sql.gz /tmp/reddit_media.tar

# ==========
# Partition /app into staging buckets, one per final-image layer, so no layer
# carries the whole 39G tree. Same partitioner as ../shopping/restore-stage.sh.
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
for i in $(seq 0 $((BUCKET_COUNT - 1))); do mkdir -p "/staging/bucket-0$i"; done
python3 - "$BUCKET_LIMIT_KB" "$BUCKET_COUNT" /app <<'EOF'
import os
import shutil
import subprocess
import sys

LIMIT_KB = int(sys.argv[1])
MAX_BUCKETS = int(sys.argv[2])
ROOT = sys.argv[3]


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


buckets = []  # list of [used_kb, index]
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
            raise SystemExit('app tree outgrew BUCKET_COUNT buckets; '
                             'raise it and add COPY lines in the Dockerfile')
        buckets.append([kb, len(buckets)])
        assignments.append((piece, len(buckets) - 1))

for piece, idx in assignments:
    rel = os.path.relpath(piece, ROOT)
    dest = os.path.join('/staging', f'bucket-{idx:02d}', rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # Not os.rename: paths from lower overlayfs layers EXDEV on cross-layer
    # directory renames; shutil.move falls back to copy+delete there.
    shutil.move(piece, dest)

print(f'{len(buckets)} buckets:')
for used, idx in buckets:
    print(f'  bucket-{idx:02d}: {used / 2**20:.1f}G')
EOF

# makedirs creates the intermediate parents root-owned, so re-assert ownership
# to ship app-owned trees in every COPY layer.
chown -R www-data:www-data /staging
