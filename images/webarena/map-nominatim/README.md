# webarena/map-nominatim — Nominatim geocoding backend

Rebuilds the geocoding third of the WebArena map backend as a self-contained image. Upstream ships
this as an AWS AMI whose cloud-init downloads `nominatim_volumes.tar` at boot, unpacks it into
`/var/lib/docker/volumes`, and bind-mounts the `nominatim-data` volume at
`/var/lib/postgresql/14/main` (plus a `nominatim-flatnode` volume and `/opt/osm_dump`). This image
bakes the Postgres cluster in, so a pulled image geocodes with no external fetch and no import.

Behavioural reference: `webarena-map-backend-boot-init.yaml` (upstream cloud-init user-data).

## Provenance

| | |
|---|---|
| base image | `mediagis/nominatim:4.2` — the exact tag upstream pulls — pinned by **digest** `sha256:d0eae7b5…` |
| gazetteer | `nominatim_volumes.tar`, 124,774,901,760 bytes, from the public S3 bucket `webarena-map-server-data` |
| project data | `osm_dump.tar`, 1,878,691,840 bytes, same bucket |

The bucket is in **us-east-1**; the upstream yaml's own aws config says `us-east-2`, which is wrong.
No upstream checksum exists for either object, so both sha256 pins were **computed at import** in this
repo by streaming and hashing while checking the byte count against `Content-Length` in the same pass.

The tag is a real version, unlike the tile server's untagged `:latest`, so version parity is
straightforward here. The digest is pinned alongside it because the tag is mutable and the baked
cluster is a **PostgreSQL 14** data directory — `restore-stage.sh` asserts that major against the
base image, so a tag that moves to 15 fails the build instead of shipping a database that refuses
to start.

## What is inside the 116 GiB tar — and what this image ships

Measured by walking the archive's header chain with ranged GETs (each header states its own size, so
the next one's offset is computable; ~166 requests, no bulk download):

| member | size | shipped? |
|---|---:|---|
| `…/volumes/nominatim-data/_data/` — the Postgres cluster, 7,221 entries | **34.76 GiB** | yes |
| `…/volumes/nominatim-flatnode/_data/flatnode.file` — one file | **81.44 GiB** | **no** |

**The flatnode file is deliberately not baked in.** It is osm2pgsql's node-location cache: written
during the import, and read again only by a *later* import or by replication updates. Nothing on the
query path touches it. Three things make skipping it safe rather than hopeful:

- `/app/start.sh` runs replication only when `REPLICATION_URL` is set, and nothing here sets it.
- `/app/config.sh` writes `NOMINATIM_FLATNODE_FILE` into `/nominatim/.env` **only if
  `/nominatim/flatnode` exists**. This image does not create that directory, so the setting stays
  empty and Nominatim never looks for the file — the absence is configured, not stumbled into.
- The pre-push `smoke` step answers a real geocoding query, which is the behaviour this data does
  not participate in.

What it costs: a *future* re-import or a switch to continuous replication would have to rebuild the
node cache. That is a deliberate trade of 81 GiB of image, pull bandwidth and per-runner disk against
a capability the benchmark does not use. The bytes remain pinned in the tar, so nothing is lost — a
build that wants them adds a second member to the extract in `restore-stage.sh`.

## Decisions

**`--strip-components=7`, which is neither upstream's 5 nor the tile image's 4.** Upstream extracts
into `/var/lib/docker/volumes` and wants docker's volume layout (`nominatim-data/_data/…`)
reconstructed there, so it strips the 5 components of
`projects/metis2/docker/docker/volumes/`. This image extracts into the cluster path itself, so the
volume name and `_data` are two more components to drop. The three tars in this backend genuinely
differ in depth — see the sibling READMEs. **Count per tar; never copy the number.**

**The base image's own cluster is deleted before the COPY.** `mediagis/nominatim:4.2` ships an
`initdb`'d cluster at `/var/lib/postgresql/14/main` from its own build. Copying onto it would merge
two unrelated clusters: ours would overwrite the files whose names collide and leave every base-only
file in place, including a `global/pg_control` from a different system identifier. The Dockerfile
`rm -rf`s the directory and recreates it `postgres:postgres` mode `0700` — the mode matters, because
the bucket COPYs fill the directory but never set the mode of its root, and Postgres refuses to start
on a data directory with looser permissions.

**`postmaster.pid` is removed.** The volume was snapshotted from a container that had run, so the
archive carries a pid file naming a process and shared-memory segment from that host. Removing it is
the documented recovery for a copied data directory, and it is unambiguously safe in a build stage
where no postmaster exists.

**`osm_dump.tar` is folded into this image rather than becoming a fourth service** (1.75 GiB against
a 35 GiB image). It is an *input* to the geocoder, not a service.

**Extracted with `--strip-components=1`, which fixes a path upstream gets wrong.** The archive has its
own `osm_dump/` prefix; upstream extracts it with no strip into `/opt/osm_dump` and mounts that at
`/nominatim/data`, so the files land at `/nominatim/data/osm_dump/…` while `PBF_PATH` says
`/nominatim/data/us-northeast-latest.osm.pbf`. That path does not exist on upstream's own instance.
It never bites, because the only consumer is `/app/init.sh` and the import never re-runs — but it is
why upstream's setting cannot be copied verbatim and called verified. Stripping the prefix makes both
documented paths resolve.

**`PBF_PATH` is set even though nothing reads it.** `/app/config.sh` exits 1 unless exactly one of
`PBF_PATH`/`PBF_URL` is set — on *every* boot, including the one that skips the import. It is
load-bearing for startup, not for data.

## Validation

Three layers, deliberately separated:

- **In-build, structural**: PG-major match (above), the `import-finished` marker present, cluster
  ownership `postgres:postgres`, a 20 GiB floor on the cluster, and both `osm_dump` files present.
  The marker is the one that would otherwise be discovered the hard way: without it `/app/start.sh`
  runs the *full* import on every boot, and the only symptom is a container that never becomes
  healthy.
- **In-build, behavioural**: the restore stage **starts Postgres**, asserts a `nominatim` database
  exists, and requires `placex` to hold more than a million rows — structure alone cannot tell a real
  gazetteer from an empty cluster with the right file names. Starting it also does this snapshot's
  crash recovery **once at build time** rather than on every boot of every pulled image, and the
  stage then stops the cluster cleanly, so what ships is a clean shutdown rather than a crashed one.
- **Pre-push, behavioural**: `smoke` boots the image and polls `[service].healthcheck` — a real
  `/search` query — before the push step. An image that assembles but cannot geocode never reaches
  the registry.

## Ports

Container **8080**, published on **8085** — upstream's mapping (`-p 8085:8080`). Postgres 5432 is
deliberately not published; nothing outside the container reads it.
