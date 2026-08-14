# WebArena map backend — Nominatim geocoding.
#
# The upstream AMI downloads nominatim_volumes.tar at boot, unpacks it into
# /var/lib/docker/volumes, and bind-mounts the nominatim-data volume at
# /var/lib/postgresql/14/main (plus a flatnode volume). We bake the Postgres
# cluster into the image instead, so a pulled image geocodes with no external
# fetch and no import.
#
# Base pinned BY DIGEST alongside upstream's `mediagis/nominatim:4.2`. The tag
# is real — it gives genuine version parity — but it is mutable, and the baked
# cluster is a PostgreSQL 14 data directory that only starts under a 14 base.
# restore-stage.sh asserts that major, so a moved tag fails the build loudly.
FROM mediagis/nominatim@sha256:d0eae7b513747e57b33933a11ba7297cb499684eb9f1dd3f206cbb2db1b8741e AS restore
# osm_dump.tar is deliberately absent from this stage: the project data is inert,
# so it is demuxed into bucket tars on the CI host and ADDed into the final stage
# (see [media] in image.toml). Nothing here reads it.
COPY --from=stagelib partition-tree.py /partition-tree.py
COPY restore-stage.sh /restore-stage.sh
# Bind-mounted rather than COPYed. COPY --from=datasets would write all
# 124,774,901,760 bytes into a build layer to read 34.8G back out of them, and
# the `rm` that used to follow reclaimed none of it — a deleted file in a
# committed layer is a whiteout in the next one, not free space. That single
# line was over a fifth of this image's 520.7G measured peak disk. A bind mount
# is read-only and never committed, so the archive is read where it already
# sits.
RUN --mount=type=bind,from=datasets,source=nominatim_volumes.tar,target=/tmp/nominatim_volumes.tar \
    /bin/bash /restore-stage.sh

FROM mediagis/nominatim@sha256:d0eae7b513747e57b33933a11ba7297cb499684eb9f1dd3f206cbb2db1b8741e
# The base image ships its OWN initdb'd cluster at this path, created when the
# image was built. COPYing onto it would MERGE two unrelated clusters — ours
# would overwrite the files it shares a name with and leave every base-only file
# behind, including a global/pg_control from a different system identifier. That
# is a corrupt data directory, not a restored one. Wipe first, and recreate the
# directory with the owner and 0700 mode Postgres refuses to start without
# (the COPYs below fill it, but never set the mode of the root itself).
RUN rm -rf /var/lib/postgresql/14/main \
    && install -d -o postgres -g postgres -m 700 /var/lib/postgresql/14/main
# One layer per staging bucket keeps every pushed layer under ~10G (GHCR's
# comfortable ceiling). Bucket count must match BUCKET_COUNT in
# restore-stage.sh. COPY --from=<stage> preserves uid/gid, which this image
# depends on absolutely: a cluster postgres does not own is a cluster postgres
# will not start.
COPY --from=restore /staging/bucket-00/ /var/lib/postgresql/14/main/
COPY --from=restore /staging/bucket-01/ /var/lib/postgresql/14/main/
COPY --from=restore /staging/bucket-02/ /var/lib/postgresql/14/main/
COPY --from=restore /staging/bucket-03/ /var/lib/postgresql/14/main/
COPY --from=restore /staging/bucket-04/ /var/lib/postgresql/14/main/
COPY --from=restore /staging/bucket-05/ /var/lib/postgresql/14/main/
# --- Media: 1.75G of project data, demuxed on the CI host, never in a build stage ---
# The project data upstream bind-mounts at /nominatim/data. Only the import path
# reads it, and the import never runs here — it is shipped so the settings below
# name files that exist, and so a re-import is possible from the image alone.
# Inert on both counts, which is what makes it media: it enters and leaves
# byte-identical, so it arrives as a layer instead of a COPY out of a stage that
# had to extract it first.
#
# builder/docker.py writes the tar into .media/, a gitignored subdir of this
# image dir — which is the default build context, and the only context ADD can
# extract from (it has no --from). One line, because [media].max_buckets is 1;
# the count must match it. The archive's own members are osm_dump/<file>, which
# [media].strip drops, so they land directly in the directory named here.
ADD --chown=root:root .media/bucket-00.tar /nominatim/data/

# /app/config.sh EXITS 1 unless exactly one of PBF_PATH / PBF_URL is set, on
# every boot, including the one that skips the import. So this is required, not
# decorative — and unlike upstream's, it points at a file that is really there.
ENV PBF_PATH=/nominatim/data/us-northeast-latest.osm.pbf
# Consumed only by /app/init.sh, i.e. never in a baked image. Set for the same
# reason: it records what this cluster was imported WITH, at a path that
# resolves, so a re-import from this image reproduces upstream's data.
ENV IMPORT_WIKIPEDIA=/nominatim/data/wikimedia-importance.sql.gz
# Substituted into /nominatim/.env by config.sh at boot; upstream's value.
ENV IMPORT_STYLE=extratags

# The media path takes the project data out of every build stage, and with it
# restore-stage.sh's check that the two files arrived under the names upstream's
# settings use. [media].min_entries replaces the counting half of that check and
# only the counting half — two entries is two entries whatever they are called.
# This is the naming half, and it asserts the ENV values directly rather than a
# copy of the filenames, so it is the settings themselves that are proven to
# resolve. That is the entire reason this tree ships.
#
# The floors are deliberately far under the real 1,485,107,682 and 393,574,858
# (image.toml pins both): the archive's sha256 is verified at download, so this
# is not where truncation is caught — it is here to fail loudly on an ADD that
# extracted the wrong layout rather than on a re-import hours later.
RUN set -eu; \
    for spec in "$PBF_PATH:1400000000" "$IMPORT_WIKIPEDIA:350000000"; do \
      f=${spec%:*}; floor=${spec##*:}; \
      [ -f "$f" ] || { echo "media: $f missing — osm_dump layout changed" >&2; \
                       ls -la /nominatim/data >&2; exit 1; }; \
      n=$(stat -c %s "$f"); \
      [ "$n" -ge "$floor" ] || { echo "media: $f is only $n bytes" >&2; exit 1; }; \
    done

# A real geocoding query, not a liveness stub: `place_id` only appears if the
# gazetteer actually resolved the name. The long start period is the cluster's
# first-boot cost — /app/start.sh runs `nominatim refresh --website --functions`
# and then warms the caches against a 35G database before it serves.
HEALTHCHECK --start-period=900s --interval=30s --timeout=30s --retries=5 \
  CMD curl -fsS --max-time 25 \
      'http://localhost:8080/search?q=carnegie+mellon+university&format=json&limit=1' \
      | grep -q '"place_id"' || exit 1

EXPOSE 8080
# The base image's CMD, restated because the ENV lines above must not be the
# last thing a reader sees and assume the command was lost. /app/start.sh skips
# the import when /var/lib/postgresql/14/main/import-finished exists, which
# restore-stage.sh asserts is baked in.
CMD ["/app/start.sh"]
