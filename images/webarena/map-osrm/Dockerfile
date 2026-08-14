# WebArena map backend — OSRM routing (car / bike / foot).
#
# The upstream AMI downloads osrm_routing.tar at boot and bind-mounts
# /opt/osrm/{car,bike,foot} into three containers. We bake the routing data
# into the image instead, so a pulled image routes with no external fetch.
#
# The routing data is inert — nothing in the build writes into it — so it takes
# the media path ([media] in image.toml): the archive is demuxed into one tar
# per layer on the CI host and ADDed here, and the tree never enters a build
# stage at all. There is therefore no restore stage; the only thing one ever did
# was extract 19.8 GiB so that a COPY could lift the same bytes back out.

FROM ghcr.io/project-osrm/osrm-backend:v5.27.1
# One ADD per bucket, and the count is [media].max_buckets rather than the
# number of buckets the data fills: every index up to the ceiling gets a tar,
# and an unused one carries the three profile directories as padding, because
# docker does not recognise a zero-member tar as an archive and would ADD it as
# a file. ADD auto-extracts only from the DEFAULT build context, which is why
# .media/ sits inside the image dir.
#
# --chown=root:root is not the archive's own ownership: its 78 files are already
# 0/0, but the three profile directories carry uid/gid 1018 (`fangzhex`) from the
# machine the tar was captured on, a name that resolves to nothing here. See
# image.toml.
#
# Ahead of the script COPYs on purpose: editing entrypoint.sh then rebuilds the
# tail rather than 19.8 GiB of layers.
ADD --chown=root:root .media/bucket-00.tar /data/
ADD --chown=root:root .media/bucket-01.tar /data/
ADD --chown=root:root .media/bucket-02.tar /data/
ADD --chown=root:root .media/bucket-03.tar /data/
# Structural validation, in the final stage because that is where the routing
# data now is — so it checks what SHIPS rather than what was staged.
# Behavioural validation (does it actually route?) is the pipeline's `smoke`
# step, which boots this image and polls the healthcheck in image.toml BEFORE
# anything is pushed.
# The file set below is not a guess: it is what osrm-routed itself names as
# "Required files are missing" when they are absent (observed by running the
# base image against a directory holding ONLY .mldgr). Checking .mldgr alone
# would pass a tree that is missing every sibling and defer the failure to the
# smoke step — a build-time check that claims more than it verifies.
# The ownership assert is what closes the loop on the ADDs above: `ADD --chown`
# does not fail the build on a name the image cannot resolve, it lands the tree
# 0:0 and exits clean, so nothing else here would notice the flag being dropped
# or misspelled.
RUN set -eu; \
    for p in car bike foot; do \
      base="/data/$p/us-northeast-latest.osrm"; \
      for ext in mldgr ramIndex fileIndex edges geometry names properties \
                 timestamp datasource_names icd maneuver_overrides \
                 turn_weight_penalties turn_duration_penalties; do \
        [ -f "$base.$ext" ] || { echo "media: $base.$ext missing — the bucket tars do not carry the declared tree" >&2; exit 1; }; \
      done; \
      sz=$(stat -c %s "$base.mldgr"); \
      [ "$sz" -gt 100000000 ] || { echo "media: $base.mldgr is only $sz bytes — truncated" >&2; exit 1; }; \
      echo "media: $p ok (mldgr $sz bytes, full file set present)"; \
    done; \
    bad=$(find /data \( ! -user root -o ! -group root \) -print); \
    [ -z "$bad" ] || { echo "media: --chown did not take, these are not root:root:" >&2; echo "$bad" >&2; exit 1; }
COPY entrypoint.sh /entrypoint.sh
COPY healthcheck.sh /healthcheck.sh
# The base image sets no ENTRYPOINT and CMD ["bash"] (verified by inspecting its
# config blob), which is why upstream passes `osrm-routed …` as the run command.
EXPOSE 5000 5001 5002
# Required, not optional: the base declares no HEALTHCHECK, and `bin/build
# smoke` refuses to boot an image without one — `docker compose up --wait` on
# such an image degrades to waiting for RUNNING, which a container that never
# serves a byte also reaches. The probe checks all three profiles; see the
# script for why it speaks HTTP over bash's /dev/tcp.
HEALTHCHECK --start-period=180s --interval=15s --timeout=15s --retries=5 \
  CMD /bin/bash /healthcheck.sh || exit 1
ENTRYPOINT ["/bin/bash", "/entrypoint.sh"]
