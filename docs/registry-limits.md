# M0 registry-size probe — findings

Purpose: find out empirically whether GHCR and/or Docker Hub impose a practical size limit
(per-layer or total-image) before we push real, large benchmark images (WebArena services in
particular can be very large). Neither registry documents a hard limit as of this writing.

## How to run the probe

```
bin/build download probe                                          # no-op (no datasets)
images/probe/synthetic/generate-dataset.sh datasets/ 12 10         # 12 chunks x 10G = 120G
images/probe/synthetic/generate-dataset.sh datasets/ 12 10          # (idempotent — reruns skip existing chunks)
docker build --build-context datasets=datasets \
  -f images/probe/synthetic/Dockerfile.generated \
  -t <registry>/probe-synthetic:probe images/probe/synthetic
docker push <registry>/probe-synthetic:probe
```

Run once against `ghcr.io/shkolnik` and once against `docker.io/<namespace>` (`--registry` on
`bin/build push`, or `docker push` directly as above for the generated-Dockerfile variant, since
`bin/build build` uses the small committed default `Dockerfile`, not `Dockerfile.generated`).

`bin/build smoke probe` is N/A and fails loud (no `[service].healthcheck` in `image.toml`) — the
probe is not a runnable service, only a data-shape stress test. That is intentional; see
`images/probe/synthetic/image.toml`.

## Results

| Registry | total size | chunk size | result | per-layer errors | push wall-time | pull-back verified | date |
|---|---|---|---|---|---|---|---|
| ghcr.io (private, free-tier `shkolnik-beep`) | 20.01 GiB (g20) | 10.003 GiB | ACCEPTED | none | — | — | 2026-08-05 |
| ghcr.io (same) | 40.01 GiB (g40) | 10.003 GiB | ACCEPTED | none | — | — | 2026-08-05 |
| ghcr.io (same) | 60.02 GiB (g60) | 10.003 GiB | ACCEPTED | none | — | — | 2026-08-05 |
| ghcr.io (then flipped public) | 80.03 GiB (g80) | 10.003 GiB | ACCEPTED | none | — | YES (Synology, anonymous) | 2026-08-05 |
| ghcr.io (public) | **100.03 GiB (g100)** | 10.003 GiB | ACCEPTED | none | ~50 min (build+push rung) | — | 2026-08-05 |
| docker.io (public repo, free account `jshkol`) | 10.005 GiB (1 chunk) | **10.003 GiB** | ACCEPTED | none | 185 s | YES (anonymous, registry API) | 2026-08-09 |

**Conclusion:** GHCR accepts ≥100 GiB single images with ~10 GiB layers on a free-tier account,
private or public, with anonymous pull proven end-to-end at 80 GiB. Every size in our benchmark
compilation (largest: gitlab ~73 GiB) fits with ≥20% headroom. All rung sizes above are
registry-side ground truth via `tools/ghcr-manifest-size.py`, not local estimates. Staircase
deliberately cut after g100 (see below); GHCR's actual ceiling was not reached.

## Docker Hub layer probe (2026-08-09, verified live)

The Docker Hub arm of the probe above was designed on 2026-08-05 but never run, so every
Docker Hub number in the research findings below stayed REPORTED. This closes the layer half of
that gap. Motivation was concrete: mirroring the fleet is a straight copy if Docker Hub takes
~10 GiB layers, and a full rechunk of every image if it caps at the S3-multipart 5 GB.

- Target `docker.io/jshkol/benchmark-test:probe-10g`, **public repo on a free account**.
- One `busybox` base plus one `COPY` of a single 10 GiB `/dev/urandom` chunk — incompressible, so
  the compressed layer is the same size as the source. Same 10.003 GiB rung GHCR accepted.
- **ACCEPTED.** `docker push` exit 0 in 185 s (~58 MB/s).
- Registry-side truth, not the client's word: the amd64 manifest carries one layer of
  **10,740,695,364 bytes = 10.003 GiB**, alongside busybox's 2.2 MB.
- Retrievable, not merely accepted: anonymous `HEAD` on the blob returns 200 with
  `content-length: 10740695364`, and a ranged GET of the final 16 bytes returns 206 — the blob is
  stored whole and addressable end to end.

**Conclusion:** the 5 GB S3-multipart ceiling does NOT bind Docker Hub. Our largest real layer is
wikipedia's 9.66 GB (8.996 GiB); the accepted probe layer exceeds it by ~11%, so mirroring needs
**no rechunking**. A free account and a public repo are sufficient.

**Still unverified, and the reason mirroring is not yet a decision:** the *total* caps. Docker
Hub's cited 100 GB limit remains community lore, and it is ambiguous between per-image and
per-account. Both matter to us — the fleet is ~280 GB compressed and wikipedia alone is 87.9 GB,
which would sit inside a 100 GB per-image cap with only ~12% headroom. Settling that needs a
~90 GB staircase (per-image) or a full-fleet push (per-account); neither was run.

