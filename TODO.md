# TODO — open items

Captured 2026-08-10 from the working agent's task list; validated against the repo, GitHub,
and the live GHCR registry on 2026-08-10 (see "Resolved since capture" at the bottom for
items that were already done).

## In progress

### Build data path: what the media work found, and what is left to apply

Added 2026-08-14. Mirrors the session task list (`TaskList`); this file is the durable copy.

**Landed.** `[media]` routes an inert archive around the docker build: the CI host demuxes it
straight into one tar per image layer and the final stage `ADD`s them, so the tree never enters
a build stage. shopping merged as PR #51 — media phase 36 min → 6.5 min, whole build 1h52m →
1h20m, three passes over 45G down to one. reddit and classifieds are PR #52 (38.5G and 73G
routed off the build; staging buckets 7→2 and 12→2), which also carries:

- map-osrm (7.9G), map-nominatim's project data (1.75G) and map-tile's cluster (38.4G). The
  last two of those needed `[media].archive` to accept a pinned dataset and not only a prepare
  output — `download` has already verified its sha256 end to end, which is the stronger
  guarantee, not the weaker one. map-tile has no restore stage left at all.
- The demux now REFUSES an unpaddable spare bucket instead of emitting a zero-member tar.
  Docker does not recognise one as an archive, so `ADD` copies it verbatim and drops a literal
  `bucket-NN.tar` into the image — green build, wrong tree.
- `partition-tree.py` bounds a bucket by what its layer tar writes rather than by `du`, which
  is wrong in both directions (measured on 2,000-entry trees: tiny files, `du` over by 4×;
  block-aligned files, under by 512 B/entry; one 64 MB sparse file, `du` says 0 K against a
  real 65,540 K).
- Every remaining `COPY --from=datasets` of a read-once archive is a `--mount=type=bind`.

**The precondition, stated correctly.** Not "does the restore stage touch the tree" — that
excluded three images wrongly. It is "does the restore stage TRANSFORM the tree". map-tile
(`cat PG_VERSION`, `[ -f ]` marker, `stat`, `du`) and map-osrm (`[ -f ]` file list,
`stat -c %s`) are read-only and qualify. map-nominatim's cluster genuinely does not — it starts
Postgres, replays the snapshot's WAL, queries `placex` and shuts down cleanly, so what ships is
not what was extracted. gitlab does not either: `gitlab-backup` stores repositories as git
BUNDLES (verified in the pinned base image — `gitaly_backup.rb` runs `gitaly-backup` with
`-layout pointer`, and the binary carries `CreateBundleFromRefListRequest` /
`CreateRepositoryFromBundleRequest`), so the shipped repos are rebuilt from bundles: same
reachable objects and SHAs, different packfiles, no unreachable objects, no reflogs.

**Also landed, from the same pass.**

- Progress through the three phases that were mute for minutes at a time: nominatim's extract
  (tar `--checkpoint`), `partition-tree.py`'s measuring walk and move loop, and dataset
  sha256 verification — the last being the one that runs even on a full cache, ~180G on a map
  build that downloads nothing.
- `docs/build-data-path.md` — the measured `tar`/`ADD`/buildkit behaviour the data path is
  built around, every item of it a silent failure. Includes the audit-the-extractor trap.
- What nominatim's extract actually costs: **34.8G, not 116G**. The two volumes do not
  interleave (nominatim-data is members 0–7220, the flatnode volume is three members after
  it) and tar lseeks over a member it is not extracting when the archive is seekable —
  0.002s via `-f` against 3.4s through a pipe, on an 8G archive with a huge tail member. No
  streaming reader is needed; the only thing that would undo it is piping the archive.

**Settled — reviewed and accepted, no action pending.**

