# syntax=docker/dockerfile:1
# WebArena "shopping-admin" (Magento 2 admin panel), rebuilt provenance-clear as
# a single all-in-one appliance — the same pattern as ../shopping (see that
# image's README.md for the full adjudication notes: markshust base, Mage-OS
# mirror, MariaDB/nginx/ES version deviations, why /opt/magento not
# /var/www/html). This instance's dataset is small (85M pub/media vs.
# shopping's 45G), so the final stage COPYs the restored /opt/magento tree
# directly instead of shopping's bucket-partitioning scheme.
#
# Base: the published image of markshust/docker-magento's images/php/8.1
# Dockerfile (MIT, vendored notes in vendor-markshust/) — same digest as
# ../shopping, for version parity.

FROM markoshust/magento-php@sha256:0f89dad43bde07034fe007eb02f0f24fcff98f98dacf7b1d900ac98425941a01 AS runtime
USER root

# All services beside php-fpm (which the base provides): nginx + redis (7.0) +
# mariadb (10.11) from Debian bookworm, Elasticsearch
# 7.17.9 from Elastic's own deb (bundled JDK), pinned by sha512 — same pins as
# ../shopping.
RUN apt-get update \
 && apt-get install -y --no-install-recommends nginx mariadb-server redis-server \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSLo /tmp/es.deb https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-7.17.9-amd64.deb \
 && echo "4516c3fdb5ab5f8ee18378fc7992d370c4a080d0f7e0cecd74fb31b7599016bdb692b40979d418f12929d7819f6576626c593dd3e8fff5f8f2d6129479b92eaa  /tmp/es.deb" | sha512sum -c - \
 && dpkg -i /tmp/es.deb \
 && rm /tmp/es.deb

COPY conf/nginx.conf /etc/nginx/nginx.conf
COPY conf/magento-site.conf /etc/nginx/conf.d/magento-site.conf
COPY conf/mariadb-admin.cnf /etc/mysql/mariadb.conf.d/99-shopping-admin.cnf
COPY conf/redis-admin.conf /etc/redis/redis-admin.conf
COPY conf/elasticsearch.yml /etc/elasticsearch/elasticsearch.yml
COPY conf/es-jvm-heap.options /etc/elasticsearch/jvm.options.d/heap.options
# The shared supervisor (#73) plus this image's service set. services.sh is
# sourced by the restore stage AND the entrypoint, so the boot path validated
# in-build is the one that ships.
COPY --from=stagelib run-services.sh /run-services.sh
COPY services.sh /services.sh

# /opt/magento, NOT /var/www/html: the base image declares VOLUME /var/www, and a
# tree there would be copied into a fresh anonymous volume on every `docker run`.
# The fpm master runs as root (unlike the base image's own
# entrypoint, which runs it as app): uncomment the pool's user/group, which a
# root master requires and which keep workers as app.
RUN sed -i 's/^;user = app/user = app/; s/^;group = app/group = app/' /usr/local/etc/php-fpm.conf \
 && grep -q '^user = app' /usr/local/etc/php-fpm.conf \
 && rm -f /etc/nginx/sites-enabled/default \
 && mkdir -p /opt/magento /var/log/supervisor /run/mysqld \
 && chown app:app /opt/magento \
 && chown mysql:mysql /run/mysqld \
 && chown -R elasticsearch:elasticsearch /etc/elasticsearch

EXPOSE 7780
# --chmod so the mode is a property of the build, not of the checkout: a COPY
# that inherited a non-executable file failed with "Permission denied" before
# any of the validation below could run.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
# HTTP_HOST/HTTP_PORT have to be validated before any
# service starts, and base_url written before nginx serves its first page.
CMD ["/entrypoint.sh"]

