# syntax=docker/dockerfile:1
# WebArena's wikipedia service: kiwix-serve over the pinned English Wikipedia
# ZIM. This is the one service in the fleet that is upstream's own image used
# as-is — kiwix-serve is a published, provenance-clear openZIM release and the
# ZIM is a published Kiwix dump, so there is nothing here to rebuild. What this
# image changes is delivery: upstream bind-mounts the archive from the host
# (`--volume <path>:/data`), which makes the 88.7 GiB download a separate
# manual step. Baking it in makes this benchmark a single pullable artifact
# like the others.
#
# Version parity with WebArena's environment_docker/README.md, which runs
# ghcr.io/kiwix/kiwix-serve:3.3.0. Digest-pinned in the fleet's usual style so
# a moved tag cannot silently change the server under us.
FROM ghcr.io/kiwix/kiwix-serve:3.3.0@sha256:889f08dcbfe94b44a506060632618d671ea0d8ce4aed8a2878e0a24890984374

# Overrides the base's inherited label (which points at openzim/kiwix-tools) so
# GHCR links this package to this repo and inherits its visibility.
LABEL org.opencontainers.image.source="https://github.com/shkolnik/browser-use-benchmarks"

# /zim, NOT the base image's /data. /data is declared VOLUME in the base
# config, and Docker initialises an anonymous volume by copying the image's
# contents at that path on every `docker run` — baking the archive there would
# mean copying 88.7 GiB per container start, and leave a volume that size
# behind each time. Any path outside the declared volume is baked into the
# layers and read in place.
#
# One COPY per part, each its own layer: a registry caps a layer far below
# 88.7 GiB, so a single COPY of the whole file could never be pushed. The parts
# are produced by split-zim.sh (image.toml [prepare]); see it for why libzim
# reassembles them without the whole file ever existing on disk.
#
# They are NOT all the same size, and that is the point: a part boundary must
# never land inside an Xapian index, or search breaks while every article keeps
# serving. Part 9 is the fulltext index exactly — 14.91 GiB, larger than a layer
# — so it arrives as two `.partNN` sub-files that entrypoint.sh concatenates
# before kiwix-serve opens the archive. libzim resolves `.zimaa`, `.zimab`, ...
# and never sees a `.partNN` name, so the extra files are inert until then.
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimaa /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimab /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimac /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimad /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimae /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimaf /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimag /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimah /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimai /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimaj.part00 /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimaj.part01 /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimak /zim/

EXPOSE 80

# Readiness, probed at the IN-CONTAINER port (80) — the published port is 8888
# and nothing is bound to it in here. See docs/service-contract.md.
#
# Two things about this line are measured, not copied from a sibling image:
#
#   - `wget`, not curl. The fleet's standard probe binary is curl, and this
#     Alpine-based base image does not ship it. busybox wget exits non-zero on a
#     404 (checked both ways in-container), so it can carry the check — but only
#     on a path that does NOT redirect: following a redirect it prints the error
#     and still exits 0, which would make this check pass against a broken
#     server. The URL below answers 200 in one hop, with zero redirects.
#
#   - 127.0.0.1, not localhost. kiwix-serve binds 0.0.0.0:80 (IPv4 only), while
#     /etc/hosts maps localhost to both 127.0.0.1 and ::1; busybox wget tries ::1
#     first and gets ECONNREFUSED. `localhost` here would leave a perfectly
#     healthy container permanently unhealthy.
#
# The target is WebArena's own landing page for this service rather than the
# library index, because a kiwix-serve whose archive failed to open still
# answers / with a page. Opening the archive is mmap, not a load, so kiwix
# itself is ready seconds after start.
#
# The start period is long for one reason, and it is not slack: entrypoint.sh
# concatenates the 14.91 GiB fulltext part from its sub-files before the server
# starts. Measured on the real archive: **24 s** (~640 MiB/s on an idle host; an
# earlier ~106 s figure was extrapolated from a host busy with a parallel build,
# which is the number to plan for). 1200 s is a wide margin for slower storage,
# and costs nothing when the check passes on the first probe. `docker compose
# up --wait` treats UNHEALTHY as terminal but keeps waiting through `starting`,
# so this number, not a driver timeout, is what a first boot is allowed.
HEALTHCHECK --interval=30s --timeout=15s --start-period=1200s --retries=5 \
    CMD wget -T 10 -q -O /dev/null \
        "http://127.0.0.1/wikipedia_en_all_maxi_2022-05/A/User:The_other_Kiwix_guy/Landing"

# --chmod so the mode is a property of the BUILD, not of the checkout.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
# The base entrypoint runs `kiwix-serve --port=80 $@`, so this is the archive
# argument, and entrypoint.sh passes it straight through. There is deliberately
# no file at this path — libzim resolves the .zimaa/.zimab/... parts beside it.
CMD ["/zim/wikipedia_en_all_maxi_2022-05.zim"]
