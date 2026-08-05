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