- map-nominatim ships **1.75 GiB of pure import input** in the final image:
  `us-northeast-latest.osm.pbf` (1,485,107,682 bytes) and `wikimedia-importance.sql.gz`
  (393,574,858), under `/nominatim/data`. Nothing on the query path reads it; the Dockerfile
  says so itself. The stated reasons are that `PBF_PATH` should name a file that exists and
  that a re-import is possible from the image alone. The first does not survive reading
  `/app/config.sh`, which checks the VARIABLE is set, not the file. The second is undercut by
  the 81.44 GiB flatnode file being deliberately absent — the pbf makes a re-import cheaper,
  not self-contained. It stays: the media conversion keeps exactly what ships today, and
  deleting it is a separate change nobody has asked for.
- `bucket-media.py` (267 lines + 280 of tests) has **no caller** — every `[media]` image sets
  `restore_needs_media = false`. Kept as the documented fallback for a future image that needs
  the tree in-build.
- shopping-admin has a media tar but it is 85M and its restore stage reads it. Not converted:
  it costs complexity and saves nothing.

**Known and deliberate, not a candidate.** wikipedia joins 88.7 GiB of parts into one file at
first boot (~632 s, roughly doubling runtime disk). libzim serves every article from split
parts but gives no full-text search at all; measured and documented in that image's README.

### Derived-inputs GHCR cache → authoritative digest-pinned input (finish the cleanup)

DECIDED (James): oras artifacts, fail-by-default on a pinned miss, with the simplest possible
escape hatch (workflow_dispatch input `allow_derive_cache_miss` → `ALLOW_DERIVE_CACHE_MISS=1`)
for a GHCR outage. Migration is amortized "as we go" on legacy hits, never by re-downloading
upstream.

Shipped in PRs #21/#22/#23: `builder/stage-lib/derive-cache.sh`, `builder/derived-cache.lock`
(7 pinned refs), all seven cache sites converted, wikipedia opted out of migration via
`DCACHE_NO_MIGRATE=1`.

