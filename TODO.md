# TODO — open items

Captured 2026-08-10 from the working agent's task list. Each entry preserves the full
context from the original task, since this file is now the only record.

## In progress

### Derived-inputs GHCR cache → authoritative digest-pinned input (finish the fleet migration)

DECIDED (James): oras artifacts, fail-by-default on a pinned miss, with the simplest possible
escape hatch (workflow_dispatch input `allow_derive_cache_miss` → `ALLOW_DERIVE_CACHE_MISS=1`)
for a GHCR outage. Migration is amortized "as we go" on legacy hits, never by re-downloading
upstream.

Shipped in PRs #21/#22/#23: `builder/stage-lib/derive-cache.sh`, `builder/derived-cache.lock`
(7 pinned refs), all seven cache sites converted, wikipedia opted out of migration via
`DCACHE_NO_MIGRATE=1`.

PROVEN IN PRODUCTION (run 31284811602, 2026-08-09): vwa-classifieds logged "derive: cache hit
(legacy), outputs extracted" and its GHCR tag `a2a794da92f6-r1` is now an oras artifact —
artifactType `application/vnd.unknown.artifact.v1`, 9 titled layers
(`classifieds_uploads.tar.part-00..08`), 76.92 GB, and NOTHING else. That last part is the
guard against the neighbour-sweep bug: `$dest` is the shared `DATASETS_DIR` and the migrated
entry contains only the files the legacy entry held. `webarena-wikipedia-derived:f12163513307-r1`
still has no artifactType, so the `DCACHE_NO_MIGRATE` opt-out held.

REMAINING before this closes: the other five (gitlab, reddit, shopping, shopping-admin, webshop)
must migrate the same way; then a subsequent run must be observed taking the oras path
(`DCACHE_HIT_FORMAT=oras`) rather than the legacy fallback. After the fleet converts:
re-resolve all seven digests, tighten `dcache_require` to a real digest comparison, and delete
the legacy-fallback branch in the read path.

### Build webarena-map image(s) (OSM tile + nominatim) — build only

Build map service images following upstream's `webarena-map-backend-boot-init.yaml` (saved at
`~/beep-scratch/browser-use-benchmarks/map/`). IN PROGRESS on branch `map-image` (worktree
`.worktrees/map-image`, off main, no commits yet). Read
`~/beep-scratch/browser-use-benchmarks/map/FINDINGS.md` FIRST — it has verified tar layouts and
corrects the `--strip-components` trap (upstream's 5 is right for nominatim, over-strips tile
which needs 4; moot if we extract to explicit destinations). sha256 pins being computed by a
background stream-hash (`map/hash-tars.sh` → `map/pins.txt`), ~110 min, zero disk cost;
`osm_dump.tar` already pinned =
`e6d58ad43e1da7530b7384522a4d0d30f59a44fdcadb8ad7c2e6695a43c585b1`. Next: author
`images/webarena/map-osrm` (smallest, 21.3G, no strip needed, profiles car/bike/foot at tar top
level) then map-tile then map-nominatim; build via CI dispatch at `--ref map-image`. Sandbox
disk is fine (415G free).

## Pending

### Security-patch dependency upgrade pass (after full version-parity fleet)

James's policy (2026-08-06): keep all benchmark images pinned at upstream version parity until
the whole fleet builds and we're convinced we're consistent with upstream sources. THEN upgrade
to security-patched dependency versions (starting with the 4 dependabot findings on the default
branch — 1 high, likely Magento composer.lock) and compare before/after for equivalence. Until
then: dependabot findings on pinned benchmark deps are accepted, not upgraded.

### Fleet-wide: search results with tied relevance order non-deterministically after a rebuild

Measured on webarena/reddit 2026-08-06: `/search?q=music` returns the same twelve results in
the same order except positions 8 and 9, which swap between the upstream container and the
rebuilt image. Both databases are identical (127,391 submissions, same ranked ID list, same
ts_rank values) and the two swapped rows are EXACTLY tied at ts_rank 0.7745836. Postmill's
search ORDER BY has no deterministic tiebreak, so tied rows follow the plan's physical order —
and a cluster restored from pg_dump is laid out differently from one that grew row by row.

Not a defect in the reddit image and not fixable there without changing upstream's query. But
it is FLEET-WIDE: shopping (Magento search), shopping-admin and classifieds (Osclass search)
all have relevance-ranked search, and any benchmark task whose expected answer depends on the
relative order of equally-ranked results is unstable across a rebuild.

