#!/bin/bash
# Runs inside the "restore" build stage of the shopping Dockerfile.
# Boots the shipped supervisord stack, loads the derived DB dump + media +
# synthesizes config.php, reindexes search, validates the storefront in-build,
# shuts everything down cleanly (quiesced mariadb), trims junk, audits runtime
# file ownership, then partitions /opt/magento into /staging buckets small
# enough for the final stage to COPY as <=10G registry layers.
set -euo pipefail

MAGE=/opt/magento
BUCKET_LIMIT_KB=$((8 * 1024 * 1024))  # 8G target keeps layers under GHCR's ~10G comfort zone
BUCKET_COUNT=7  # must match the COPY --from lines in the Dockerfile's final stage

echo "=== boot the shipped stack ==="
/usr/bin/supervisord -c /etc/supervisord.conf &
SUP_PID=$!
sctl() { supervisorctl -c /etc/supervisord.conf "$@"; }

for i in $(seq 1 60); do
  mariadb-admin --socket=/run/mysqld/mysqld.sock ping >/dev/null 2>&1 && break
  [ "$i" = 60 ] && { echo "mariadb never came up" >&2; sctl status >&2 || true; exit 1; }
  sleep 2
done
for i in $(seq 1 120); do
  curl -fs http://127.0.0.1:9200/ >/dev/null 2>&1 && break
  [ "$i" = 120 ] && { echo "elasticsearch never came up" >&2; tail -50 /var/log/supervisor/elasticsearch.log >&2 || true; exit 1; }
  sleep 2
done
echo "mariadb + elasticsearch up"

echo "=== load DB (DEFINER-stripped, as in the experiment) ==="
mariadb <<'SQL'
CREATE DATABASE magentodb;
CREATE USER 'magentouser'@'localhost' IDENTIFIED BY 'MyPassword';
CREATE USER 'magentouser'@'127.0.0.1' IDENTIFIED BY 'MyPassword';
GRANT ALL PRIVILEGES ON magentodb.* TO 'magentouser'@'localhost';
GRANT ALL PRIVILEGES ON magentodb.* TO 'magentouser'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
zcat /mnt/shopping_db.sql.gz | sed 's/DEFINER=`[^`]*`@`[^`]*`//g' | mariadb magentodb
products=$(mariadb -N -e 'SELECT COUNT(*) FROM magentodb.catalog_product_entity')
[ "$products" = 104368 ] || { echo "product count $products != 104368 — bad DB load" >&2; exit 1; }
echo "DB loaded: $products products"

echo "=== media ==="
tar xf /mnt/shopping_media.tar -C "$MAGE/pub"
chown -R app:app "$MAGE/pub/media"
test -d "$MAGE/pub/media/catalog/product"

echo "=== synthesize config.php + reindex (NEVER setup:upgrade/di:compile: ==="
echo "=== core setup_module rows are absent in this dataset) ==="
cd "$MAGE"
runuser -u app -- php bin/magento module:enable --all
runuser -u app -- php bin/magento app:config:import -n
test -f app/etc/config.php
runuser -u app -- php bin/magento indexer:reindex
runuser -u app -- php bin/magento cache:flush