MIGRATION WAVE IS DONE — all six eligible datasets converted during run 31284811602
(2026-08-08/09): vwa-classifieds, gitlab, reddit, shopping, shopping-admin, webshop are all
oras artifacts on GHCR now (verified against live manifests: artifactType
`application/vnd.unknown.artifact.v1`, push timestamps inside that run's window). That run
concluded "failure" only because of wikipedia transfer/nginx readiness issues, fixed in PR #24
(merged 2026-08-09); the migrations landed anyway.

WIKIPEDIA IS CONVERTED TOO (2026-08-11, branch `wikipedia-cache-oras`). It could never migrate
opportunistically — ~88 GB inside a 300-min CI budget — so it was converted out of band without
re-deriving: the legacy manifest's 13 blobs were fetched straight from GHCR, each verified
against its manifest digest, extracted, checked (12 parts == image.toml's outputs; sizes ==
the manifest; concatenated sha256 == the ZIM pin `f1216351…eea021`; both Xapian indexes still
whole inside one part), then re-pushed through `dcache_push`. The tag now resolves to
`sha256:7184991800ee212a1626a2fc021e3125411a53939f5d15000d5e4c6550f0ddd2`, artifactType
`application/vnd.unknown.artifact.v1`, 13 layers, sizes identical to the verified bytes. The
old legacy manifest `sha256:1de48463…d2c6` was the recovery handle for that republish; it was
deleted on 2026-08-12 with the rest of the pre-ORAS versions and is confirmed 404. There is no
second copy of this dataset anywhere now.

THE FIRST REAL ORAS READ HAPPENED, run 31483094508 (2026-08-11, branch
`wikipedia-pull-concurrency`): `split-zim: cache hit (oras format)`, 95 GB extracted and
verified against the manifest. It took four attempts to get there, and what it cost is worth
recording because it is not a derived-cache defect and will bite every large pull:

LONG TRANSFERS ARE INTERRUPTED PERIODICALLY IN CI. Nine transfer failures across three runs
(31460767854, 31464177083, 31483094508), every one of them
`stream error: PROTOCOL_ERROR; received from peer`, arriving on a regular period. The cause
is local to the CI environment, not oras, not GHCR, not the path: the same entry pulled clean
elsewhere in 1156 s, one blob fetched alone verified digest-exact in 206 s, and
`--concurrency 1` changed nothing. Not yet identified; until it is, every large pull pays
for it.

`dcache_pull` now survives it (PR #36): blob-by-blob fetch, skipping any layer whose size AND
sha256 already match, so an interruption costs one blob instead of the whole transfer. Run
31483094508 was interrupted in passes 1, 2 and 3 and still completed in ~40 min. Before that
change the retry loop could not converge at all: a clean pull needs ~19 min and interruptions
arrive every 10-15.

LEGACY READER DELETED (PR #35): the fallback branch in `dcache_pull`,
`dcache__migrate_legacy_hit`, `DCACHE_NO_MIGRATE` and the dead `FILTER` argument at all seven
call sites are gone; a tag that is not an artifact is now a loud failure rather than a miss (a
miss would send a caller off to re-derive over a format mismatch — up to ~24 h for wikipedia).
All seven lock digests were re-resolved live on 2026-08-11 and written into
`builder/derived-cache.lock`, header `Docker-Content-Digest` cross-checked against sha256 of
the raw manifest body, all seven `artifactType application/vnd.unknown.artifact.v1`. That
check is what licensed the deletion, and it closes the re-resolve item that stood here.

THE DIGEST LOCK IS NOW ENFORCED, not just recorded (PR #41). `dcache_pull` hashes the manifest
the tag resolved to and compares it against `builder/derived-cache.lock` before fetching a
single blob; a mismatch is fatal and names both digests. Since the manifest names every layer
by sha256 and the fetch already refuses a blob whose bytes disagree, a matching pin means every
byte extracted is the reviewed byte — the tag stops being the thing trusted. An unpinned ref
still pulls and prints the line to add. No CI escape hatch for a mismatch on purpose: the fix
is a reviewed edit to the lock, which is the whole reason it is checked in
(`ALLOW_DERIVE_CACHE_DIGEST_MISMATCH=1` exists for re-pinning by hand). Proven against a live
`registry:2` as well as fakes — including that sha256 over the raw manifest bytes is the same
digest the registry itself reports, which is the way this check would otherwise be subtly
wrong.

THE PRE-ORAS VERSIONS ARE GONE (2026-08-12, run by James — user-scoped packages can only be
deleted by their owner, so the bot account could inventory but never delete). There were no
separate legacy *repositories*: the legacy entries were older VERSIONS inside the same seven
`*-derived` packages — 31 versions, of which 24 were legacy (288.2 GB) and 7 pinned. Nothing
could reach them; every derive script builds its ref as `<hash>-$RECIPE`, so the four
un-suffixed tags (`6269a90527a6`, `6ff70f73bc80`, `2052430ee930`, `ad607557a79f`) were
unreachable by construction and the other 20 were untagged. Indexes were deleted before the
manifests they pointed at, so nothing was left dangling. Verified after: each package holds
exactly one version, all seven pinned digests still re-resolve live, and both wikipedia legacy
manifests return 404.

THIS SECTION IS CLOSED. What it leaves behind, and what to be careful of: there is now exactly
one copy of every derived dataset, pinned in `builder/derived-cache.lock` and enforced on both
the miss and the hit path. Wikipedia's is the one to respect — no second copy exists anywhere,
and re-deriving it is a ~24-hour, ~95 GB upstream fetch. Do not delete, re-tag or re-push any
pinned entry without putting the bytes somewhere else first.

### Build webarena-map image(s) (OSM tile + nominatim + osrm) — build only

IN PROGRESS on branch `map-image` (worktree `.worktrees/map-image`, rebased onto main and
pushed). All three images are authored, BUILT AND SMOKED LOCALLY — the first evidence any map image works
rather than assembles:

- **map-osrm** — green. `webarena-map-osrm: healthy and reachable at .../route/v1/driving/...`
- **map-tile** — green. Restore reports `cluster PG_VERSION=15, image ships postgresql-15`,
  `database 38G, tiles 4.0K`; smoke serves `/tile/0/0/0.png`; a hand-fetched z14 tile over
  Brackenridge PA is a real labelled 31 KB render, so the gazetteer is genuinely there.
- **map-nominatim** — green. The restore stage starts the baked cluster in-build and reports
  `cluster PG_VERSION=14, image ships postgresql-14`, `cluster 34G`, `placex holds at least
  1000001 rows`, then partitions it into 5 buckets; smoke serves `/search?q=carnegie+mellon...`,
  and a hand-run query returns `Carnegie Mellon University, Schenley Drive Extension, North
  Oakland, Pittsburgh, Allegheny County, 15213` at 40.4442,-79.9427 with importance 0.86 — so the
  wikimedia-importance data made it in too.

Three things the local builds found that no amount of reading would have:

1. Neither map image declared a `HEALTHCHECK`, and none of the three upstream bases declares one
   either, so `bin/build smoke` refused to boot them. Fixed, plus a unit test that now says so in
   70s instead of after a 13-minute build.
2. `osm_tile_server.tar` holds ONE volume, not two. `/data/tiles` is the rendered-tile cache and
   docker creates it empty at run time; the two `--volume=` flags in upstream's cloud-init are not
   evidence of two volumes in the archive.
3. Both tile and nominatim bases ship their own `initdb`'d cluster at the restore path, so
   restoring on top of one MERGES two unrelated clusters. Both now wipe first.

Reference material at `~/beep-scratch/browser-use-benchmarks/map/` (FINDINGS.md — verified tar
layouts and the `--strip-components` trap — plus SPEC.md and upstream's
`webarena-map-backend-boot-init.yaml`). All 4 sha256 pins are computed and recorded in
`map/pins.txt`.

The nominatim tar's contents were measured by walking its header chain with ranged GETs, no bulk
download: `nominatim-data` 34.76 GiB (the Postgres cluster, baked in) and `nominatim-flatnode`
81.44 GiB (one `flatnode.file` — osm2pgsql's node cache, deliberately not shipped; nothing on the
query path reads it).

REMAINING: open a PR. The CI dispatch is deliberately NOT done — these images are proven locally
instead, and the measured per-image peak disk is why: **129.4 GiB** for map-osrm (19.8 GiB tar),
**280.4 GiB** for map-tile (38.4 GiB), **520.7 GiB** for map-nominatim (116.2 GiB). Those figures
feed the burst volume sizing in `docs/burst-runners.md` on branch `burst-runners`, whose earlier
modelled numbers they correct by roughly 3x.

## Pending

### Security-patch dependency upgrade pass (after full version-parity fleet)

James's policy (2026-08-06): keep all benchmark images pinned at upstream version parity until
the whole fleet builds and we're convinced we're consistent with upstream sources. THEN upgrade
to security-patched dependency versions and compare before/after for equivalence. Until then:
dependabot findings on pinned benchmark deps are accepted, not upgraded.

Current state (2026-08-10): 8 open dependabot alerts, not the 4 the original note recorded —
the same four findings appear in BOTH `images/webarena/shopping/composer.lock` and
`images/webarena/shopping-admin/composer.lock` (alerts #1–#8). Per lockfile: 1 high
(`spomky-labs/otphp`), 2 medium (`spomky-labs/otphp`, `web-token/jwt-framework`), 1 low
(`symfony/http-client`). None fixed or dismissed.

### Fleet-wide: search results with tied relevance order non-deterministically after a rebuild

STILL OPEN, unchanged. Measured on webarena/reddit 2026-08-06: `/search?q=music` returns the
same twelve results in the same order except positions 8 and 9, which swap between the upstream
container and the rebuilt image. Both databases are identical (127,391 submissions, same ranked
ID list, same ts_rank values) and the two swapped rows are EXACTLY tied at ts_rank 0.7745836.
Postmill's search ORDER BY has no deterministic tiebreak, so tied rows follow the plan's
physical order — and a cluster restored from pg_dump is laid out differently from one that grew
row by row.

Not a defect in the reddit image and not fixable there without changing upstream's query. But
it is FLEET-WIDE: shopping (Magento search), shopping-admin and classifieds (Osclass search)
all have relevance-ranked search, and any benchmark task whose expected answer depends on the
relative order of equally-ranked results is unstable across a rebuild.

What to decide (James): whether the benchmark harness should (a) accept any permutation within
a rank tie when scoring, (b) pin a deterministic secondary sort in each app (a real divergence
from upstream, applied consistently), or (c) accept the instability and document it
per-benchmark. Leaning (a) — it is a harness-side fix that changes no image and no upstream
code. Documented in `images/webarena/reddit/README.md` (lines ~110-115); no harness-side
decision or commit yet.

### Upstream drift detection: periodic content-length + last-modified probe (DEFERRED)

STILL OPEN, not built (confirmed: `probe-upstream` appears nowhere but this file; no baseline
sidecar exists). DEFERRED by James 2026-08-06: "put this drift detection feature at the end of
the priority list (these things don't change often so it can wait until we finish setting up
all the benchmarks)." Do NOT start before the benchmark fleet is built and published.

WHY IT IS NEEDED: nothing in the system is coupled to upstream state. The derived cache never
expires and lazy fetch means a cache hit never contacts upstream, so a fetch only happens on a
RECIPE bump, a deliberate pin update, a GHCR blob disappearing, or a fresh runner that also
misses the cache. Upstream drift would therefore be noticed NEVER — or at an arbitrary future
moment as a sha256 mismatch presenting as a build failure. James confirms no cache expiry is
the right design; a periodic probe is the missing piece.

APPROVED MECHANISM (James): Content-Length + Last-Modified as a fuzzy check — "it's good enough
for rsync so it's good enough for us." Deliberately NOT ETag: metis is Apache, whose default
ETag is inode-size-mtime, so a byte-identical re-upload would false-positive.

MEASURED BASELINE (2026-08-06 ~22:00Z, live `curl -sIL`):
- `shopping_final_0712.tar` (metis): 67,575,898,112 / Fri, 08 Sep 2023 00:35:57 GMT
- `gitlab-populated-final-port8023.tar` (metis): 77,755,595,776 / Sun, 30 Jul 2023 15:09:50 GMT
- `postmill-populated-exposed-withimg.tar` (archive.org): 53,435,097,088 / Wed, 29 May 2024 22:24:15 GMT

All three lengths match our pinned copies and all Last-Modified predate our salvage ⇒ no drift
as of that measurement.

DESIGN NOTES (worked out, not built):
1. Baseline is PER-URL, not per-dataset — the lab mirror is a copy, so its Last-Modified is the
   copy time and will legitimately differ from upstream's. Store as a committed JSON sidecar
   keyed by URL so drift shows up as a git diff.
2. THREE states, never a bool: match / changed / unreachable / new. A flaky mirror must not
   read as "upstream changed", and must not read as "verified unchanged" either. metis is known
   flaky.
3. Redirect handling is load-bearing: archive.org 302s to a node and the FIRST response carries
   `content-length: 0`, which would look like a catastrophic shrink. Parse the LAST response's
   headers.
4. Two tiers: tier 1 = HEAD compare (seconds, scheduled, never in the build path); tier 2 =
   full download + sha256, the only authoritative check, hours, triggered only when tier 1
   changes.
5. MUST run on the SELF-HOSTED runner: the lab mirror is not reachable from GitHub-hosted
   runners.
6. Suggested CLI shape: `bin/build probe-upstream [target] [--update]`, `--update` rewrites the
   baseline as a deliberate committed act; exit nonzero on drift.

STILL OPEN when this is picked up: where a tier-1 change REPORTS to (scheduled workflow that
opens an issue vs. one that fails a check). A line in a log nobody reads is not detection.

Full context: `~/beep-scratch/browser-use-benchmarks/dataset-mirror-and-caching.md`

### Cache webshop's Lucene index (move the pyserini build into the prepare step)

STILL OPEN, unchanged — note the image now lives at `images/webshop/server/` (not
`images/webarena/webshop`). The Dockerfile still builds the Lucene index over all ~1.18M
products in a RUN at image-build time (`images/webshop/server/Dockerfile:93-105`,
`python -m pyserini.index.lucene --collection JsonCollection --input resources --index indexes
--threads 1 --storePositions --storeDocvectors --storeRaw`, preceded by
`convert_product_file_format.py`; ~290s per its README). `image.toml:70` explicitly documents
that the index "is NOT cached" by derive-backup.sh.

Commit cb62977 (wave-bc) cached webshop's dataset INPUTS but deliberately not the index —
caching inputs is not caching the derived artifact. This is the true analogue of what
gitlab/shopping/reddit/classifieds derive.

Shape: move the index build into `derive-backup.sh` (build a `--target` stage,
`docker create`/`export` the index dir, tar + push to GHCR), and have the Dockerfile COPY the
index from datasets. Key it on `PREPARE_INPUTS_DIGEST` + RECIPE, same as the input cache — the
index is a pure function of the three JSONs plus the pyserini version.

Motivating cost: landing anything in `builder/` triggers a full-fleet rebuild (build.yml:
"shared build inputs changed: building all"), so this index rebuild is paid repeatedly, not once.

Requires a real verify-by-running: build + smoke webshop before and after, and confirm the
served app returns identical search results for a fixed query.

### partition-tree: the EXDEV cross-filesystem fallback is invisible in build logs

STILL OPEN, unchanged. `move_preserving()` (`builder/stage-lib/partition-tree.py:179-189`)
tries `os.rename` and falls back to an ownership-preserving copy on `errno.EXDEV`. Neither
branch emits anything, so no build log can distinguish them. (Commit d4de67b fixed the
ownership bug on that path but added no visibility.)

Consequence: a green build is not evidence the fallback ever ran, and a future regression that
silently stops taking the EXDEV branch (e.g. a base-image change putting /staging on the same
filesystem) would be indistinguishable from today.

Suggested fix: emit one line to stderr the first time the fallback is taken, naming the
src/dest pair and the errno, and count them so the summary reports "N pieces moved
cross-filesystem". Keep it one line per run, not per piece — classifieds moves thousands.

Not urgent; the unit test proves the branch works. This is about observability, not correctness.

## Resolved since capture (verified 2026-08-10)

- **Smoke gate blind to crashlooping runit services / gitlab sshd crashloop**: PR #20 MERGED
  2026-08-08. `mkdir -p /run/sshd` is in `images/webarena/gitlab/entrypoint.sh:106`; the
  "any crashed service fails its container" contract is implemented (gitlab runit `finish`
  hooks + `arm-services.sh`; shared `builder/stage-lib/run-services.sh` elsewhere), and was
  extended by PR #32 (a stack that never finishes booting fails the container) and PR #31
  (puma moved off 8080, colliding port refused). gitlab built and smoked green post-merge
  (run 31347169479); the rest of the fleet built green the same day (run 31345200709).
- **gitlab HTTP_PORT moves the internal listener / stale EXPOSE 8023**: resolved via
  option (a) — the constraint is documented and runtime-guarded in
  `images/webarena/gitlab/entrypoint.sh` (lines 29-44, 57-64, and 77-87's hard refusal of a
  port collision, from PR #31). Internal port was NOT split from external_url. Only cosmetic
  residue: the `EXPOSE 8023` line at `images/webarena/gitlab/Dockerfile:53` carries no caveat.
