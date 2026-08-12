# Building on burst ephemeral runners

`burst.toml` + `runs-on: [self-hosted, burst]` in `.github/workflows/build.yml` move the
`build-images` matrix off the single home runner and onto one ephemeral AWS VM per matrix job
([burst-actions](https://github.com/shkolnik/burst-actions)).

The change that matters is the deletion of `max-parallel: 1`. That line existed because every
image shared one disk; with a single-tenant VM per job it is meaningless, and without it the
fleet's wall-clock stops being the **sum** of the image build times and becomes the **slowest
single image**. Everything else here is a consequence of that move.

## What the build loses

- **The datasets cache.** `--datasets-dir "$HOME/benchmark-datasets"` starts empty on every job.
- **The docker layer cache.** Every build is a cold build.
- **`bin/build clean` / `docker builder prune` stop mattering.** They are kept — they cost seconds
  and they are what keeps the workflow correct if a job ever lands on the home runner again.

What it does *not* lose is the expensive part. The costly inputs are not rebuilt from upstream on a
cold runner; they are pulled from the GHCR derived-inputs cache, pinned digest-for-digest in
`builder/derived-cache.lock`. That is the difference between a cold wikipedia build taking about an
hour and taking about a day (see below). **The derived cache is what makes ephemeral runners
affordable at all**, so a burst run with `allow_derive_cache_miss` set is not a cheap retry — it is
the one configuration that can turn a 300-minute timeout into a real risk.

## Disk: the number burst needs

Everything below lands on one volume unless the VM puts `/var/lib/docker` on a second one.

**The first version of this document modelled the peak as roughly two copies of the data and was
wrong by a factor of three.** It has since been measured, by sampling free space every 20s across a
full `download` → `build` → `smoke` of two images on an otherwise idle host:

| image | input tar | restored tree | measured peak |
|---|---:|---:|---:|
| webarena/map-osrm | 19.82 GiB | 19.8 GiB | **129.4 GiB** |
| webarena/map-tile | 38.45 GiB | 38 GiB | **280.4 GiB** |
| webarena/map-nominatim | 116.21 GiB | 34.8 GiB | **520.7 GiB** |

Each run started from a pruned builder. (The map-tile figure carries the cached context of one failed
build attempt that preceded the good one; a clean run is nearer 240 GiB.)

Two copies of the archive plus about four of the restored tree fits all three within ~10%:
`2 × 19.8 + 4 × 19.8 = 119` (measured 129), `2 × 38.5 + 4 × 38 = 229` (measured 240 clean),
`2 × 116.2 + 4 × 34.8 = 372` (measured 521 — the outlier, and the one to size against; a 116 GiB
context is where buildkit's own accounting stops being a rounding error).

Where it goes — five copies of the same bytes coexist, not two:

1. **`--datasets-dir`** — the verified archive itself.
2. **The buildkit context copy** — `COPY --from=datasets <tar>` writes a second copy into the build
   cache before the restore stage can touch it.
3. **The restore stage's extracted tree.**
4. **The final stage's layers**, because it `COPY --from=restore`s that tree. (The partitioner adds
   nothing: `partition-tree.py` renames within one filesystem, and only copies on EXDEV.)
5. **The unpacked image**, written again by the exporter — and on this fleet's images `docker system
   df` shows the image store and the build cache holding it simultaneously.

Plus **the smoke container's writable layer**: `bin/build smoke` boots the image *before* it is
pushed, so that is part of peak, not a runtime-only concern.

Nothing is reclaimed until the job ends. `docker builder prune -af` between images is what brings a
shared runner back down, and it is exactly what a per-job VM does not need to care about — but it is
also why the peak below is a **per-job** number, not a fleet number.

| image | dataset / derived input | final image | peak disk | source |
|---|---|---|---|---|
| **webarena/map-nominatim** | 116.2 GiB tar (34.8 GiB restored) | ~36 GB | **520.7 GiB, measured** | sampled every 20s |
| webarena/map-tile | 38.45 GiB tar | ~40 GB | **280.4 GiB, measured** | sampled every 20s |
| webarena/map-osrm | 19.82 GiB tar | ~21 GB | **129.4 GiB, measured** | sampled every 20s |
| webarena/wikipedia | 88.7 GiB of parts | ~89 GiB | ~530 GiB, modelled | `images/webarena/wikipedia/README.md`, `split-zim.sh` |
| vwa/classifieds | ~73 GB of item photos | 77.8 GB | ~410 GiB, modelled | `images/vwa/classifieds/README.md` |
| webarena/shopping | ~50 GB media tar | ~48 GB | ~280 GiB, modelled | `images/webarena/shopping/derive-backup.sh` |
| webarena/reddit | 41.3 GB media tar | ~39 GB | ~230 GiB, modelled | `images/webarena/reddit/derive-backup.sh` |
| webarena/gitlab | ~40 GB backup tar | ~45 GB | ~225 GiB, modelled | `images/webarena/gitlab/image.toml`, `derive-backup.sh` |
| webarena/shopping-admin | ~3 GB | ~45 GB | ~140 GiB, modelled | `images/webarena/shopping-admin/Dockerfile` |
| webshop/server | ~19 GB | 15.8 GB | ~110 GiB, modelled | `images/webshop/server/README.md` |
| miniwob/server | <1 GB | small | ~20 GiB, modelled | — |

**Three rows are measured; the rest are the same model applied to their inputs** (`2 × archive +
4 × restored tree`, rounded up). Every image in the fleet has the same shape — `COPY --from=datasets`
a large archive, restore it in a build stage, `COPY --from=restore` it into the final one — which is
why the model transfers, but a modelled row is still a floor to size against, not a guarantee.
`shopping-admin` is the one whose peak is driven by its final image rather than its input.

**Recommendation: a 750 GiB volume**, uniform across jobs unless burst can size per-job. That clears
the measured worst case (nominatim, 521 GiB) with real headroom, and it is deliberately not a snug
fit: the model under-predicted that build by 40%, so the margin is covering the part of this that is
still not understood. 400 GiB — the figure this document carried before anything was measured — would
have failed on three of the eleven images.

If burst can size per-job, the cheap version is **250 GiB for everything except `map-nominatim`,
`wikipedia` and `classifieds`**, which take the 750.

### The two ways wikipedia gets worse

- **On a derived-cache miss** it needs the whole 88.7 GiB archive *beside* the 88.7 GiB of parts —
  `split-zim.sh` refuses to start a split it cannot finish and logs the shortfall. Under the measured
  model that pushes its peak past **600 GiB**, which the 750 still covers and the old 400 did not.
- **The fetch itself.** A cold fetch from the public mirrors was measured at ~1.05 MB/s to the home
  runner — run 31256297478 moved its disk 264G → 268G in 65 minutes, i.e. about **24 hours** for the
  file. `timeout-minutes: 300` would kill it first. From AWS the mirrors may well be faster; nobody
  has measured that, and the derived cache means nobody should have to.

### Memory, secondarily

Not the binding constraint, but two jobs are not comfortable on a small instance: `webshop/server`
was OOM-killed under `docker run -m 16g` before its index rework (peak ~19 GiB RSS, now ~0.8 GiB),
and `webarena/gitlab`'s derive boots a full GitLab omnibus container to take a backup. Anything at
or above ~16 GiB of RAM is safe for the fleet as it stands.

## What was deliberately not changed

- **`concurrency: build-images` stays.** It never serialized the matrix — `max-parallel` did — so
  keeping it costs the fan-out nothing. Its original rationale (two runs racing on one shared
  datasets cache) does die with per-job VMs, but a second one does not: two runs of the same ref
  would each push the same image to `:latest` and then attest whatever their own `docker inspect`
  resolved locally, so the loser can stamp a provenance attestation onto a digest that `:latest` no
  longer points at. One run at a time is the cheap fix. Delete the block if run-level parallelism
  turns out to be worth solving that properly.
- **`discover` stays on the home runner.** It is a checkout, a `git diff` and one `gh` call; a VM
  boot would cost more than the job. Note that burst VMs also carry the implicit `self-hosted`
  label, so an idle one could pick it up — harmless, and burst will never *boot* a VM for it, since
  only `burst`-labeled jobs are counted.
- **`timeout-minutes: 300` stays.** Confirmed fine by the burst side, which has its own TTL kill.

## Before the first live run

Owned by the burst operator, not by this repo: docker in the AMI (+ rebake), a PAT covering this
repo, and the fork-approval repo setting verified. Until then, jobs labeled `burst` simply sit
queued — which is also why this branch should not land on `main`: merging it would leave every push
to `main` queued behind a runner that does not exist yet.