echo "=== in-build validation ==="
code=$(curl -s -o /tmp/home.html -w '%{http_code}' http://127.0.0.1:7770/)
[ "$code" = 200 ] || { echo "storefront returned $code" >&2; tail -50 "$MAGE"/var/log/*.log >&2 || true; exit 1; }
grep -q "One Stop Market" /tmp/home.html || { echo "storefront 200 but no 'One Stop Market'" >&2; exit 1; }
scode=$(curl -s -o /tmp/search.html -w '%{http_code}' 'http://127.0.0.1:7770/catalogsearch/result/?q=toothbrush')
[ "$scode" = 200 ] && grep -q 'product-item-link' /tmp/search.html \
  || { echo "catalog search not working (code $scode)" >&2; exit 1; }
echo "storefront + search OK in-build"

echo "=== clean shutdown ==="
sctl stop nginx php-fpm
sctl stop elasticsearch redis
sctl stop mariadb
# A quiesced MariaDB matters: the shipped image must start from a cleanly
# shut-down datadir, not one that looks like a crash.
if pgrep -x mariadbd >/dev/null; then echo "mariadbd still running after stop" >&2; exit 1; fi
grep -q "Shutdown complete" /var/log/supervisor/mariadb.log \
  || { echo "no 'Shutdown complete' in mariadb log" >&2; tail -20 /var/log/supervisor/mariadb.log >&2; exit 1; }
sctl shutdown || true
wait "$SUP_PID" 2>/dev/null || true

echo "=== trim state not worth shipping ==="
rm -rf "$MAGE"/var/log/* "$MAGE"/var/cache/* "$MAGE"/var/page_cache/* \
       "$MAGE"/var/session/* "$MAGE"/var/report/* "$MAGE"/var/tmp/* /tmp/home.html /tmp/search.html
rm -rf /var/log/supervisor/* /var/log/nginx/* /var/log/elasticsearch/* /var/log/mysql/* /run/mysqld/*
# Redis persistence is off (see redis-shopping.conf), so no dump.rdb should
# exist anywhere — the gitlab image's root-owned-dump defect class. Verify.
found=$(find / -xdev -name 'dump.rdb' 2>/dev/null || true)
[ -z "$found" ] && : || { echo "unexpected redis dump.rdb: $found" >&2; exit 1; }

echo "=== ownership audit: everything a service reads at runtime ==="
# Root-run maintenance tooling drops root-owned metadata files in the datadir
# (Debian's install-time debian-NN.flag, mariadb-upgrade's mysql_upgrade_info).
# They are maintenance metadata, not data mariadbd serves, but normalize them
# so the audit below stays a zero-tolerance check.
chown mysql:mysql /var/lib/mysql/debian-*.flag /var/lib/mysql/mysql_upgrade_info 2>/dev/null || true
bad=$(find /var/lib/mysql ! -user mysql -print 2>/dev/null | head -5)
[ -z "$bad" ] || { echo "non-mysql-owned files in datadir: $bad" >&2; exit 1; }
bad=$(find /var/lib/elasticsearch ! -user elasticsearch -print 2>/dev/null | head -5)
[ -z "$bad" ] || { echo "non-elasticsearch-owned files in ES data: $bad" >&2; exit 1; }
bad=$(find "$MAGE" ! -user app -print 2>/dev/null | head -5)
[ -z "$bad" ] || { echo "non-app-owned files in magento tree: $bad" >&2; exit 1; }

echo "=== partition $MAGE into staging buckets ==="
mkdir -p /staging
for i in $(seq 0 $((BUCKET_COUNT - 1))); do mkdir -p "/staging/bucket-0$i"; done
python3 - "$BUCKET_LIMIT_KB" "$BUCKET_COUNT" "$MAGE" <<'EOF'
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
    yield from children(path, kb)


def children(path, kb):
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
# children(), never partition(): ROOT must not be yielded as a piece of
# itself. relpath(ROOT, ROOT) is '.', so the move lands at bucket-NN/. and
# shutil nests the whole tree as bucket-NN/<basename> — the final image then
# gets /app/app/... and nginx's `root /app/public` points at nothing. It only
# bites when the tree fits in ONE bucket, so a shrinking dataset is all it
# takes; caught by booting a build made with an empty media tar.
for piece, kb in children(ROOT, du_kb(ROOT)):
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
    # Not os.rename: paths from lower overlayfs layers EXDEV on cross-layer
    # directory renames; shutil.move falls back to copy+delete there.
    shutil.move(piece, dest)

print(f'{len(buckets)} buckets:')
for used, idx in buckets:
    print(f'  bucket-{idx:02d}: {used / 2**20:.1f}G')
EOF
# The bucket move drops directory ownership context for parents created by
# makedirs (root-owned): re-assert app ownership so the COPY layers ship
# app-owned trees.
chown -R app:app /staging

echo "restore stage complete"
