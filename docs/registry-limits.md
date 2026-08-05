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
| _(not yet run at full scale in this environment)_ | | | | | | | |

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
  The ~10 GB layer ceiling across registries is an inherited S3 5 GB-multipart artifact.
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
