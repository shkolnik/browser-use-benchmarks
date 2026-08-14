# syntax=docker/dockerfile:1
# WebArena "shopping" (Magento 2 One Stop Market), rebuilt provenance-clear as a
# single all-in-one appliance: every byte is either (a) the markshust/docker-magento
# community PHP image (pinned by digest), (b) Debian/MariaDB/Elastic packages
# installed below, (c) Magento 2.4.6 from the keyless Mage-OS mirror via the
# committed composer.lock, or (d) our derived data artifacts (db dump, media,
# env.php — see derive-backup.sh), restored in a build stage that boots the very
# same service stack the image ships (services.sh, sourced by both). The
# upstream 67G docker-save tar contributes only *data*, never image layers. See README.md for adjudication
# notes (mariadb 10.11, /opt/magento, ES deb).
#
# Base: the published image of markshust/docker-magento's images/php/8.1
# Dockerfile (MIT, vendored notes in vendor-markshust/) — PHP 8.1-fpm on Debian
# bookworm with every Magento extension, proven live against this dataset in the
# shopping-community-base experiment.

FROM markoshust/magento-php@sha256:4b10bfa3cd2586d4e20defbb6789dfa75d30346cd1b93da22a6c9f18c35d4185 AS runtime
USER root

# All services beside php-fpm (which the base provides): nginx + redis (7.0) +
# mariadb (10.11) from Debian bookworm, Elasticsearch
# 7.17.9 from Elastic's own deb (bundled JDK), pinned by sha512.
RUN apt-get update \
 && apt-get install -y --no-install-recommends nginx mariadb-server redis-server \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSLo /tmp/es.deb https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-7.17.9-amd64.deb \
 && echo "4516c3fdb5ab5f8ee18378fc7992d370c4a080d0f7e0cecd74fb31b7599016bdb692b40979d418f12929d7819f6576626c593dd3e8fff5f8f2d6129479b92eaa  /tmp/es.deb" | sha512sum -c - \
 && dpkg -i /tmp/es.deb \
 && rm /tmp/es.deb

COPY conf/nginx.conf /etc/nginx/nginx.conf
COPY conf/magento-site.conf /etc/nginx/conf.d/magento-site.conf
COPY conf/mariadb-shopping.cnf /etc/mysql/mariadb.conf.d/99-shopping.cnf
COPY conf/redis-shopping.conf /etc/redis/redis-shopping.conf
COPY conf/elasticsearch.yml /etc/elasticsearch/elasticsearch.yml
COPY conf/es-jvm-heap.options /etc/elasticsearch/jvm.options.d/heap.options
# The shared supervisor (#73) plus this image's service set. services.sh is
# sourced by the restore stage AND the entrypoint, so the boot path validated
# in-build is the one that ships.
COPY --from=stagelib run-services.sh /run-services.sh
COPY services.sh /services.sh

# /opt/magento, NOT /var/www/html: the base image declares VOLUME /var/www, and a
# tree there would be copied into a fresh 48G anonymous volume on every
# `docker run`. Outside the volume path the layers are served in place.
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

EXPOSE 7770
# --chmod so the mode is a property of the build, not of the checkout: a COPY
# that inherited a non-executable file failed with "Permission denied" before
# any of the validation below could run.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
# HTTP_HOST/HTTP_PORT have to be validated before any
# service starts, and base_url written before nginx serves its first page.
CMD ["/entrypoint.sh"]

# 900s is deliberately conservative, and UNVERIFIED for this image — it cannot
# be built on the dev host (datasets/shopping_media.tar is absent), so nothing
# here has been measured against a running container.
#
# What IS established: restore-stage.sh never runs setup:upgrade/di:compile, so
# any generated/ classes in the image arrive with the restored /opt/magento
# tree rather than from a compile. On ../shopping-admin, which restores the
# same way, generated/code IS present and the container reached healthy in 15s
# — so the feared multi-minute first-request compile may not happen here
# either. Left large until someone can measure it: an over-long start period
# only delays detecting a stuck container, while too short a one fails a
# healthy image outright.
#
# --timeout is the decisive parameter, not the total budget: a probe that gives
# up after 10s against a first request that takes minutes can never succeed at
# ANY number of retries. Docker never overlaps probes, so a long timeout is
# safe. Failures during the start period do not count toward --retries, and a
# success during it marks the container healthy immediately.
#
# No -L, and no Host header either. Unlike the admin next door, the storefront
# is not pinned to base_url — redirect_to_base=0 makes it serve any Host — so
# this answers the loopback directly: measured 200, 171,694 bytes, no redirect.
# There is nothing to follow, which is why the container stayed healthy on the
# deployment where the admin and classifieds did not. A healthcheck answers "is
# this container serving?" and must not depend on DNS, the proxy, or anything
# else outside the container.
HEALTHCHECK --start-period=900s --interval=15s --timeout=120s --retries=3 \
  CMD curl -fsS --max-time 115 http://localhost:7770/ >/dev/null || exit 1

