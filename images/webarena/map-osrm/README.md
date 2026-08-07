# webarena/map-osrm — OSRM routing backend (car / bike / foot)

Rebuilds the routing third of the WebArena map backend as a self-contained image. Upstream ships this
as an AWS AMI whose cloud-init downloads `osrm_routing.tar` at boot and bind-mounts
`/opt/osrm/{car,bike,foot}` into three containers; this image bakes the routing data in, so a pulled
image routes with no external fetch and no boot-time download.

Behavioural reference: `webarena-map-backend-boot-init.yaml` (upstream cloud-init user-data).

## Provenance

| | |
|---|---|
| base image | `ghcr.io/project-osrm/osrm-backend:v5.27.1` — the exact tag upstream pulls (version parity; no upgrade) |
| routing data | `osrm_routing.tar`, 21,278,935,040 bytes, from the public S3 bucket `webarena-map-server-data` |

The bucket is in **us-east-1**. The upstream yaml's own aws config says `us-east-2`; that is wrong,
and the HTTPS URL in `image.toml` is the region that actually serves the object.

No upstream checksum exists for this tar, so the sha256 in `image.toml` was **computed at import** in
this repo by streaming the object and hashing it, with the byte count checked against
`Content-Length` in the same pass — a stream that dies mid-transfer otherwise yields a perfectly
plausible hash of a prefix. Same posture as `webarena/shopping`'s tar.

The tar's entries are owned by `fangzhex` and dated **2023-07-09**, i.e. the routing graphs are a 2023
build of `us-northeast-latest`, republished to this bucket in August 2025.

## Decisions

**One image serving all three profiles, not three images.** The binary and its arguments are
identical across car, bike and foot — only the `/data` directory and the port differ. Three images
would mean three copies of the same base for ~21G of data that the benchmark always uses together.
The entrypoint runs all three on the ports upstream's clients expect (car 5000, bike 5001, foot 5002).

*Tradeoff, stated plainly:* three processes in one container is less docker-idiomatic than one
process per container, and you cannot run just one profile. Accepted because the WebArena map tasks
need all three simultaneously, and because it keeps the repo's one-image-one-service model honest —
the service here is "the routing backend", not "a routing profile". The entrypoint exits non-zero if
any single profile dies, so a partially-working backend fails loudly instead of silently scoring
tasks wrong.

**One `COPY` per profile.** Each profile is ~7G, comfortably under GHCR's ~10G layer ceiling, so this
image needs none of the bucket-partitioning that `gitlab` and `shopping` require.

**No `--strip-components`.** Verified from the tar's own headers: its top level is already
`car/ bike/ foot/`. This is worth stating because the sibling tars are *not* like this, and upstream's
comment about them is misleading:

- `osm_tile_server.tar` → `projects/ogma3/docker/volumes/…` (4 components before the volume name)
- `nominatim_volumes.tar` → `projects/metis2/docker/docker/volumes/…` (5 — a different prefix, with
  `docker` appearing twice)

Upstream applies `--strip-components=5` to both and justifies it with the `ogma3` prefix alone. That
number is right for nominatim and over-strips the tile tar, collapsing `osm-data` and `osm-tiles` onto
one path. Don't copy the number to the sibling images; count the components per tar.

## Validation

Two layers, deliberately separated:

- **In-build, structural** (its own `RUN`, after the extraction layer so a failure doesn't re-run the
  21G extract): every profile has the **full file set `osrm-routed` requires** — `.mldgr`, `.ramIndex`,
  `.fileIndex`, `.edges`, `.geometry`, `.names`, `.properties`, `.timestamp`, `.datasource_names`,
  `.icd`, `.maneuver_overrides`, `.turn_weight_penalties`, `.turn_duration_penalties` — plus a size
  floor of 100MB on `.mldgr`. That list is not a guess: it is what the binary itself prints as
  *"Required files are missing, cannot continue"*, observed by running the base image against a
  directory holding only `.mldgr`. An earlier version checked `.mldgr` alone, which would have passed
  a tar missing every sibling and deferred the failure to the smoke step.
- **Pre-push, behavioural**: the pipeline's `smoke` step boots this image and polls
  `[service].healthcheck` — a real route request — *before* the push step. An image that assembles but
  cannot route never reaches the registry.
