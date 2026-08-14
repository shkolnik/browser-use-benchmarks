# webarena/map-tile — OpenStreetMap raster tile server

Rebuilds the tile-serving third of the WebArena map backend as a self-contained image. Upstream ships
this as an AWS AMI whose cloud-init downloads `osm_tile_server.tar` at boot, unpacks it into
`/var/lib/docker/volumes`, and bind-mounts the `osm-data` and `osm-tiles` volumes into a container.
This image bakes the database in, so a pulled image serves tiles with no external fetch and no import.

`osm-tiles` is *not* in the archive and is not baked: see the decision below. It is the rendered-tile
cache, and docker creates it empty at run time.

Behavioural reference: `webarena-map-backend-boot-init.yaml` (upstream cloud-init user-data), verified
byte-identical to the copy published at the root of `web-arena-x/webarena`.

## Provenance

| | |
|---|---|
| base image | `overv/openstreetmap-tile-server@sha256:b6a79da3…` — pinned by **digest** |
| tile data | `osm_tile_server.tar`, 41,280,327,680 bytes, from the public S3 bucket `webarena-map-server-data` |

The bucket is in **us-east-1**; the upstream yaml's own aws config says `us-east-2`, which is wrong.
No upstream checksum exists, so the sha256 in `image.toml` was **computed at import** by streaming the
object and checking the byte count against `Content-Length` in the same pass.

### There is no upstream version tag — but the data dates itself

Upstream runs `docker pull overv/openstreetmap-tile-server` **completely untagged**, so the version any
given WebArena instance ran is whatever `:latest` was the morning it booted. No release tag matches.
The baked Postgres cluster settles it:

| evidence | value |
|---|---|
| `/data/database/postgres/PG_VERSION` | **15** |
| `postmaster.opts` (written at last start, 2024-09-24) | `/usr/lib/postgresql/15/bin/postgres` |
| `postgresql.auto.conf` mtime = initdb time | **2023-03-18 20:48 UTC** |
| release `v2.3.0` | published 2023-03-18 **20:37 UTC** — ships postgresql-**14** |
| master commit `61270b8b` "Add support to PostgreSQL 15" | 2023-03-18 **20:45:25 UTC** |

`initdb` ran **three minutes after** PG15 landed on master and eleven after v2.3.0 shipped with PG14.
So the upstream tile server is an untagged `:latest` built from master strictly *after* v2.3.0 — which
is exactly why no release tag can be cited. Pinning today's `:latest` digest is therefore both the
honest reading of "what upstream runs" and the only option verified compatible with the data.

**This is a hard constraint, not a preference.** A Postgres cluster only starts under its own major
version. The final stage asserts the cluster's `PG_VERSION` equals the major the base image ships,
so a future base bump to PG16 fails the build loudly instead of producing an image whose database
silently refuses to start.

## Decisions

**The archive holds one volume, not two — measured, after assuming otherwise.** A full listing is
**1,624 entries and every one of them is under `osm-data`**. Upstream's cloud-init mounts
`--volume=osm-tiles:/data/tiles/` as well, and the first version of this image read those two mounts
as evidence the tar carried two volumes; the build failed on the missing `osm-tiles/_data` rather than
shipping a wrong tree. Docker creates that volume empty at run time, which is right: `/data/tiles` is
the **rendered-tile cache**, generated on demand from the database. An empty directory is the honest
initial state, and the healthcheck's first zoom-0 request is what starts filling it.

**The cluster never enters a build stage.** It is inert — nothing between the archive and the final
image rewrites a byte of it — so it takes the repo's media path (`[media]` in `image.toml`):
`osm_tile_server.tar` is demuxed into one tar per layer on the CI host and `ADD`ed straight into the
final stage. The build writes the 38.4 GiB once, into the layers that ship, instead of extracting it
into a restore stage, partitioning it there and letting a `COPY` lift the same bytes back out.
Nothing else was in that stage — it extracted, validated and partitioned, and all three have gone —
so there is no restore stage and no `restore-stage.sh` at all.

Two consequences worth stating:

- **The layers are a size split, measured from the archive's own headers** (walked over the network
  with ranged GETs; no bulk download). Accounting the way `demux-media.py` does — a 512-byte header
  per member plus the payload rounded up to a block — the 1,622 shipped members cost **38.445 GiB**
  and fill **6 buckets at `limit_kb` = 8 GiB, the largest 7.997 GiB**, comfortably inside GHCR's ~10G
  ceiling. `max_buckets` stays at **8**: it is a ceiling with headroom, and the two spares get a tar
  padded with the archive's one top-level directory (`postgres/`), which re-extracts to a directory
  `bucket-00` already carries.
