# syntax=docker/dockerfile:1
# Rebuilds the VWA classifieds benchmark image (Osclass 8.1.2) from
# provenance-clear parts: Osclass's own release zip, plus a committed patch
# carrying the seven changes WebArena made to it, plus the seed dump from the
# archive.org compose zip and the item photos derived from the upstream image
# by derive-backup.sh (image.toml [prepare]).
#
# Unlike ../../webarena/reddit, the upstream image here has a complete buildkit
# history — but a readable history is not a reproducible source, and the tag
# this rebuild was originally supposed to use does not exist. See README.md for
# how the real origin was found and for the byte-for-byte reconstruction that
# validates the patch; app-tree.sha256 is the manifest the audit stage re-checks.
#
# The build steps below follow the upstream image's own history (gd with
# freetype/jpeg, then mysqli) plus the services upstream ran as a second compose
# container. Deviations are individually justified in place; there are three.

# ⚠️ Pinned by digest, not by the tag. The tag is what upstream's history
# records; the digest is what makes this build repeatable.
FROM php:8.1.27-cli@sha256:3ffa0f8fdef6a56e1da293e092b6adb3574de88e4181bb901d454366ef5dda82 AS runtime

# ⚠️ DEVIATION 1 (database server): upstream's compose db is `mysql:8.1`, which
# cannot be installed — 8.1 was an innovation release, and Oracle's apt repo for
# bookworm today publishes only mysql-8.0, mysql-8.4-lts, mysql-9.7-lts and
# mysql-innovation (read off the live Release file). 8.0 is OLDER than upstream
# and already past EOL, so the choice is 8.4 LTS: the maintained successor to
# the line 8.1 sat in. Checked by running rather than argued — the seed dump
# restored into mysql:8.4 yields identical counts on all seven validation pins,
# and it is plain InnoDB/MyISAM utf8mb3 with no routines, views, triggers or
# MySQL-8-only collations. James's call, 2026-08-06: the database version is the
# right place to be pragmatic rather than literal.
ARG MYSQL_VERSION=8.4.11-1debian12
# The key is pinned by FINGERPRINT, not by the file's hash: Oracle re-issues the
# same key with a later expiry (the 2023 file expired 2025-10-22 and would make
# apt refuse the repo), which changes the bytes while the identity holds.
ARG MYSQL_KEY_FPR=BCA43417C3B485DD128EC6D4B7B3B788A8D3785C

