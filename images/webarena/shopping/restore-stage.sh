#!/bin/bash
# Runs inside the "restore" build stage of the shopping Dockerfile.
# Boots the shipped service stack, loads the derived DB dump + media +
# synthesizes config.php, reindexes search, validates the storefront in-build,
# shuts everything down cleanly (quiesced mariadb), trims junk, audits runtime
# file ownership, then partitions /opt/magento into /staging buckets small
# enough for the final stage to COPY as <=10G registry layers.
set -euo pipefail

MAGE=/opt/magento
BUCKET_LIMIT_KB=$((8 * 1024 * 1024))  # 8G target keeps layers under GHCR's ~10G comfort zone
BUCKET_COUNT=7  # must match the COPY --from lines in the Dockerfile's final stage

echo "=== boot the shipped stack ==="
. /run-services.sh
. /services.sh
svc_start_stack

for i in $(seq 1 60); do
  mariadb-admin --socket=/run/mysqld/mysqld.sock ping >/dev/null 2>&1 && break
  [ "$i" = 60 ] && { echo "mariadb never came up" >&2; svc_status >&2 || true; exit 1; }
  sleep 2
done
for i in $(seq 1 120); do
  curl -fs http://127.0.0.1:9200/ >/dev/null 2>&1 && break
  [ "$i" = 120 ] && { echo "elasticsearch never came up" >&2; tail -n 50 /var/log/supervisor/elasticsearch.log >&2 || true; exit 1; }
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

# Rebase the restored dataset off CMU's deployment. core_config_data still names
# metis.lti.cs.cmu.edu:7770, and Magento answers any other host with a 302 to it,
# so the image demanded to be reached under a hostname that does not resolve here.
# redirect_to_base=0 is what actually stops the bounce: it disables the base-URL
# comparison, after which the storefront serves whatever Host it is given
# (verified live — localhost, 127.0.0.1 and an invented domain all return the
# home page). base_url still has to name something reachable, because it is what
# generated links and static asset URLs point at.
#
# This is deliberately DB config, not app/etc/env.php. Magento hashes env.php's
# `system` section and compares it against the hash stored in the `flag` table on
# EVERY request, so a `system` block that varies per request — say, one computing
# base_url from $_SERVER['HTTP_HOST'] — recomputes a different hash each time and
# answers 500 with "The configuration file has changed". That failure mode was
# real, not hypothetical; DB config is not policed by that check.
for setting in \
  "web/unsecure/base_url http://localhost:7770/" \
  "web/secure/base_url http://localhost:7770/" \
  "web/url/redirect_to_base 0"
do
  # shellcheck disable=SC2086 # deliberate word split: path and value are separate args
  runuser -u app -- php bin/magento config:set $setting
done
runuser -u app -- php bin/magento cache:flush

# nginx is held back at runtime until entrypoint.sh knows HTTP_HOST/HTTP_PORT;
# the in-build validation still needs it.
svc_start_nginx
# svc_start returns at fork, not at bind. mariadb and elasticsearch above each
# wait for readiness; nginx did not, and run 31284811602 lost that race on both
# this image and shopping-admin — curl exited 7 before nginx was listening.
svc_wait_http http://127.0.0.1:7770/ nginx 60 /var/log/supervisor/nginx.log

echo "=== in-build validation ==="
# Deliberately 127.0.0.1 rather than the configured localhost base_url: this
# asserts the host-agnostic serving above, and would fail if the 302 came back.
#
# `|| true` on every capture below is load-bearing under `set -e`: a curl that
# cannot connect makes the ASSIGNMENT fail, killing the script with curl's own
# exit status before the check on the next line can report anything. That is
# what made run 31284811602 fail with a bare "exit code: 7" and no diagnosis.
# With it, curl writes 000 and the existing message says so.
code=$(curl -s -o /tmp/home.html -w '%{http_code}' http://127.0.0.1:7770/ || true)
[ "$code" = 200 ] || { echo "storefront returned $code" >&2; tail -n 50 "$MAGE"/var/log/*.log >&2 || true; exit 1; }
grep -q "One Stop Market" /tmp/home.html || { echo "storefront 200 but no 'One Stop Market'" >&2; exit 1; }
scode=$(curl -s -o /tmp/search.html -w '%{http_code}' 'http://127.0.0.1:7770/catalogsearch/result/?q=toothbrush' || true)
[ "$scode" = 200 ] && grep -q 'product-item-link' /tmp/search.html \
  || { echo "catalog search not working (code $scode)" >&2; exit 1; }
echo "storefront + search OK in-build"

echo "=== clean shutdown ==="
svc_stop nginx php-fpm
svc_stop elasticsearch redis
svc_stop mariadb
# A quiesced MariaDB matters: the shipped image must start from a cleanly
# shut-down datadir, not one that looks like a crash.
if pgrep -x mariadbd >/dev/null; then echo "mariadbd still running after stop" >&2; exit 1; fi
grep -q "Shutdown complete" /var/log/supervisor/mariadb.log \
  || { echo "no 'Shutdown complete' in mariadb log" >&2; tail -n 20 /var/log/supervisor/mariadb.log >&2; exit 1; }
svc_stop_all

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
# Shared implementation: builder/stage-lib/partition-tree.py, reached through
# the `stagelib` build context.
mkdir -p /staging
for i in $(seq 0 $((BUCKET_COUNT - 1))); do mkdir -p "$(printf '/staging/bucket-%02d' "$i")"; done
python3 /partition-tree.py "$BUCKET_LIMIT_KB" "$BUCKET_COUNT" "$MAGE" /staging

# The partitioner mirrors each recreated parent's owner and mode, so this is
# not repairing a loss — it is the same single-owner re-assertion ../../vwa/
# classifieds makes, cheap insurance on a tree that has exactly one owner.
chown -R app:app /staging

echo "restore stage complete"