What to decide (James): whether the benchmark harness should (a) accept any permutation within
a rank tie when scoring, (b) pin a deterministic secondary sort in each app (a real divergence
from upstream, applied consistently), or (c) accept the instability and document it
per-benchmark. Leaning (a) — it is a harness-side fix that changes no image and no upstream
code. Documented in `images/webarena/reddit/README.md`.

### Upstream drift detection: periodic content-length + last-modified probe (DEFERRED)

DEFERRED by James 2026-08-06: "put this drift detection feature at the end of the priority list
(these things don't change often so it can wait until we finish setting up all the
benchmarks)." Do NOT start before the benchmark fleet is built and published.

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

webshop's Dockerfile builds a Lucene index over all ~1.18M products in its last RUN:
`python -m pyserini.index.lucene --collection JsonCollection --input resources --index indexes
--threads 1 --storePositions --storeDocvectors --storeRaw`, preceded by
`convert_product_file_format.py`. Single-threaded, and rebuilt on every image build.

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

### gitlab: HTTP_PORT moves the container's internal listener; EXPOSE 8023 goes stale

Found 2026-08-08 while verifying the gitlab ownership fix.

gitlab is the ONLY fleet image whose internal listener follows HTTP_PORT. The entrypoint runs
`gitlab-ctl reconfigure`, which rewrites `external_url`, and nginx derives
`listen *:<HTTP_PORT>` from it. So the published and container ports must be EQUAL.

`-p 19023:8023` with `HTTP_PORT=19023` yields a container that reports healthy in-container and
is unreachable from the host. Everywhere else in the fleet HTTP_PORT describes only the
published side, which is what the contract's wording says — so gitlab quietly violates the
fleet's stated contract.

`EXPOSE 8023` in `images/webarena/gitlab/Dockerfile` is wrong whenever HTTP_PORT != 8023.

CI is currently SAFE: `images/webarena/compose.yml` uses HTTP_PORT "8023" + ports "8023:8023",
and `builder/docker.py` probes host reachability separately from in-container health, so the
smoke gate would catch a mismatch (same failure shape as #66).

Options: (a) document the equal-ports constraint and drop/parameterise EXPOSE; (b) make the
entrypoint keep nginx on a fixed internal port and let `external_url` carry only the published
address — needs checking whether GitLab tolerates the split, since links are baked from
`external_url`.

### Smoke gate cannot see a crashlooping runit service inside a healthy container

ADDRESSED by PR #20 (unmerged) — and the defect it describes turned out to be REAL and live,
not hypothetical.

Booting the published gitlab image with observe-only runit hooks caught sshd crashlooping: 193
failed starts in four minutes, "Missing privilege separation directory: /run/sshd", nothing
ever listening on 22, container reporting healthy throughout. It has been shipping that way.
runit restarting it forever is exactly what hid it from the smoke gate.

Two fixes in #20: `mkdir -p /run/sshd` in the entrypoint (`/run` does not persist, so the
Dockerfile alone would not do it), and the "any crashed service fails the whole container"
contract, which makes a crashed service exit the container — a state the smoke gate already
detects, so it no longer needs to see inside.

Keep open until #20 merges and a full-suite run confirms gitlab still builds and smokes green
with sshd up.

### partition-tree: the EXDEV cross-filesystem fallback is invisible in build logs

`move_preserving()` (`builder/stage-lib/partition-tree.py`, added in PR #14 / f394018) tries
`os.rename` and falls back to an ownership-preserving copy on `errno.EXDEV`. Neither branch
emits anything, so no build log can distinguish them.

Consequence: a green build is not evidence the fallback ever ran. In post-merge run
31308627334 classifieds and reddit both partitioned successfully, but whether either hit EXDEV
is unknowable from the logs — the fix's correctness rests entirely on the unit test.

That also means a future regression that silently stops taking the EXDEV branch (e.g. a
base-image change putting /staging on the same filesystem) would be indistinguishable from
today, and we would lose the coverage without noticing.

Suggested fix: emit one line to stderr the first time the fallback is taken, naming the
src/dest pair and the errno, and count them so the summary reports "N pieces moved
cross-filesystem". Keep it one line per run, not per piece — classifieds moves thousands.

Not urgent; the unit test proves the branch works. This is about observability, not correctness.