RUN set -eux; \
    savedAptMark="$(apt-mark showmanual)"; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libfreetype6-dev libjpeg62-turbo-dev libpng-dev \
        ca-certificates curl gnupg unzip patch python3; \
    mkdir -p /etc/apt/keyrings; \
    docker-php-ext-configure gd --with-freetype --with-jpeg; \
    docker-php-ext-install -j"$(nproc)" gd mysqli; \
    \
    curl -fsSL https://repo.mysql.com/RPM-GPG-KEY-mysql-2025 -o /tmp/mysql.asc; \
    gpg --batch --import /tmp/mysql.asc; \
    gpg --batch --list-keys --with-colons "$MYSQL_KEY_FPR" > /dev/null; \
    gpg --batch --export "$MYSQL_KEY_FPR" > /etc/apt/keyrings/mysql.gpg; \
    rm /tmp/mysql.asc; \
    echo "deb [signed-by=/etc/apt/keyrings/mysql.gpg] https://repo.mysql.com/apt/debian bookworm mysql-8.4-lts" \
        > /etc/apt/sources.list.d/mysql.list; \
    apt-get update; \
    # Pinned exactly. Oracle's repo carries only the current release of each
    # series, so when 8.4.12 lands this fails LOUDLY and needs a one-line bump —
    # which is the reviewable outcome, unlike silently drifting forward.
    apt-get install -y --no-install-recommends "mysql-server=$MYSQL_VERSION"; \
    \
    apt-mark auto '.*' > /dev/null; \
    # The runtime halves of the -dev packages, named explicitly. gd links
    # against these, and without them the build still SUCCEEDS while gd.so is
    # unloadable — PHP only says so at startup, so the image boots and serves
    # every listing photo broken. A booted probe caught exactly that:
    # `Unable to load dynamic library 'gd.so' (libpng16.so.16: cannot open
    # shared object file)`.
    #
    # docker-php's ldd|dpkg-query idiom was tried first and silently marked
    # NOTHING: ldd prints /lib/x86_64-linux-gnu/… while dpkg records the
    # usrmerged /usr/lib/… path, so every lookup missed — and `2>/dev/null`
    # ate the complaint. Three named packages beat a pipeline that can fail
    # to zero without saying so.
    apt-mark manual $savedAptMark mysql-server unzip patch python3 \
        ca-certificates curl libpng16-16 libjpeg62-turbo libfreetype6 > /dev/null; \
    apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false; \
    rm -rf /var/lib/apt/lists/*; \
    # Osclass connects as mysqli's default socket; Debian's server puts it here,
    # while PHP's compiled-in default is /tmp/mysql.sock. Without this the app
    # cannot reach a database running in its own container.
    { \
        echo 'mysqli.default_socket = /var/run/mysqld/mysqld.sock'; \
        echo 'memory_limit = 256M'; \
    } > "$PHP_INI_DIR/conf.d/zz-classifieds.ini"; \
    cp "$PHP_INI_DIR/php.ini-production" "$PHP_INI_DIR/php.ini"; \
    # The class fix behind the gd breakage above: an extension that fails to
    # load is a STARTUP WARNING, not an error, so nothing else in this build
    # would notice. Assert the two the application cannot run without, in the
    # same layer that installs them.
    php -r 'exit(extension_loaded("gd") && extension_loaded("mysqli") ? 0 : 1);' \
        || { echo "runtime: gd/mysqli did not load — check the runtime shared libraries" >&2; \
             php -m; exit 1; }; \
    # The package's postinst already initialised a datadir (24 files: auto.cnf,
    # binlog.000001, a self-signed CA, the redo log). Two problems, one fix:
    # `mysqld --initialize-insecure` refuses a non-empty datadir, and the final
    # stage COPYs OUR datadir over this one — a COPY merges, so the package's
    # server-uuid and binlog would survive underneath the restored cluster.
    rm -rf /var/lib/mysql; \
    mkdir -p /var/lib/mysql; \
    chown mysql:mysql /var/lib/mysql

WORKDIR /usr/src/myapp

# ===
# App — Osclass 8.1.2 from its own release zip, plus WebArena's changes.
# ===
FROM runtime AS app
COPY webarena-modifications.patch /tmp/webarena-modifications.patch
COPY config.php /tmp/config.php
# The two zips are bind-mounted, not COPYed: each is read once and kept never,
# and a COPY commits it to a layer that the `rm` below can only whiteout.
RUN --mount=type=bind,from=datasets,source=osclass-v8.1.2.zip,target=/tmp/osclass.zip \
    --mount=type=bind,from=datasets,source=classifieds_docker_compose.zip,target=/tmp/compose.zip \
    set -eux; \
    unzip -qq /tmp/osclass.zip -d /usr/src/myapp; \
    patch -p1 -d /usr/src/myapp --batch < /tmp/webarena-modifications.patch; \
    # classifieds_restore.sql is the RESET script the reset controller executes,
    # not seed data — dropping it silently breaks task isolation between
    # benchmark runs, with nothing failing at build time. Taken from the
    # hash-pinned zip rather than out of the upstream image; the two copies are
    # both 19,001 bytes and differ only in where one blank line sits.
    unzip -qq -j /tmp/compose.zip \
        classifieds_docker_compose/mysql/classifieds_restore.sql \
        -d /usr/src/myapp; \
    cp /tmp/config.php /usr/src/myapp/config.php; \
    rm -f /tmp/webarena-modifications.patch /tmp/config.php; \
    # ⚠️ DEVIATION 2: three runtime log files (oc-content/debug.log,
    # queries.log, explain_queries.log — 73 MB) and the macOS .DS_Store/._*
    # AppleDouble files are baked into the upstream image. They are debris from
    # how it was assembled, not part of the benchmark, and are not shipped.
    rm -f /usr/src/myapp/oc-content/*.log; \
    mkdir -p /usr/src/myapp/oc-content/uploads

# =======
# Restore — load the seed dump into a build-time MySQL datadir. All of it
# happens ONCE here, so a container boot does no restore work at all — the same
# property the reddit and Magento images have.
#
# classifieds_uploads.tar is deliberately NOT copied in: the item photos are
# bucketed on the CI host and ADDed into the final stage instead (see [media] in
# image.toml), so nothing here reads or writes them.
# =======
FROM app AS restore
COPY --from=stagelib partition-tree.py /partition-tree.py
COPY restore-stage.sh /restore-stage.sh
RUN --mount=type=bind,from=datasets,source=classifieds_docker_compose.zip,target=/tmp/compose.zip \
    /bin/bash /restore-stage.sh

# =====
# Audit — its own layer so a failed assertion does not re-run the restore.
# =====
FROM restore AS audited
COPY app-tree.sha256 /app-tree.sha256
COPY audit.sh /audit.sh
RUN /bin/bash /audit.sh

# =====
# Final — carries the restored datadir, the app tree and the item photos, but
# not the dump that produced the datadir.
# =====
FROM runtime
# One layer per staging bucket keeps every pushed layer under ~10G. These carry
# the 105 MB app tree only — the photos arrive through the ADD block below — so
# the second bucket is expected to be empty and a COPY of an empty bucket is a
# no-op. Bucket count must match BUCKET_COUNT in restore-stage.sh.
COPY --from=audited /staging/bucket-00/ /usr/src/myapp/
COPY --from=audited /staging/bucket-01/ /usr/src/myapp/
COPY --from=audited /var/lib/mysql /var/lib/mysql
# The shared supervisor (#73): any service that exits fails the container.
COPY --from=stagelib run-services.sh /run-services.sh

# --- Media: 73G of item photos, bucketed on the CI host, never in a build stage ---
# builder/docker.py writes these into .media/, a gitignored subdir of this image
# dir — which is the default build context, and the only context ADD can extract
# from (it has no --from).
#
# The target is oc-content, not oc-content/uploads: the archive's own members are
# uploads/<item>/…, which is why [media].strip is empty. Count must match
# [media].max_buckets in image.toml. Unused buckets carry only the `uploads`
# directory entry and extract to nothing.
ADD --chown=www-data:www-data .media/bucket-00.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-01.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-02.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-03.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-04.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-05.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-06.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-07.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-08.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-09.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-10.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-11.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-12.tar /usr/src/myapp/oc-content/
ADD --chown=www-data:www-data .media/bucket-13.tar /usr/src/myapp/oc-content/

# Checks DIRECTORIES on purpose — both silent failures here leave a root-owned
# directory while every file stays www-data-owned:
#
#   1. `ADD --chown=<name>` does not fail the build when the name does not
#      resolve; it lands everything 0:0 root:root and exits clean.
#   2. `--chown` applies to tar MEMBERS, so a directory existing only implicitly
#      in a member's path is created by the extractor and never chowned.
#      The host-side bucketing emits every ancestor explicitly to prevent this.
#
# Osclass writes new item photos into oc-content/uploads/<item>/ as www-data, so
# a root-owned directory here is a runtime failure that a healthcheck fetching
# the home page cannot see. -quit stops at the first offender; files are never
# stat'd.
RUN bad="$(find /usr/src/myapp/oc-content/uploads -type d ! -user www-data -print -quit)"; \
    if [ -n "$bad" ]; then echo "root-owned uploads dir: $bad" >&2; exit 1; fi

# ⚠️ No `chown -R` here, and that is not a style preference. A recursive chown
# over the app tree touches all 336,634 photo files, and every touched file is
# COPIED INTO A NEW LAYER by the union filesystem — so the fix-the-permissions
# step would have added a second full copy of the 73G payload to the image.
# Measured the wrong way first: the chown alone ran past 12 minutes on this
# host before it was killed. Ownership is therefore never repaired after the
# fact: the app tree is chowned once in the restore stage and COPY --from
# preserves uid/gid, and the photos carry theirs from `ADD --chown` above.
RUN set -eux; \
    mkdir -p /var/run/mysqld; \
    chown mysql:mysql /var/run/mysqld

# RESET_TOKEN is read by the reset controller and is genuinely a constant, so it
# stays an ENV. CLASSIFIEDS is NOT: it is this fleet's HTTP_HOST/HTTP_PORT under
# an app-specific name, and the entrypoint derives it. Leaving a default here
# would mean a silently-ignored HTTP_HOST still serves the old links.
ENV RESET_TOKEN=4b61655535e7ed388f0d40a93600254c

EXPOSE 9980
# --chmod so the mode is a property of the BUILD, not of the checkout.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
# HTTP_HOST/HTTP_PORT are validated, and CLASSIFIEDS derived, before anything
# serves a page; the entrypoint then starts the services and supervises them.
CMD ["/entrypoint.sh"]

# ⚠️ The Host header is load-bearing. Osclass 302s every request whose Host is
# not WEB_PATH's, so a probe that omits it never sees a page at all; it used to
# pass only because -L chased that redirect to the PUBLISHED name, which the
# container generally cannot resolve. Measured here with
# HTTP_HOST=classifieds.depot.example.com:
#     no Host header  -> 302 to the public name, 0 bytes (with -L: "curl: (6)
#                        Could not resolve host")
#     Host=$HTTP_HOST -> 200, 31,721 bytes of rendered page
# Connect over the loopback, present the configured name, and follow nothing:
# readiness must not depend on the container resolving its own public address.
#
# localhost:9980 is the IN-CONTAINER address. Osclass boots MySQL and PHP
# behind the entrypoint's supervisor, so give it a real start period; the per-probe --timeout is
# the parameter that decides whether a slow first page reads as unhealthy.
HEALTHCHECK --start-period=90s --interval=10s --timeout=15s --retries=3 \
  CMD curl -fsS --max-time 14 -H "Host: ${HTTP_HOST}" \
      http://localhost:9980/ >/dev/null || exit 1
