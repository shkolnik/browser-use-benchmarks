# WebArena map backend — OpenStreetMap raster tile server.
#
# The upstream AMI downloads osm_tile_server.tar at boot, extracts it into
# /var/lib/docker/volumes, and bind-mounts the osm-data and osm-tiles volumes
# into a container. We bake both volumes into the image, so a pulled image
# serves tiles with no external fetch and no boot-time import.
#
# Base pinned BY DIGEST, not by tag. Upstream pulls
# `overv/openstreetmap-tile-server` completely untagged, so there is no
# upstream version string to match; the baked Postgres cluster is what dates
# it. See README.md — and note the PG-major assert in restore-stage.sh, which
# is what actually enforces compatibility.
FROM overv/openstreetmap-tile-server@sha256:b6a79da39b6d0758368f7c62d22e49dd3ec59e78b194a5ef9dee2723b1f3fa79 AS restore
# The builder passes the verified tar via the `datasets` build context, so the
# 41G archive never becomes a layer of the shipped image.
COPY --from=datasets osm_tile_server.tar /tmp/osm_tile_server.tar
COPY --from=stagelib partition-tree.py /partition-tree.py
COPY restore-stage.sh /restore-stage.sh
RUN /bin/bash /restore-stage.sh

FROM overv/openstreetmap-tile-server@sha256:b6a79da39b6d0758368f7c62d22e49dd3ec59e78b194a5ef9dee2723b1f3fa79
# Same wipe as the restore stage, for the same reason and in the final image
# this time: the base ships an initdb'd cluster at /data/database/postgres, and
# COPYing the restored one on top of it would leave every base-only file in
# place — WAL segments from a different system identifier included. Only the
# colliding names get replaced, so the result would be a merge of two clusters.
RUN rm -rf /data/database /data/tiles \
    && install -d -o renderer -g renderer /data/database /data/tiles
# One layer per staging bucket keeps every pushed layer under ~10G (GHCR's
# comfortable ceiling). Bucket count must match BUCKET_COUNT in
# restore-stage.sh. COPY --from=<stage> preserves uid/gid, which this image
# depends on: the cluster must stay postgres-owned and the tiles renderer-owned.
COPY --from=restore /staging/bucket-00/ /data/
COPY --from=restore /staging/bucket-01/ /data/
COPY --from=restore /staging/bucket-02/ /data/
COPY --from=restore /staging/bucket-03/ /data/
COPY --from=restore /staging/bucket-04/ /data/
COPY --from=restore /staging/bucket-05/ /data/
COPY --from=restore /staging/bucket-06/ /data/
COPY --from=restore /staging/bucket-07/ /data/
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
