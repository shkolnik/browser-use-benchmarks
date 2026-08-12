# WebArena map backend — OSRM routing (car / bike / foot).
#
# The upstream AMI downloads osrm_routing.tar at boot and bind-mounts
# /opt/osrm/{car,bike,foot} into three containers. We bake the routing data
# into the image instead, so a pulled image routes with no external fetch.
#
# The tar has NO nested prefix (verified from its headers): its top level is
# already car/ bike/ foot/, each holding us-northeast-latest.osrm*. That is
# unlike the tile and nominatim tars, which carry a projects/.../volumes/
# prefix of DIFFERENT depths — see README.md.

FROM ghcr.io/project-osrm/osrm-backend:v5.27.1 AS restore
# The builder passes the verified tar via the `datasets` build context, so the
# 21G archive never becomes a layer of the shipped image.
COPY --from=datasets osrm_routing.tar /tmp/osrm_routing.tar
RUN mkdir -p /data \
    && tar -C /data -xf /tmp/osrm_routing.tar \
    && rm -f /tmp/osrm_routing.tar
# Structural validation, in its OWN layer so a failure here does not re-run the
# extraction above. Behavioural validation (does it actually route?) is the
# pipeline's `smoke` step, which boots this image and polls the healthcheck in
# image.toml BEFORE anything is pushed.
# The file set below is not a guess: it is what osrm-routed itself names as
# "Required files are missing" when they are absent (observed by running the
# base image against a directory holding ONLY .mldgr). Checking .mldgr alone
# would pass a tar that is missing every sibling and defer the failure to the
# smoke step — a build-time check that claims more than it verifies.
RUN set -eu; \
    for p in car bike foot; do \
      base="/data/$p/us-northeast-latest.osrm"; \
      for ext in mldgr ramIndex fileIndex edges geometry names properties \
                 timestamp datasource_names icd maneuver_overrides \
                 turn_weight_penalties turn_duration_penalties; do \
        [ -f "$base.$ext" ] || { echo "restore: $base.$ext missing — tar layout changed" >&2; exit 1; }; \
      done; \
      sz=$(stat -c %s "$base.mldgr"); \
      [ "$sz" -gt 100000000 ] || { echo "restore: $base.mldgr is only $sz bytes — truncated" >&2; exit 1; }; \
      echo "restore: $p ok (mldgr $sz bytes, full file set present)"; \
    done

FROM ghcr.io/project-osrm/osrm-backend:v5.27.1
# One COPY per profile: each is ~7G, which keeps every pushed layer well under
# GHCR's ~10G ceiling without needing the bucket-partitioning the much larger
# gitlab and shopping trees require.
COPY --from=restore /data/car/ /data/car/
COPY --from=restore /data/bike/ /data/bike/
COPY --from=restore /data/foot/ /data/foot/
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
