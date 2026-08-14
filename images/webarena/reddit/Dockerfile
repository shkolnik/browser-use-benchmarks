# syntax=docker/dockerfile:1
# Rebuilds the WebArena reddit benchmark image (Postmill) from provenance-clear
# parts: Postmill's own source at a pinned commit, built with Postmill's own
# multi-stage recipe, plus the populated data derived from the upstream
# docker-save tar by derive-backup.sh (image.toml [prepare]).
#
# Why this is a rebuild and not a re-tag: the upstream tar's top four layers are
# hand `docker commit`s whose history entries read literally
# `supervisord -n -j /supervisord.pid`. Nothing records how the code got in.
# Here every executable byte traces to a published commit, and only rows and
# uploaded images come from upstream.
#
# The stage names and build steps below follow Postmill's own Dockerfile (found
# at /var/www/html/Dockerfile inside the upstream image, byte-identical to the
# pinned commit's). Deviations from it are individually justified in place;
# there are three, and they all come from collapsing upstream's multi-container
# compose project into the single appliance this fleet ships.

ARG PHP_VERSION=8.1
ARG NODE_VERSION=14
ARG COMPOSER_VERSION=2
ARG SRC=Postmill-a6a9bbc86f44.tar.gz
ARG SRC_COMMIT=a6a9bbc86f44

# ======
# Assets — upstream's postmill_assets stage
# ======
FROM node:${NODE_VERSION}-alpine AS assets
ARG SRC
WORKDIR /app
COPY --from=datasets ${SRC} /tmp/src.tar.gz
RUN set -eux; \
    tar xzf /tmp/src.tar.gz --strip-components=1 -C /app; \
    rm /tmp/src.tar.gz; \
    apk add --no-cache curl; \
    yarn; \
    yarn run build-prod; \
    yarn cache clean; \
    rm -rf node_modules

# ========
# Composer — upstream's postmill_composer_cache stage
# ========
FROM composer:${COMPOSER_VERSION} AS composer_cache
ARG SRC
WORKDIR /app
COPY --from=datasets ${SRC} /tmp/src.tar.gz
RUN set -eux; \
    tar xzf /tmp/src.tar.gz --strip-components=1 -C /app; \
    rm /tmp/src.tar.gz; \
    # Held rather than streamed: composer emits a line per package, and a burst
    # that size overruns the Actions live-log channel — the dropped flush takes
    # the surrounding build's live output with it until the burst ends. The
    # lines are the diagnostic only on failure, which is where they are printed.
    composer install \
        --ignore-platform-reqs \
        --no-autoloader \
        --no-cache \
        --no-dev \
        --no-progress \
        --no-scripts \
        --prefer-dist >/tmp/composer.log 2>&1 \
      || { cat /tmp/composer.log >&2; exit 1; }; \
    rm -f /tmp/composer.log

# ==============
# Runtime base — upstream's postmill_php_base, plus the services that upstream
# ran as separate compose containers.
#
# ⚠️ DEVIATION 1 (base tag): upstream uses `php:8.1-fpm-alpine`, which today
# resolves to alpine 3.21 — and alpine 3.21 has NO postgresql14 package at all
# (only 15 and 17). Since the measured upstream server is postgres 14.7, and
# version parity outranks image freshness in this program, the base is pinned
# to the alpine 3.16 variant, which carries postgresql14 (14.12) and nginx
# 1.22.1. Verified by running, not assumed. Revisit under the
# security-upgrade pass, where moving off an EOL alpine is the whole point.
# ==============
FROM php:${PHP_VERSION}-fpm-alpine3.16 AS runtime

# Pinned by digest, not by the floating :latest tag. A runner holding a stale
# cached :latest failed this build with `No releases available for package
# "pecl.php.net/apcu"` — old installer versions resolve extensions through
# pecl.php.net, which has since been retired. A digest also makes this the only
# third-party binary in the image traceable to one exact artifact.
COPY --from=mlocati/php-extension-installer@sha256:b6d3fa381b9ba5cf051117c1c601d6a523b590e534bf3d56eb4fbe352949c138 /usr/bin/install-php-extensions /usr/bin

RUN set -eux; \
    IPE_GD_WITHOUTAVIF=1 install-php-extensions \
        amqp \
        apcu \
        gd \
        intl \
        opcache \
        pdo_pgsql \
        zip \
    ; \
    apk add --no-cache \
        acl \
        su-exec \
        bash \
        nginx \
        postgresql14 \
        postgresql14-client \
    ; \
    # ⚠️ DEVIATION 2 (a real upstream bug, not a preference): upstream's line is
    #   { echo 'apc.enable_cli = On'; echo 'memory_limit = 192m'; } \
    #       >> 'apc.enable_cli = On' >> "$PHP_INI_DIR/conf.d/zz-postmill.ini";
    # The stray middle redirect writes a FILE named `apc.enable_cli = On` into
    # the working directory and sends the settings there, so the ini upstream
    # meant to write is only partly written. Reproducing a typo verbatim is not
    # parity, it is cargo cult; the intended two settings are written here.
    { \
        echo 'apc.enable_cli = On'; \
        echo 'memory_limit = 192m'; \
    } >> "$PHP_INI_DIR/conf.d/zz-postmill.ini"

