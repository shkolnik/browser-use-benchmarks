#!/bin/bash
# Runs inside the "restore" build stage of the shopping-admin Dockerfile.
# Boots the shipped supervisord stack, loads the derived DB dump + media +
# synthesizes config.php, reindexes search, validates the admin panel and DB
# row counts in-build, shuts everything down cleanly (quiesced mariadb), trims
# junk. The ownership audit runs in the Dockerfile's own following stage/layer
# (../shopping's build lost two builds to an audit failure re-running the
# whole ~11-minute restore because the audit lived in this same RUN — see
# SPEC.md "Lesson to apply").
set -euo pipefail

MAGE=/opt/magento

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
  [ "$i" = 120 ] && { echo "elasticsearch never came up" >&2; tail -n 50 /var/log/supervisor/elasticsearch.log >&2 || true; exit 1; }
  sleep 2
done
echo "mariadb + elasticsearch up"

echo "=== load DB (DEFINER-stripped, as in shopping) ==="
mariadb <<'SQL'
CREATE DATABASE magentodb;
CREATE USER 'magentouser'@'localhost' IDENTIFIED BY 'MyPassword';
CREATE USER 'magentouser'@'127.0.0.1' IDENTIFIED BY 'MyPassword';
GRANT ALL PRIVILEGES ON magentodb.* TO 'magentouser'@'localhost';
GRANT ALL PRIVILEGES ON magentodb.* TO 'magentouser'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
zcat /mnt/shopping_admin_db.sql.gz | sed 's/DEFINER=`[^`]*`@`[^`]*`//g' | mariadb magentodb
# Measured counts (SPEC.md) — a real regression pin, not a tautology.
products=$(mariadb -N -e 'SELECT COUNT(*) FROM magentodb.catalog_product_entity')
[ "$products" = 2040 ] || { echo "product count $products != 2040 — bad DB load" >&2; exit 1; }
orders=$(mariadb -N -e 'SELECT COUNT(*) FROM magentodb.sales_order')
[ "$orders" = 308 ] || { echo "sales_order count $orders != 308 — bad DB load" >&2; exit 1; }
echo "DB loaded: $products products, $orders sales_order rows"

echo "=== media ==="
tar xf /mnt/shopping_admin_media.tar -C "$MAGE/pub"
chown -R app:app "$MAGE/pub/media"
test -d "$MAGE/pub/media/catalog/product"

echo "=== synthesize config.php + reindex (NEVER setup:upgrade/di:compile: ==="
echo "=== core setup_module rows are absent in this dataset) ==="
cd "$MAGE"
runuser -u app -- php bin/magento module:enable --all
runuser -u app -- php bin/magento app:config:import -n
test -f app/etc/config.php
runuser -u app -- php bin/magento indexer:reindex

# Rebase the restored dataset off CMU's deployment — see ../shopping/restore-stage.sh
# for the full reasoning, including why this is DB config and NOT app/etc/env.php
# (a per-request-varying `system` block fails Magento's config-hash check with a
# 500 on every request; that was measured, not theorised).
#
# One honest limitation, measured on this image: redirect_to_base=0 makes the
# STOREFRONT serve any Host, but the ADMIN panel remains reachable only under the
# host named in base_url — every other Host falls through to the frontend area and
# renders its 404 instead of the login form. So the admin panel is pinned to
# localhost:7780 here, which is the port compose publishes and the URL
# image.toml's healthcheck uses. Making adminhtml host-agnostic is unsolved.
for setting in \
  "web/unsecure/base_url http://localhost:7780/" \
  "web/secure/base_url http://localhost:7780/" \
  "web/url/redirect_to_base 0"
do
  # shellcheck disable=SC2086 # deliberate word split: path and value are separate args
  runuser -u app -- php bin/magento config:set $setting
done
runuser -u app -- php bin/magento cache:flush

# nginx is autostart=false (entrypoint.sh starts it at runtime once
# HTTP_HOST/HTTP_PORT are known); the in-build validation still needs it.
sctl start nginx

echo "=== in-build validation ==="
# localhost, not 127.0.0.1: per the note above, adminhtml answers only under the
# host in base_url. The storefront check below is the one that proves the redirect
# to CMU is gone.
code=$(curl -s -o /tmp/admin.html -w '%{http_code}' http://localhost:7780/admin)
[ "$code" = 200 ] || { echo "admin panel returned $code" >&2; tail -n 50 "$MAGE"/var/log/*.log >&2 || true; exit 1; }
# Any Host must reach the app rather than being bounced to CMU. The storefront is
# the surface that can prove it, since adminhtml is host-pinned.
scode=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7780/)
[ "$scode" = 200 ] || { echo "storefront on 127.0.0.1 returned $scode (host-agnostic serving broken)" >&2; exit 1; }
# name="login[username]" is Magento core's admin login field (stable across
# 2.x — referenced by the form's own JS validation), a more reliable marker
# than any theme-rendered copy text.
grep -q 'name="login\[username\]"' /tmp/admin.html \
  || { echo "admin panel 200 but no login-form marker" >&2; head -c 2000 /tmp/admin.html >&2; exit 1; }
# Elasticsearch really indexed the catalog (mirrors shopping's catalogsearch
# assertion, which requires an unauthenticated storefront page the admin
# panel doesn't have): query the reindexed count directly.
escount=$(curl -s 'http://127.0.0.1:9200/_cat/count/magento2*?h=count' | tr -d '[:space:]')
[ -n "$escount" ] && [ "$escount" -gt 0 ] 2>/dev/null || { echo "elasticsearch index empty (count='$escount')" >&2; curl -s 'http://127.0.0.1:9200/_cat/indices?v' >&2 || true; exit 1; }
echo "admin panel + DB counts + elasticsearch OK in-build ($escount ES docs)"

echo "=== clean shutdown ==="
sctl stop nginx php-fpm
sctl stop elasticsearch redis
sctl stop mariadb
# A quiesced MariaDB matters: the shipped image must start from a cleanly
# shut-down datadir, not one that looks like a crash.
if pgrep -x mariadbd >/dev/null; then echo "mariadbd still running after stop" >&2; exit 1; fi
grep -q "Shutdown complete" /var/log/supervisor/mariadb.log \
  || { echo "no 'Shutdown complete' in mariadb log" >&2; tail -n 20 /var/log/supervisor/mariadb.log >&2; exit 1; }
sctl shutdown || true
wait "$SUP_PID" 2>/dev/null || true

echo "=== trim state not worth shipping ==="
rm -rf "$MAGE"/var/log/* "$MAGE"/var/cache/* "$MAGE"/var/page_cache/* \
       "$MAGE"/var/session/* "$MAGE"/var/report/* "$MAGE"/var/tmp/* /tmp/admin.html
rm -rf /var/log/supervisor/* /var/log/nginx/* /var/log/elasticsearch/* /var/log/mysql/* /run/mysqld/*
# Redis persistence is off (see redis-admin.conf), so no dump.rdb should exist
# anywhere — the gitlab image's root-owned-dump defect class. Verify.
found=$(find / -xdev -name 'dump.rdb' 2>/dev/null || true)
[ -z "$found" ] && : || { echo "unexpected redis dump.rdb: $found" >&2; exit 1; }

echo "restore stage complete"