- **`chown = ""` — the archive's own ownership, and the only correct answer here.** `/data` is split
  two ways: the cluster under `postgres/` is uid/gid **101:103** and the volume root's
  `planet-import-complete` and `region.poly` are **1000:1000**, which are exactly `postgres:postgres`
  and `renderer:renderer` in this image. A single `--chown` would flatten that split, and a cluster
  `postgres` does not own is a cluster `postgres` will not start. An empty `chown` emits no `--chown`
  flag at all, which is what makes `ADD` reproduce the header's uid/gid **and** mode verbatim —
  including the `0700` on `postgres/`, which Postgres equally refuses to start without. Verbatim
  means *numerically*: buildkit ignores the header's `uname`/`gname`, which say `systemd-resolve` and
  `junjieh` — the accounts on the machine the tar was captured on. That is the same hazard the
  extraction's `--numeric-owner` used to answer.

**A 6-component strip, which is neither upstream's 5 nor this image's own earlier 4.** The contents
go straight to `/data/database`, where upstream bind-mounts the volume, so the whole
`projects/ogma3/docker/volumes/osm-data/_data/` prefix comes off — 6 components, declared as the
prefix itself in `[media].strip` (`run_media_prep` derives the count from it). Upstream strips 5
because it is rebuilding docker's volume layout under `/var/lib/docker/volumes`, but it justifies the
number with the `projects/ogma3/docker/volumes/` prefix, which is only **4** — so upstream's own tile
extraction eats the volume *name*. All three tars in this backend differ. Count per tar; never copy
the number from a sibling.

**The uids are the image's, not a coincidence.** `id` against the base resolves **101:103** to
`postgres:postgres` and **1000:1000** to `renderer:renderer`, which is why the archive's own headers
can be shipped as they stand. Resolving them by *name* instead would remap both — the tar's `uname`
fields name accounts from the capture machine, and on one host uid 101 renders as `uuidd`. The final
stage asserts the resulting ownership rather than issuing a blanket `chown`, which would erase the
postgres/renderer split the image depends on.

**The base image's own cluster is deleted first, before the `ADD`s.**
`overv/openstreetmap-tile-server` ships an `initdb`'d cluster at `/data/database/postgres` from its
own build. `ADD`ing on top of it would *merge* two unrelated clusters: only the colliding names get
replaced, and every base-only file survives — WAL segments stamped with a different system identifier
among them. That a merged directory happens to start proves nothing about what it contains. So the
image's first instruction removes `/data/database` and `/data/tiles` and recreates them
`renderer:renderer`, which is the base image's own ownership for `/data/*`; the cluster inside is
`postgres`-owned and carries that from the archive. The same hazard, and the same fix, applies to
`map-nominatim`.

## Validation

- **On the host, a floor**: `[media].min_entries = 1400` — the last point at which a truncated or
  empty archive is visible at all, since past the demux the data is layers and no build stage can
  count it. Measured contents are 1,622 shipped members (1,624 entries, less the two that make up the
  stripped root); the floor sits under that so a re-pinned capture with a different WAL segment count
  is not a build failure. It is a tripwire rather than the real check: the archive is a pinned
  dataset, so `download` has already verified its sha256 end to end.
- **In-build, structural** (one `RUN` in the **final** stage, after the `ADD`s, so it validates what
  ships rather than what was staged): PG-major match (above), the `planet-import-complete` marker
  present, cluster ownership `postgres:postgres`, and a 10G floor on `/data/database` so a truncated
  import fails the build instead of serving blank tiles.

  The ownership assert is the only check on the `ADD`s' lack of a `--chown`. Nothing else in the
  build would notice a `--chown` being introduced, an archive re-captured under different accounts,
  or an extractor resolving uid 101 by name: all three build clean and ship a cluster Postgres
  cannot read.
- **In-container, behavioural**: the `HEALTHCHECK` fetches `/tile/0/0/0.png` — upstream's own
  readiness check — so the container reports healthy only once it has really rendered a tile.
- **Pre-push, behavioural**: `smoke` boots the image and polls `[service].healthcheck` — a real
  rendered tile — before the push step, this time from the host through the published port.

⚠️ The healthcheck renders zoom 0 (the whole world) from a cold 40G cluster on first request. If smoke
proves flaky, raise `poll_health`'s timeout rather than substituting a cheaper URL, which would stop
proving the thing worth proving.