COPY --from=composer_cache /usr/bin/composer /usr/bin

ENV COMPOSER_ALLOW_SUPERUSER=1 \
    COMPOSER_HTACCESS_PROTECT=0 \
    COMPOSER_HOME="/tmp" \
    COMPOSER_MEMORY_LIMIT=-1 \
    SU_USER=www-data

WORKDIR /app

# ==========
# App — upstream's postmill_php (prod) stage.
#
# ⚠️ DEVIATION 3 (APP_ENV): the upstream WebArena image runs APP_ENV=dev, which
# in Symfony renders the web debug toolbar into every page. That toolbar is DOM
# an agent benchmark would have to look at, and it is a development artifact
# nobody deploys on purpose — upstream's own Dockerfile builds a prod stage for
# exactly this reason. Built prod here, and called out because it is a visible
# difference from the image being replaced: a task-level A/B against the
# upstream container is the check that this changed no benchmark-relevant page.
# ==========
FROM runtime AS app
ARG SRC
ARG SRC_COMMIT
COPY --from=datasets ${SRC} /tmp/src.tar.gz
RUN set -eux; \
    tar xzf /tmp/src.tar.gz --strip-components=1 -C /app; \
    rm /tmp/src.tar.gz; \
    rm -rf /app/node_modules /app/Dockerfile /app/docker-compose.yml

COPY --from=composer_cache /app/vendor /app/vendor
COPY --from=assets /app/public/build /app/public/build

ENV APP_ENV=prod \
    APP_SECRET="change this in prod or die" \
    DATABASE_URL='pgsql://postmill:secret@127.0.0.1:5432/postmill?serverVersion=14' \
    LOG_FILE='php://stderr'

RUN set -eux; \
    apk add --no-cache --virtual .build-deps git; \
    { \
        echo 'opcache.max_accelerated_files = 20000'; \
        echo 'opcache.validate_timestamps = Off'; \
        echo 'realpath_cache_size = 4096K'; \
        echo 'realpath_cache_ttl = 600'; \
    } >> "$PHP_INI_DIR/conf.d/zz-postmill.ini"; \
    cp "$PHP_INI_DIR/php.ini-production" "$PHP_INI_DIR/php.ini"; \
    # Order here is upstream's and is load-bearing, which I learned by breaking
    # it: `composer install` runs Symfony's cache:clear, and cache:clear
    # resolves every env var the config references — including APP_BRANCH.
    # Those two entries in .env are `$(git ...)` snippets that never expand
    # (there is no .git), but they do exist as literal strings, which is enough
    # to satisfy resolution. Stripping them BEFORE the install instead of after
    # kills the build with `Environment variable not found: "APP_BRANCH"`.
    # After the install the two entries are rewritten to real values rather
    # than DELETED, because dump-env bakes .env.local.php from whatever .env
    # still holds and only baked names survive to runtime. Deleting them and
    # re-declaring ENV in this stage does NOT work: the final stage is
    # `FROM runtime`, so it inherits no ENV from here, and php-fpm's default
    # `clear_env = yes` would wipe them from the workers even if it did. A
    # booted image proved it — every page 500'd with
    # `Environment variable not found: "APP_BRANCH"`.
    #
    # Held rather than streamed, as in the composer_cache stage: the per-package
    # burst overruns the Actions live-log channel and blanks the build's live
    # output until it ends. Printed on failure, where it is the diagnostic.
    composer install \
        --apcu-autoloader \
        --classmap-authoritative \
        --no-dev \
        --prefer-dist >/tmp/composer.log 2>&1 \
      || { cat /tmp/composer.log >&2; exit 1; }; \
    rm -f /tmp/composer.log; \
    sed -i "s|^APP_BRANCH=.*|APP_BRANCH=|; s|^APP_VERSION=.*|APP_VERSION=$SRC_COMMIT|" .env; \
    composer dump-env prod; \
    composer clear-cache; \
    apk del --no-network .build-deps; \
    mkdir -p /app/var /app/public/media/cache /app/public/submission_images; \
    chown -R www-data:www-data /app/var /app/public

# =======
# Restore — load the derived data into a build-time postgres cluster.
# =======
FROM app AS restore
COPY --from=datasets reddit_db.sql.gz /tmp/reddit_db.sql.gz
# reddit_media.tar is deliberately absent: this stage reads no uploaded image
# (see [media] in image.toml), so the archive never enters a build stage.
COPY --from=stagelib partition-tree.py /partition-tree.py
COPY restore-stage.sh /restore-stage.sh
RUN /bin/sh /restore-stage.sh

