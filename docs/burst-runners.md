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

Everything below lands on one volume unless the VM puts `/var/lib/docker` on a second one. The peak
is the sum of three things that coexist:

1. **`$HOME/benchmark-datasets`** — the derived parts pulled from GHCR (and, on a cache miss, the
   upstream archive as well).
2. **`/var/lib/docker`** — build-stage layers plus the final image. For images with a restore stage
   this is close to two copies of the restored tree, because the final stage `COPY --from=restore`s
   it. (The partitioner itself does not double anything: `partition-tree.py` renames within one
   filesystem, and only copies on EXDEV.)
3. **The smoke container's writable layer** — `bin/build smoke` boots the image *before* it is
   pushed, so this is part of peak, not a runtime-only concern.

| image | dataset / derived input | final image | peak disk (est.) | source of the sizes |
|---|---|---|---|---|
| **webarena/wikipedia** | 88.7 GiB of parts | ~89 GiB | **~270 GiB** | `images/webarena/wikipedia/README.md`, `split-zim.sh` |
| vwa/classifieds | ~73 GB of item photos | 77.8 GB | ~160 GiB | `images/vwa/classifieds/README.md` |
| webarena/shopping | ~50 GB media tar | ~48 GB | ~150 GiB | `images/webarena/shopping/derive-backup.sh` |
| webarena/gitlab | ~40 GB backup tar | ~45 GB | ~140 GiB | `images/webarena/gitlab/image.toml`, `derive-backup.sh` |
| webarena/reddit | 41.3 GB media tar | ~39 GB | ~130 GiB | `images/webarena/reddit/derive-backup.sh` |
| webarena/shopping-admin | ~3 GB | ~45 GB | ~100 GiB | `images/webarena/shopping-admin/Dockerfile` |
| webshop/server | ~19 GB | 15.8 GB | ~60 GiB | `images/webshop/server/README.md` |
| miniwob/server | <1 GB | small | ~20 GiB | — |

**Only the first column is measured.** The dataset and final-image sizes are recorded in the tree
from real builds; the peak-disk column is those numbers added up under the model above, not a
figure anyone has watched on a gauge. Treat it as a floor to size against, not a guarantee.

**Recommendation: a 400 GiB volume**, uniform across jobs unless burst can size per-job. That
clears wikipedia's ~270 GiB with room for the base images and the ~90 GiB the smoke container
writes, and every other image fits several times over.

### The two ways wikipedia gets worse

- **On a derived-cache miss** it needs the whole 88.7 GiB archive *beside* the 88.7 GiB of parts —
  `split-zim.sh` refuses to start a split it cannot finish and logs the shortfall. That pushes peak
  to roughly **360 GiB**, which 400 GiB still covers, but only just.
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
