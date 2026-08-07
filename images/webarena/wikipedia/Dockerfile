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
# One COPY per part, each its own ~9 GiB layer: GHCR rejects a layer over 10 GB
# and the archive is 88.7 GiB, so a single COPY of the whole file could never be
# pushed. The parts are produced by split-zim.sh (image.toml [prepare]); see it
# for why libzim reassembles them without the whole file ever existing on disk.
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimaa /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimab /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimac /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimad /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimae /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimaf /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimag /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimah /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimai /zim/
COPY --from=datasets wikipedia_en_all_maxi_2022-05.zimaj /zim/

# The base entrypoint runs `kiwix-serve --port=80 $@`, so this is the archive
# argument. There is deliberately no file at this path — libzim resolves the
# .zimaa/.zimab/... parts sitting beside it.
CMD ["/zim/wikipedia_en_all_maxi_2022-05.zim"]