# =======
# Audit — its own layer so a failed assertion does not re-run the restore.
# =======
FROM restore AS audited
COPY audit.sh /audit.sh
RUN /bin/sh /audit.sh

# =====
# Final — carries the restored cluster and app tree, but none of the dump or
# media tarballs that produced them.
# =====
FROM runtime
# The code tree, one layer per staging bucket. It fits a single bucket now that
# the uploaded images arrive by the media path below; the second is headroom and
# a COPY of an empty bucket is a no-op. Count must match BUCKET_COUNT in
# restore-stage.sh.
COPY --from=audited /staging/bucket-00/ /app/
COPY --from=audited /staging/bucket-01/ /app/
COPY --from=audited /var/lib/postgresql/data /var/lib/postgresql/data

# --- Uploaded images: 38.5G bucketed on the CI host, never in a build stage ---
# builder/docker.py writes these into .media/, a gitignored subdir of this image
# dir — which is the default build context, and the only context ADD can extract
# from (it has no --from). The archive carries both `media/` and
# `submission_images/`, so the target is their shared parent.
#
# Count must match [media].max_buckets in image.toml. Unused buckets carry only
# directory entries and extract to nothing.
ADD --chown=www-data:www-data .media/bucket-00.tar /app/public/
ADD --chown=www-data:www-data .media/bucket-01.tar /app/public/
ADD --chown=www-data:www-data .media/bucket-02.tar /app/public/
ADD --chown=www-data:www-data .media/bucket-03.tar /app/public/
ADD --chown=www-data:www-data .media/bucket-04.tar /app/public/
ADD --chown=www-data:www-data .media/bucket-05.tar /app/public/
ADD --chown=www-data:www-data .media/bucket-06.tar /app/public/
ADD --chown=www-data:www-data .media/bucket-07.tar /app/public/

# Checks DIRECTORIES on purpose — both silent failures here leave a root-owned
# directory while every file stays www-data-owned:
#
#   1. `ADD --chown=<name>` does not fail the build when the name does not
#      resolve; it lands everything 0:0 root:root and exits clean.
#   2. `--chown` applies to tar MEMBERS, so a directory existing only implicitly
#      in a member's path is created by the extractor and never chowned.
#      The host-side bucketing emits every ancestor explicitly to prevent this.
#
# php-fpm runs as www-data and writes thumbnails under public/media/cache, so a
# root-owned directory here is a runtime failure no build-time assertion sees.
# -quit stops at the first offender; files are never stat'd.
RUN bad="$(find /app/public -type d ! -user www-data -print -quit)"; \
    if [ -n "$bad" ]; then echo "root-owned media dir: $bad" >&2; exit 1; fi
COPY nginx.conf /etc/nginx/http.d/default.conf
# The shared supervisor (#73): any service that exits fails the container.
COPY --from=stagelib run-services.sh /run-services.sh

ENV APP_ENV=prod \
    APP_SECRET="change this in prod or die" \
    DATABASE_URL='pgsql://postmill:secret@127.0.0.1:5432/postmill?serverVersion=14' \
    LOG_FILE='php://stderr'

RUN set -eux; \
    cp "$PHP_INI_DIR/php.ini-production" "$PHP_INI_DIR/php.ini"; \
    chown -R postgres:postgres /var/lib/postgresql/data; \
    chmod 0700 /var/lib/postgresql/data; \
    mkdir -p /run/postgresql /run/nginx; \
    chown postgres:postgres /run/postgresql

EXPOSE 80
# --chmod so the mode is a property of the BUILD, not of the checkout.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
CMD ["/entrypoint.sh"]

# ⚠️ localhost:80, NOT 9999. The healthcheck runs INSIDE the container, so it
# uses the listen port; 9999 is the published side and nothing is bound to it in
# here. This is the only image in the fleet where the two differ, which makes it
# the easiest one to get wrong by copying image.toml's URL.
#
# /forums, not /: a document root nested one level too deep still answers / and
# 404s every real page — the exact defect that shipped once already.
# Postgres + php-fpm + nginx come up behind the entrypoint's supervisor, hence
# the start period.
#
# No -L: Postmill serves /forums as a 200 whatever Host it is asked under
# (measured: 33,671 bytes, no redirect), so there is nothing to follow. A
# healthcheck answers "is this container serving?", which must not depend on
# DNS, the proxy, or anything else outside the container.
HEALTHCHECK --start-period=90s --interval=10s --timeout=15s --retries=3 \
  CMD curl -fsS --max-time 14 http://localhost:80/forums >/dev/null || exit 1
