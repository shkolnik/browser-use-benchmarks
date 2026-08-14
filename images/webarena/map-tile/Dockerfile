# WebArena map backend — OpenStreetMap raster tile server.
#
# The upstream AMI downloads osm_tile_server.tar at boot, extracts it into
# /var/lib/docker/volumes, and bind-mounts the osm-data volume into a
# container. We bake the database into the image, so a pulled image serves
# tiles with no external fetch and no boot-time import.
#
# The cluster is inert — nothing in the build writes a byte of it — so it takes
# the media path ([media] in image.toml): the archive is demuxed into one tar
# per layer on the CI host and ADDed here, and the 38.4 GiB never enters a build
# stage at all. There is therefore no restore stage; the only thing one ever did
# was extract the cluster so that a COPY could lift the same bytes back out.
#
# Base pinned BY DIGEST, not by tag. Upstream pulls
# `overv/openstreetmap-tile-server` completely untagged, so there is no
# upstream version string to match; the baked Postgres cluster is what dates
# it. See README.md — and note the PG-major assert below, which is what
# actually enforces compatibility.
FROM overv/openstreetmap-tile-server@sha256:b6a79da39b6d0758368f7c62d22e49dd3ec59e78b194a5ef9dee2723b1f3fa79
# BEFORE the ADDs, and load-bearing there: the base ships its own initdb'd
# cluster at /data/database/postgres, and extracting ours on top of it would
# MERGE two unrelated clusters. Only the colliding names get replaced; every
# base-only file survives, WAL segments stamped with a different system
# identifier included. That is a corrupt data directory, not a restored one —
# and that a merged one happens to START proves nothing about what it holds.
#
# Recreated renderer-owned, which is the base image's ownership for /data/*.
# /data/tiles gets the same treatment and then stays empty: it is the rendered
# tile cache, built on demand from the database, and the archive carries no
# osm-tiles volume to fill it with (see README.md).
RUN rm -rf /data/database /data/tiles \
    && install -d -o renderer -g renderer /data/database /data/tiles
# One ADD per bucket, and the count is [media].max_buckets rather than the
# number of buckets the data fills: every index up to the ceiling gets a tar,
# and the two unused ones carry the archive's top-level postgres/ directory as
# padding, because docker does not recognise a zero-member tar as an archive and
# would ADD it as a file. ADD auto-extracts only from the DEFAULT build context,
# which is why .media/ sits inside the image dir.
#
# No --chown, deliberately: that is what makes ADD extract the archive's own
# uid/gid and mode, keeping the cluster postgres-owned 0700 and the volume root's
# files renderer-owned. A single --chown would flatten a split this image cannot
# start without. See image.toml.
ADD .media/bucket-00.tar /data/database/
ADD .media/bucket-01.tar /data/database/
ADD .media/bucket-02.tar /data/database/
ADD .media/bucket-03.tar /data/database/
ADD .media/bucket-04.tar /data/database/
ADD .media/bucket-05.tar /data/database/
ADD .media/bucket-06.tar /data/database/
ADD .media/bucket-07.tar /data/database/
# Structural validation, in the final stage because that is where the cluster
# now is — so it checks what SHIPS rather than what was staged. Behavioural
# validation (does it actually render?) is the pipeline's `smoke` step, which
# boots this image and polls the healthcheck in image.toml BEFORE anything is
# pushed.
#
# The ownership assert is what closes the loop on the ADDs above. With no
# --chown flag the tree's ownership is whatever the archive's headers say, and
# nothing else in the build would notice a future --chown being added, an
# archive re-captured under different accounts, or an extractor that resolved
# uid 101 by name instead of by number: all three build clean and ship a cluster
# Postgres cannot read.
#
# A Postgres cluster only starts under its OWN major version. The baked cluster
# is PG15 (upstream ran an untagged :latest built from master minutes after PG15
# support landed — see README). If a future base-image bump moves to PG16, this
# build must fail here rather than ship an image whose database silently refuses
# to start.
RUN set -eu; \
    [ -d /data/database/postgres ] || { echo "media: /data/database/postgres missing — the bucket tars do not carry the declared tree" >&2; ls -la /data/database >&2; exit 1; }; \
    DATA_PG=$(cat /data/database/postgres/PG_VERSION); \
    IMAGE_PG=$(ls -d /usr/lib/postgresql/*/ | head -1 | sed 's#.*/postgresql/##; s#/##'); \
    echo "media: cluster PG_VERSION=$DATA_PG, image ships postgresql-$IMAGE_PG"; \
    [ "$DATA_PG" = "$IMAGE_PG" ] || { \
      echo "media: PG MAJOR MISMATCH — baked cluster is $DATA_PG but this image ships $IMAGE_PG." >&2; \
      echo "media: Postgres will not start. Pin a base image with postgresql-$DATA_PG." >&2; \
      exit 1; }; \
    [ -f /data/database/planet-import-complete ] || { echo "media: /data/database/planet-import-complete missing — the dump is not a completed import" >&2; exit 1; }; \
    owner=$(stat -c %U:%G /data/database/postgres); \
    [ "$owner" = "postgres:postgres" ] || { echo "media: /data/database/postgres is owned by $owner, expected postgres:postgres" >&2; exit 1; }; \
    db_kb=$(du -sk /data/database | cut -f1); \
    [ "$db_kb" -gt 10485760 ] || { echo "media: /data/database is only ${db_kb}K — truncated import" >&2; exit 1; }; \
    echo "media: database $((db_kb / 1048576))G, tiles $(du -sh /data/tiles | cut -f1)"
# The base image's ENTRYPOINT is /run.sh with no CMD; upstream supplies `run`
# as the command (its `docker run … overv/openstreetmap-tile-server run` line).
# Baking it as CMD keeps the image runnable with no arguments, and compose can
# still override it.
CMD ["run"]
EXPOSE 80 5432
# Required, not optional: the base declares no HEALTHCHECK, and `bin/build
# smoke` refuses to boot an image without one — `docker compose up --wait` on
# such an image degrades to waiting for RUNNING, which a container that never
# serves a byte also reaches.
#
# Upstream's own readiness check, and a real rendered tile rather than a
# liveness stub. Zoom 0 is the whole world rendered from a cold 40G cluster on
# first request, which is what the long start period and the generous per-probe
# timeout are for. If this proves flaky, raise them — do not substitute a
# cheaper URL, which would stop proving the thing worth proving.
HEALTHCHECK --start-period=900s --interval=30s --timeout=120s --retries=5 \
  CMD curl -fsS --max-time 115 http://localhost/tile/0/0/0.png -o /dev/null || exit 1
