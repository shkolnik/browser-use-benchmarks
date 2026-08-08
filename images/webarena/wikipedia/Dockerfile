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

# /zim-parts, NOT the base image's /data. /data is declared VOLUME in the base
# config, and Docker initialises an anonymous volume by copying the image's
# contents at that path on every `docker run` — baking the archive there would
# mean copying 88.7 GiB per container start, and leave a volume that size
# behind each time. Any path outside the declared volume is baked into the
# layers and read in place.
#
# The parts live HERE and the finished archive is assembled into /zim, a
# different and deliberately empty directory. Keeping them apart is what lets an
# operator mount a named volume on /zim to pay the join once for the life of the
# volume rather than once per container: a volume over the PARTS would be seeded
# from the image, copying 88.7 GiB before the join wrote another 88.7 GiB into
# the same volume. See entrypoint.sh.
#
# One COPY per part, each its own layer: a registry caps a layer far below
# 88.7 GiB, so a single COPY of the whole file could never be pushed. The parts
# are produced by split-zim.sh (image.toml [prepare]).
#
# They are NOT all the same size. A part boundary must never land inside an
# Xapian index — and on this archive that turns out not to be enough either, so
# the entrypoint rebuilds the whole file; README.md has the measurements. Part 9
# is the fulltext index exactly (14.91 GiB, larger than a layer) and arrives as
# two `.partNN` sub-files, which is what makes it shippable at all.
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimaa /zim-parts/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimab /zim-parts/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimac /zim-parts/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimad /zim-parts/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimae /zim-parts/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimaf /zim-parts/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimag /zim-parts/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimah /zim-parts/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimai /zim-parts/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimaj.part00 /zim-parts/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimaj.part01 /zim-parts/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimak /zim-parts/

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
# joins the parts back into the whole 88.7 GiB archive before the server starts,
# because a split archive serves every article perfectly and no full-text search
# at all (README.md). Measured on the real archive: **632 s**, about 144 MiB/s —
# not the ~640 MiB/s one part reaches on its own, because the join reads and
# writes the same disk at once. 3600 s is a ~6x margin for slower storage, and
# costs nothing when the check passes on its first probe. `docker compose up
# --wait` treats UNHEALTHY as terminal but keeps waiting through `starting`, so
# this number, not a driver timeout, is what a first boot is allowed.
HEALTHCHECK --interval=30s --timeout=15s --start-period=3600s --retries=5 \
    CMD wget -T 10 -q -O /dev/null \
        "http://127.0.0.1/wikipedia_en_all_maxi_2022-05/A/User:The_other_Kiwix_guy/Landing"

# --chmod so the mode is a property of the BUILD, not of the checkout.
#
# assemble-zim goes on PATH, not beside the entrypoint: it is a command an
# operator is meant to run, to spend the ~632 s join deliberately rather than on
# a first boot somebody is waiting on —
#     docker run --rm -v wiki-zim:/zim --entrypoint assemble-zim <image>
# The entrypoint calls it too, so a plain `docker run` needs no ceremony.
COPY --chmod=755 assemble-zim /usr/local/bin/assemble-zim
COPY --chmod=755 entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
# The base entrypoint runs `kiwix-serve --port=80 $@`, so this is the archive
# argument, and entrypoint.sh passes it straight through. Nothing exists at this
# path in the image: the entrypoint builds it from /zim-parts first, into
# whatever /zim happens to be — the container's writable layer, or a named
# volume if one is mounted there.
CMD ["/zim/wikipedia_en_all_maxi_2022-05.zim"]