# --- Magento 2.4.6 code tree, deterministically from the committed lockfile ---
FROM runtime AS code
USER app
WORKDIR /opt/magento
COPY --chown=app:app composer.json composer.lock ./
# composer install (not create-project): the magento/magento2-base component in
# the lock deploys the full app skeleton (bin/magento, app/, pub/, setup/, the
# nginx.conf.sample) into the project root via magento-composer-installer.
# Every package URL in the lock points at the keyless mirror.mage-os.org.
RUN composer install --no-interaction --no-progress \
 && composer clear-cache \
 && test -f bin/magento && test -f nginx.conf.sample \
 && ln -s nginx.conf.sample nginx.conf
COPY --chown=app:app conf/env.php app/etc/env.php

# --- Restore: boot the appliance stack, load the derived data, reindex, quiesce ---
FROM code AS restore
USER root
COPY --from=stagelib partition-tree.py /partition-tree.py
COPY restore-stage.sh /restore-stage.sh
# Only the DB dump is mounted: this stage reads no media file (see [media] in
# image.toml), so shopping_media.tar is not bound here.
RUN --mount=type=bind,from=datasets,source=shopping_db.sql.gz,target=/mnt/shopping_db.sql.gz \
    bash /restore-stage.sh

# --- Final image: runtime + restored state only (never the restore scaffolding) ---
FROM runtime
# COPY cannot delete: merge the restored datadirs onto clean dirs, or files the
# restore removed/rewrote (ib logfiles, binlogs) would resurface from the
# runtime layer and corrupt the datadir.
RUN rm -rf /var/lib/mysql /var/lib/elasticsearch \
 && mkdir /var/lib/mysql /var/lib/elasticsearch \
 && chown mysql:mysql /var/lib/mysql \
 && chown elasticsearch:elasticsearch /var/lib/elasticsearch
COPY --from=restore /var/lib/mysql/ /var/lib/mysql/
COPY --from=restore /var/lib/elasticsearch/ /var/lib/elasticsearch/
# One layer per staging bucket keeps every pushed layer under ~10G (GHCR's
# comfortable ceiling). Bucket count must match BUCKET_COUNT in restore-stage.sh.
# These carry the code tree only; the last few are expected to be empty, since
# BUCKET_COUNT is a ceiling with headroom and a COPY of an empty bucket is a
# no-op.
COPY --from=restore /staging/bucket-00/ /opt/magento/
COPY --from=restore /staging/bucket-01/ /opt/magento/
COPY --from=restore /staging/bucket-02/ /opt/magento/
COPY --from=restore /staging/bucket-03/ /opt/magento/

# --- Media: 45G of pub/media, bucketed on the CI host, never in a build stage ---
# builder/docker.py writes these into .media/, a gitignored subdir of this image
# dir — which is the default build context, and the only context ADD can extract
# from (it has no --from).
#
# Count must match [media].max_buckets in image.toml. Unused buckets carry only
# directory entries and extract to nothing.
ADD --chown=app:app .media/bucket-00.tar /opt/magento/pub/media/
ADD --chown=app:app .media/bucket-01.tar /opt/magento/pub/media/
ADD --chown=app:app .media/bucket-02.tar /opt/magento/pub/media/
ADD --chown=app:app .media/bucket-03.tar /opt/magento/pub/media/
ADD --chown=app:app .media/bucket-04.tar /opt/magento/pub/media/
ADD --chown=app:app .media/bucket-05.tar /opt/magento/pub/media/
ADD --chown=app:app .media/bucket-06.tar /opt/magento/pub/media/
ADD --chown=app:app .media/bucket-07.tar /opt/magento/pub/media/
ADD --chown=app:app .media/bucket-08.tar /opt/magento/pub/media/
ADD --chown=app:app .media/bucket-09.tar /opt/magento/pub/media/

# Covers the media subtree in place of the restore stage's `find $MAGE ! -user
# app`, and checks DIRECTORIES on purpose — both silent failures here leave a
# root-owned directory while every file stays app-owned:
#
#   1. `ADD --chown=<name>` does not fail the build when the name does not
#      resolve; it lands everything 0:0 root:root and exits clean.
#   2. `--chown` applies to tar MEMBERS, so a directory existing only implicitly
#      in a member's path is created by the extractor and never chowned.
#      bucket-media.py emits every ancestor explicitly to prevent this.
#
# php-fpm runs as app and writes resized-image caches under
# pub/media/catalog/product/cache/, so a root-owned directory here is a runtime
# failure the HTML-grepping smoke gate cannot see. -quit stops at the first
# offender; files are never stat'd.
RUN bad="$(find /opt/magento/pub/media -type d ! -user app -print -quit)"; \
    if [ -n "$bad" ]; then echo "root-owned media dir: $bad" >&2; exit 1; fi

LABEL org.opencontainers.image.source="https://github.com/shkolnik/browser-use-benchmarks"