# MEASURED, not predicted: this container reached healthy in 15s, serving a
# real <title>Magento Admin</title> login page. The expected multi-minute
# first-request DI compile does not happen here — restore-stage.sh does skip
# setup:di:compile, but generated/code (Composer, Magento, PayPal, Psr,
# Symfony) arrives with the restored /opt/magento tree, so there is nothing
# left to compile. The 900s that used to sit in image.toml was sized for a
# compile this image never performs.
#
# 300s is ~20x the measured time, which absorbs a cold page cache on a CI
# runner while still failing a genuinely stuck container in about five minutes
# rather than a quarter of an hour. --timeout stays generous because Docker
# never overlaps probes, so a slow probe costs nothing when things are fine.
#
# ⚠️ The Host header is load-bearing, and it must be HTTP_HOST. Magento's
# adminhtml is pinned to the configured base_url — unlike the storefront, which
# serves any Host after redirect_to_base=0 — so a request whose Host is not the
# configured one falls through to the frontend area and 404s. Measured
# in-container against this image with HTTP_HOST=depot.example.com:
#     Host=127.0.0.1           -> 404  (falls through to the frontend area)
#     Host=depot.example.com   -> 200
# Connect over the loopback (nginx's listen port is fixed at 7780 regardless of
# HTTP_PORT) but present the name the app was configured for. Naming a literal
# here would only ever pass under the one HTTP_HOST that happened to match it.
#
# The fallthrough returns a real 404, so `curl -f` fails loudly on it rather
# than passing on an empty page — this check is therefore also, incidentally,
# proof that the entrypoint's base_url write took effect.
#
# And no -L: under the right Host this answers 200 directly, so there is no
# redirect worth following, and following one would only ever lead to the
# PUBLISHED address — which the container is not required to be able to
# resolve. Readiness must not depend on that.
HEALTHCHECK --start-period=300s --interval=15s --timeout=120s --retries=3 \
  CMD curl -fsS --max-time 115 -H "Host: ${HTTP_HOST}" \
      http://127.0.0.1:7780/admin >/dev/null || exit 1

# --- Magento 2.4.6 code tree, deterministically from the committed lockfile ---
FROM runtime AS code
USER app
WORKDIR /opt/magento
COPY --chown=app:app composer.json composer.lock ./
# composer install (not create-project): the magento/magento2-base component in
# the lock deploys the full app skeleton (bin/magento, app/, pub/, setup/, the
# nginx.conf.sample) into the project root via magento-composer-installer.
# Every package URL in the lock points at the keyless mirror.mage-os.org.
# Same composer.lock as ../shopping — version parity (D-task #52 handles
# upgrades later).
RUN composer install --no-interaction --no-progress \
 && composer clear-cache \
 && test -f bin/magento && test -f nginx.conf.sample \
 && ln -s nginx.conf.sample nginx.conf
COPY --chown=app:app conf/env.php app/etc/env.php

# --- Restore: boot the appliance stack, load the derived data, reindex, quiesce ---
FROM code AS restore
USER root
COPY restore-stage.sh /restore-stage.sh
RUN --mount=type=bind,from=datasets,source=shopping_admin_db.sql.gz,target=/mnt/shopping_admin_db.sql.gz \
    --mount=type=bind,from=datasets,source=shopping_admin_media.tar,target=/mnt/shopping_admin_media.tar \
    bash /restore-stage.sh

# --- Ownership audit: its own layer, so a failure doesn't re-run the ~restore ---
# Lesson from ../shopping's build failures (build2: debian-*.flag, build4:
# mysql_upgrade_info, both root-owned maintenance files in /var/lib/mysql):
# the audit lived inside the restore RUN there, so an audit failure re-ran the
# entire restore stage. Splitting it into its own layer means a future audit
# failure only re-runs this cheap check, not the restore.
FROM restore AS audited
COPY audit-ownership.sh /audit-ownership.sh
RUN bash /audit-ownership.sh

# --- Final image: runtime + restored state only (never the restore scaffolding) ---
FROM runtime
# COPY cannot delete: merge the restored datadirs onto clean dirs, or files the
# restore removed/rewrote (ib logfiles, binlogs) would resurface from the
# runtime layer and corrupt the datadir.
RUN rm -rf /var/lib/mysql /var/lib/elasticsearch \
 && mkdir /var/lib/mysql /var/lib/elasticsearch \
 && chown mysql:mysql /var/lib/mysql \
 && chown elasticsearch:elasticsearch /var/lib/elasticsearch
COPY --from=audited /var/lib/mysql/ /var/lib/mysql/
COPY --from=audited /var/lib/elasticsearch/ /var/lib/elasticsearch/
# This dataset's /opt/magento (Magento code ~2-3G + 85M media) is comfortably
# under GHCR's ~10G layer ceiling, unlike ../shopping's 45G media tree — so a
# single COPY suffices here; no bucket-partitioning stage needed.
COPY --from=audited /opt/magento/ /opt/magento/

LABEL org.opencontainers.image.source="https://github.com/shkolnik/browser-use-benchmarks"
