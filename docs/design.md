# browser-use-benchmarks — design (2026-08-05, James-approved brainstorm)

A small public GitHub project (`shkolnik/browser-use-benchmarks`) whose sole job is to be a
**builder service** for runnable benchmark-server docker images: build them reproducibly, push
them to a container registry, stay trivially maintainable. It replaces the earlier one-off
sandbox-local image builds (lost in a docker cleanup; the direct cause of the sandbox's disk
blowout) with something reusable and public.

## Decisions made (James, 2026-08-05)

- **Scope:** the full set previously built — MiniWoB++, WebShop, WebArena's per-site services.
  (The exact prior image list was lost with the sandbox; reconstruct from upstream benchmark
  docs and flag any uncertainty.)
- **Builds run on a self-hosted GitHub Actions runner** on James's hardware — hosted runners
  cannot hold >100G images. CI-driven, auditable pushes.
- **Registry: GHCR primary**; the size probe (below) pushes to **both GHCR and Docker Hub** for
  information. Neither documents a hard image-size limit; we find out empirically.
- **Packaging: one image per service** (smallest useful units — deliberate, given size), plus a
  per-benchmark `compose.yml` as the runnable unit. A root all-benchmarks compose is optional
  later; not v1.
- **Datasets are downloaded OUTSIDE `docker build`** by the driver (resume/retry/mirror logic is
  hard to do well inside a build), then `COPY`d in from the local filesystem.
- **Maintainability litmus (binding):** removing a benchmark service = delete one cohesive
  directory + at most one or two edits elsewhere; adding one = create a directory. The design
  beats this: discovery is by glob, so there are **no root index files**.
- James creates the repo and adds `shkolnik-beep` as collaborator (his username is `shkolnik`).

## Repo layout

```
bin/build                     # the one driver: download | build | push | smoke
images/
  <benchmark>/
    compose.yml               # the benchmark's runnable unit
    <service>/
      image.toml              # tag, dataset URLs + mirrors + sha256s, ports, healthcheck
      Dockerfile              # COPYs datasets from the supplementary build context; no network
      ...service-specific setup files
datasets/                     # git-ignored shared download cache
docs/registry-limits.md       # findings from the size probe (Milestone 0)
.github/workflows/build.yml   # matrix discovered by globbing images/*/*/
README.md
```

- Remove a benchmark: `git rm -r images/<benchmark>/`.
- Remove one service: delete its subdir + its block in the benchmark's `compose.yml`.
- Add either: create the directory; driver and CI matrix discover it by glob.

## The driver (`bin/build`)

One dependency-light script (bash or Python stdlib — implementer's choice, whichever keeps the
retry logic clearest) with four subcommands, each taking an image dir (or `all`):

1. **download** — for each dataset in `image.toml`: if a verified copy exists in `datasets/`,
   skip; else `curl -C -` resume-download in a retry loop rotating through the mirror list,
   then verify the **mandatory** sha256. Failure is loud (exact URL, byte count, which mirrors
   were tried); a file that fails verification is quarantined (renamed aside), never handed to
   docker. **Datasets marked `prepare_input = true` are skipped here** — see "Lazy
   prepare-input fetching" below.
2. **build** — `docker build` with the verified datasets exposed as a supplementary build
   context (`--build-context datasets=...`); Dockerfiles stay dumb (`COPY --from=datasets`).
   No network access needed by the build itself.
3. **push** — tag `ghcr.io/shkolnik/<benchmark>-<service>:<yyyymmdd>.<shortsha>` + `latest`;
   `--registry` flag overrides the prefix (used for the Docker Hub probe).
4. **smoke** — `docker compose up` the benchmark, poll each service's manifest-declared
   healthcheck URL until healthy or timeout, `compose down`. Exit code is the verdict.

`image.toml` is the entire per-image configuration surface: image name, datasets (URLs +
mirrors + sha256 + size), exposed ports, healthcheck path, and any build args.

### Lazy prepare-input fetching

A `[prepare]` image derives its real build inputs from a huge upstream docker-save tar and
caches the *derived* artifacts in GHCR. The tar therefore only matters when that cache misses.
Marking it `prepare_input = true` takes it out of the **download** step; the derive script
fetches it itself, on its cache-miss path only, via
`"$REPO_ROOT/bin/build" download --prepare-inputs "$IMAGE" --datasets-dir "$DATASETS_DIR"`
(`REPO_ROOT` and `IMAGE` are exported by `run_prepare`).

Without this the ordering defeats the cache: **download** fetches every declared dataset before
**build** ever runs the script that consults GHCR, so a cold runner pulled e.g. shopping's
67.6 GB tar and then never opened it. The win is not bytes saved — it is that the common path
(cache hit) stops depending on a ~3.6 MB/s flaky university mirror and reads from GHCR instead.

