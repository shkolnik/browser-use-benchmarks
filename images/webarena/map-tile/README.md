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
version. `restore-stage.sh` asserts the cluster's `PG_VERSION` equals the major the base image ships,
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

**`--strip-components=6`, which is neither upstream's 5 nor this image's own earlier 4.** The contents
go straight to `/data/database`, where upstream bind-mounts the volume, so the whole
`projects/ogma3/docker/volumes/osm-data/_data/` prefix comes off — 6 components. Upstream strips 5
because it is rebuilding docker's volume layout under `/var/lib/docker/volumes`, but it justifies the
number with the `projects/ogma3/docker/volumes/` prefix, which is only **4** — so upstream's own tile
extraction eats the volume *name*. All three tars in this backend differ. Count per tar; never copy
the number from a sibling.

**`--numeric-owner` on extraction.** The archive carries uid/gid **101:103** for the Postgres cluster
and **1000:1000** for the volume root, which are exactly `postgres:postgres` and `renderer:renderer`
inside this image — verified with `id` against the base. Resolving by *name* against the build host's
`/etc/passwd` would remap both (on one host those uids render as `uuidd` and an unrelated user).
`restore-stage.sh` asserts the resulting ownership rather than issuing a blanket `chown`, which would
erase the postgres/renderer split the image depends on.

**The base image's own cluster is deleted first — in both stages.**
`overv/openstreetmap-tile-server` ships an `initdb`'d cluster at `/data/database/postgres` from its
own build. Restoring on top of it would *merge* two unrelated clusters: only the colliding names get
replaced, and every base-only file survives — WAL segments stamped with a different system identifier
among them. That a merged directory happens to start proves nothing about what it contains. Both the
restore stage and the final stage `rm -rf /data/database /data/tiles` and recreate them
`renderer:renderer`, which is the base image's own ownership for `/data/*`; the cluster inside is
`postgres`-owned and carries that from the archive. The same hazard, and the same fix, applies to
`map-nominatim`.

**Bucketed layers.** At ~41G the data cannot ship as one layer, so the restore stage partitions `/data`
into 8 buckets of ≤8G by the same greedy first-fit used by `webarena/gitlab`. Task #53 tracks lifting
that duplicated algorithm into a shared `partition-tree.py`.

## Validation

- **In-build, structural**: PG-major match (above), the `planet-import-complete` marker present,
  cluster ownership `postgres:postgres`, and a 10G floor on `/data/database` so a truncated import
  fails the build instead of serving blank tiles.
- **In-container, behavioural**: the `HEALTHCHECK` fetches `/tile/0/0/0.png` — upstream's own
  readiness check — so the container reports healthy only once it has really rendered a tile.
- **Pre-push, behavioural**: `smoke` boots the image and polls `[service].healthcheck` — a real
  rendered tile — before the push step, this time from the host through the published port.

⚠️ The healthcheck renders zoom 0 (the whole world) from a cold 40G cluster on first request. If smoke
proves flaky, raise `poll_health`'s timeout rather than substituting a cheaper URL, which would stop
proving the thing worth proving.