### Local smoke-scale verification (2 x 1G, this sandbox, 2026-08-05)

Verified the driver mechanics work end-to-end at small scale (see Task 7 commit): 2 x 1G chunks
generated into a temp `datasets/` dir, built via the committed default `Dockerfile` (single COPY
layer) against a local `registry:2` container, pushed, and `docker pull`ed back successfully.
This is NOT the registry-size probe itself (that needs the full ~120G run against real GHCR /
Docker Hub) — it only confirms the plumbing (`--build-context`, chunk generation, push/pull
round-trip) is sound before spending real time/bandwidth on the full-scale run.

## Research findings (web sweep, 2026-08-05 — verified-vs-reported flagged inline)

- GHCR: 10 GB/compressed layer (VERIFIED, GitHub docs); 10-minute upload timeout whose scope
  (whole-push vs per-request) is ambiguous — a REPORTED 7.8 GiB push died at manifest
  finalization at exactly 10 min. If whole-push, 120G needs ~1.6 Gbit/s sustained.
- Largest verified public-registry push found anywhere: ~25 GiB via 7–8 GB layers. Nothing at
  our 100G+ scale is documented as succeeding — our probe exits the tested envelope.
- Docker Hub's cited 100 GB total cap: REPORTED community lore only, not in Docker's docs.
  Still unverified as of 2026-08-09, and ambiguous between per-image and per-account.
  The ~10 GB layer ceiling across registries is an inherited S3 5 GB-multipart artifact.
  **Superseded for Docker Hub on 2026-08-09** — a 10.003 GiB layer was accepted and read back
  (see the Docker Hub layer probe above), so the 5 GB figure does not bind there.
- docker push has NO intra-layer resume (a failed 9 GB layer re-pushes from zero) and pushes
  5 layers concurrently by default — prefer ~8 GB layers; consider --max-concurrent-uploads
  on slow uplinks.
- WebArena upstream deliberately does NOT registry-distribute its big images: docker-save tars
  over Google Drive/archive.org/CMU HTTP + an AWS AMI. The salvaged tars ARE their chosen format.
- Alternatives if registries reject: (a) code-image + runtime dataset download (registry ships
  only small service images), (b) ORAS OCI artifacts (~8 GB layers, same registries),
  (c) self-hosted Zot registry or docker save|ssh directly to the NAS — the NAS being the only
  consumer makes this a legitimate primary path, not a fallback.
- Compression: zstd for compressible layers (faster + better decompress), zstd -1..3 for
  incompressible shards (ratio is zero either way; don't burn CPU). Don't squash — per-layer
  dedup across dataset versions is the win. Lazy-pull (eStargz/SOCI) needs containerd, which
  Synology's Container Manager (dockerd) doesn't offer — not applicable to our target.

**Timeout circumvention (James, 2026-08-05):** the 10-min push timeout is gameable because layer
uploads are digest-deduped across pushes. Caveat (James): blobs from a FAILED push are
manifest-orphaned and may be GC'd server-side, so retry-banks-progress is PLAUSIBLE, not
guaranteed — retention window vs retry cadence is an unknown. The robust variant is staged
intermediate tags (first k layers, 2k, ...): each stage's manifest REFERENCES its layers, making
them GC-immune by construction. Driver does plain retries (correct for transient failures
regardless); staged tags is the design if timeouts bite. Layer limit thus remains the only real
per-artifact constraint.

**Probe confounds (James, 2026-08-05):** the staircase runs under the strictest plausible tier —
PRIVATE package, FREE-tier account (shkolnik-beep). Limits may differ for public packages (GH is
more generous in public) and for the paid shkolnik account. Passing here is the pessimistic
bound; a failed rung gets retried public (isolates visibility), then from shkolnik creds
(isolates tier). Final confirmation at target conditions (ghcr.io/shkolnik, public) required
before relying on the result. Private-package storage is billed/quota'd — delete probe tags
promptly.

## Round-trip + visibility-flip results (2026-08-05, verified live)

- Private→public flip on `registry-probe` took effect in ≤120s (anonymous manifest poll);
  the already-banked 80 GiB survived the flip byte-identical (manifest re-verified after).
- Full round trip proven: g80 (80.03 GiB, 8x 10.003 GiB layers) pulled anonymously to an
  independent host (Synology NAS, stock dockerd, 3 concurrent downloads) — download succeeded.
- Staircase cut after g100 by decision: ~20% headroom over the largest real image (gitlab 73G)
  is enough; probing for GHCR's actual ceiling buys nothing we need.