A dataset marked `prepare_input` with no `[prepare]` section is a load error: nothing would ever
download it. Note that the count of upstream fetches, not their size, is the metric this is
optimizing; it should trend to zero as derived caches stay valid.

### Cache invalidation: the pin IS the key

There is no time-based expiry, and none is needed — the derived cache is content-addressed.
Its GHCR tag is `<first 12 of the upstream sha256>-<RECIPE>`, so:

- **Upstream republishes different bytes.** The fetch verifies against `image.toml`'s mandatory
  sha256, mismatches, quarantines the file and exits. Changed upstream data can never be
  derived from silently; adopting it is a deliberate act.
- **We deliberately adopt new upstream data.** Updating the manifest's sha256 changes the cache
  tag (GHCR miss) *and* the provenance stamp (on-disk artifacts stranded), so both caches
  invalidate on their own.
- **The recipe changes.** Bump `RECIPE`; same effect, and it strands the previous revision's
  entries rather than overwriting them.

The pin therefore has exactly ONE source of truth: `image.toml`. `run_prepare` exports it as
`PREPARE_INPUT_SHA256` and the derive script uses that — it must never hardcode its own copy.
Two copies is not redundancy, it is a silent-wrong-data hole: update the manifest alone and the
downloaded bytes are new while the cache tag and the stamp both still describe the old tar, so
the derive is skipped and the build uses artifacts derived from data it no longer has. At most
one `prepare_input` dataset per image is allowed, because a second would not be represented in
that key.

Note what lazy fetching does NOT change: on a cache hit we never contact upstream, so upstream
drift is not *detected* until the next miss. That is a detection latency, not a correctness
problem — the pin means we would refuse the drifted bytes anyway.

## CI (`.github/workflows/build.yml`)

- Runs on the self-hosted runner (`runs-on: [self-hosted, benchmark-builder]`).
- Triggers: `workflow_dispatch` with an image/benchmark/all choice; on push, build only images
  whose directories changed.
- Job = matrix over discovered image dirs → `bin/build download && build && push`; smoke runs
  in CI for small benchmarks, manually for the giants.
- `datasets/` lives on the runner host and persists across runs (the cache IS the
  flakiness mitigation); a periodic prune is manual, not automated (v1).
- Keep this runner's disk contention away from the beep runner (separate label; ideally
  separate disk budget).

## Milestone 0 — the registry size probe (do FIRST)

Before any real 100G build: generate a synthetic image of **incompressible** (random) data,
~120G split across ~10G layers (layer size is a deliberate knob — chunked `COPY`s), and push it
to GHCR and Docker Hub. Record in `docs/registry-limits.md`: accepted/rejected, per-layer
errors, push time, whether resumable, and pull-back verification. Outcome decides whether real
images need layer-chunking and which registry is primary-viable. Delete the probe image from
the registry afterward.

## Error handling / principles

- Fail-loud everywhere (beep house rule carries over): no partial downloads passed onward, no
  silently-skipped datasets, checksum mismatches stop the build naming the file.
- Reproducibility over convenience: a Dockerfile never fetches from the network; every input is
  either in git or a checksummed dataset.
- YAGNI: no build framework, no root indexes, no automated cache pruning, no multi-arch (x86
  only — matches the runner and the consumers) in v1.

## Milestones

- **M0:** repo skeleton + driver + synthetic size probe → `docs/registry-limits.md`.
- **M1:** MiniWoB++ (tiny; proves the whole path end to end including compose + smoke + GHCR).
- **M2:** WebShop.
- **M3:** WebArena services, one directory at a time, informed by M0's layering findings.

## Open items

- Reconstruct the exact prior image list (upstream docs; James may remember specifics).
- Docker Hub account/namespace for the probe push (James's call if a new account is needed).
- Registry cleanup policy for superseded huge tags (GHCR storage is free for public but
  politeness/quota unknowns — note findings during M0).

## Deployment target note (James, 2026-08-05)

The built containers will likely run directly on James's Synology NAS (low workload, high disk).
Consequences: per-benchmark `compose.yml` is the deployment interface (Container Manager runs
compose natively) — keep host ports unique across benchmarks and pin versioned tags in compose,
not bare `latest`; RAM was upgraded to 32G (2026-08-05) so the full set fits comfortably; if registries
reject the huge images, `docker save | ssh` to the NAS is the fallback transport, so registry
limits cost convenience, not feasibility.

## GHCR package topology (2026-08-05)

One package per service image (`<benchmark>-<service>`), all fed from this repo. Dockerfile-built
images carry `org.opencontainers.image.source` pointing here, so GHCR auto-links each package to the
repo and inherits its (public) visibility on first push. The four WebArena `docker-save` images
cannot carry the label (their config is upstream's, and we only re-tag), so those packages need a
one-time manual visibility-flip/repo-link in the GitHub UI after their first push — visibility is
per package and defaults to private without the label.
