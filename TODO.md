# TODO — open items

Captured 2026-08-10 from the working agent's task list; validated against the repo, GitHub,
and the live GHCR registry on 2026-08-10 (see "Resolved since capture" at the bottom for
items that were already done).

## In progress

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
(merged 2026-08-09); the migrations landed anyway. `webarena-wikipedia-derived:f12163513307-r1`
remains legacy BY DESIGN (`DCACHE_NO_MIGRATE=1` held; confirmed again in run 31293520004).

REMAINING before this closes:
1. Observe one real oras read (`cache hit (oras)` / `DCACHE_HIT_FORMAT=oras`). No run has
   taken it yet — every build since skips derive entirely via the prepare-outputs reuse stamp,
   so the oras read path is exercised only by tests. May need to force one by invalidating a
   reuse stamp.
2. Re-resolve the six migrated digests in `builder/derived-cache.lock` — the lock still holds
   the pre-migration legacy digests (only wikipedia's still matches the registry).
3. Tighten `dcache_require` to a real digest comparison (`derive-cache.sh` ~309-334 is still
   ref-only, deliberately per commit ad73a9d).
4. Delete the legacy-fallback branch in `dcache_pull` — only after wikipedia is re-derived and
   pushed as oras, since its entry is still legacy.

### Build webarena-map image(s) (OSM tile + nominatim + osrm) — build only

IN PROGRESS on branch `map-image` (worktree `.worktrees/map-image`, pushed to origin and in
sync, 3 commits): `images/webarena/map-osrm` and `images/webarena/map-tile` are authored and
committed (95b86ad, 07c94a4, fa4f0d1 — osrm serves all three routing profiles from one image;
tile server baked; both registered for smoke). Reference material at
`~/beep-scratch/browser-use-benchmarks/map/` (FINDINGS.md — verified tar layouts and the
`--strip-components` trap — plus SPEC.md and upstream's `webarena-map-backend-boot-init.yaml`).
All 4 sha256 pins are computed and recorded in `map/pins.txt` (hash job DONE; osm_dump.tar =
`e6d58ad43e1da7530b7384522a4d0d30f59a44fdcadb8ad7c2e6695a43c585b1`).

REMAINING: author `images/webarena/map-nominatim`; rebase the branch onto current main (it
forked before recent main commits); trigger the CI build via workflow dispatch at
`--ref map-image` (no CI runs against the branch yet); then open a PR.

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
5. MUST run on the SELF-HOSTED runner: the lab mirror URL (sandbox-1.lab.jshkol.com:8098) is
   not reachable from GitHub-hosted runners.
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
