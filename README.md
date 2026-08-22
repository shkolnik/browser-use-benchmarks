# browser-use-benchmarks

Prebuilt, self-contained Docker images for the standard **web-agent benchmarks** —
MiniWoB++, WebShop, WebArena, and VisualWebArena — plus the builder service that
produces them reproducibly.

## Why this exists

If you want to evaluate a browser-using agent, the benchmark *tasks* are easy to get —
but the *websites they run against* are not. Upstream, each benchmark environment is
some mix of multi-gigabyte `docker save` tars on flaky university mirrors, datasets
on Google Drive, and multi-step setup scripts that patch containers after boot.
Standing up the full set from scratch takes days, and the result is hard to reproduce.

This project packages each benchmark website as a **single pullable Docker image**:

- **Self-contained** — all data is baked in. No bind mounts, no post-boot setup
  scripts, no network fetches at runtime. `docker run`, wait for healthy, done.
- **Reproducible** — every input is either in git or a sha256-pinned dataset;
  Dockerfiles never touch the network. Images are rebuilt from upstream sources and
  data dumps with clear provenance, not re-tagged from opaque upstream tars.
- **Relocatable** — each image takes `HTTP_HOST`/`HTTP_PORT` at runtime and rewrites
  the links it serves accordingly, instead of baking in someone else's hostname
  (see [`docs/service-contract.md`](docs/service-contract.md)).
- **Health-checked** — every image declares an in-container `HEALTHCHECK`, so
  `docker compose up --wait` blocks until the service actually works.

## Security posture

**These images pin old, in several cases end-of-life, dependency versions, and
they carry known-vulnerable packages. Run them only on trusted networks; do not
expose them to the public internet.**

The pins are deliberate. An agent's score is a function of the exact bytes a
page serves, and dependency upgrades move those bytes — a newer ICU changes how
Magento renders prices and dates, GitLab reorganised its navigation across 16.x,
Kiwix changed its URL scheme in 3.4. Patching would quietly invalidate
comparison against published results. [`SECURITY.md`](SECURITY.md) covers the
reasoning, what is done instead, and how to run the fleet safely.

## Images

| Benchmark | Name | Image |
| --- | --- | --- |
| WebArena | webarena-shopping | [ghcr.io/shkolnik/webarena-shopping](https://github.com/shkolnik/browser-use-benchmarks/pkgs/container/webarena-shopping) |
| WebArena | webarena-shopping-admin | [ghcr.io/shkolnik/webarena-shopping-admin](https://github.com/shkolnik/browser-use-benchmarks/pkgs/container/webarena-shopping-admin) |
| WebArena | webarena-reddit | [ghcr.io/shkolnik/webarena-reddit](https://github.com/shkolnik/browser-use-benchmarks/pkgs/container/webarena-reddit) |
| WebArena | webarena-wikipedia | [ghcr.io/shkolnik/webarena-wikipedia](https://github.com/shkolnik/browser-use-benchmarks/pkgs/container/webarena-wikipedia) |
| WebArena | webarena-gitlab | [ghcr.io/shkolnik/webarena-gitlab](https://github.com/shkolnik/browser-use-benchmarks/pkgs/container/webarena-gitlab) |
| VisualWebArena | vwa-classifieds | [ghcr.io/shkolnik/vwa-classifieds](https://github.com/shkolnik/browser-use-benchmarks/pkgs/container/vwa-classifieds) |
| WebShop | webshop-server | [ghcr.io/shkolnik/webshop-server](https://github.com/shkolnik/browser-use-benchmarks/pkgs/container/webshop-server) |
| MiniWoB++ | miniwob-server | [ghcr.io/shkolnik/miniwob-server](https://github.com/shkolnik/browser-use-benchmarks/pkgs/container/miniwob-server) |
| WebArena | webarena-map-tile | [ghcr.io/shkolnik/webarena-map-tile](https://github.com/shkolnik/browser-use-benchmarks/pkgs/container/webarena-map-tile) |
| WebArena | webarena-map-osrm | [ghcr.io/shkolnik/webarena-map-osrm](https://github.com/shkolnik/browser-use-benchmarks/pkgs/container/webarena-map-osrm) |
| WebArena | webarena-map-nominatim | [ghcr.io/shkolnik/webarena-map-nominatim](https://github.com/shkolnik/browser-use-benchmarks/pkgs/container/webarena-map-nominatim) |

The three map images are built and published but sit behind compose's `map`
profile, and no benchmark task can reach them: WebArena's 128 map tasks address
the OpenStreetMap front end, which this repo does not build.

Images are `linux/amd64` only, and several are large (wikipedia is ~88 GB
compressed; the fleet is ~280 GB to pull, ~316 GB with the map profile). See
[`deploy/README.md`](deploy/README.md) for per-image sizes and host requirements.

## Running the benchmarks

The fastest path is the fleet compose file, which brings up all eight benchmark
services (or any subset) from the published `:latest` images:

```sh
BENCH_HOST=nas.local docker compose -f deploy/compose.yml up -d --wait
```

`BENCH_HOST` is **required**: it is the address your agent/browser will use to
reach the services, and several images (Magento, Osclass, GitLab) bake it into
every link they serve. Use the host's LAN name or IP, not `localhost` — unless
you only ever browse from that host.

Bring up a subset by naming services:

```sh
BENCH_HOST=nas.local docker compose -f deploy/compose.yml up -d --wait miniwob webshop
```

Or run a single image directly:

```sh
docker run -d -p 8399:8399 -e HTTP_HOST=localhost -e HTTP_PORT=8399 \
  ghcr.io/shkolnik/miniwob-server:latest
```

[`deploy/README.md`](deploy/README.md) covers the rest: the port table and how to
move ports, a Caddy overlay that serves every benchmark as a subdomain of one
hostname (`shopping.example.com`, `reddit.example.com`, …), disk/RAM sizing, and
first-boot behavior.

## Building the images yourself

You don't need to build anything to *use* the benchmarks — pull from GHCR. Build
if you want to change an image, adopt new upstream data, or mirror to your own
registry.

The driver is `bin/build` (Python stdlib only; needs Docker with BuildKit):

```sh
bin/build list all                      # discover buildable images
bin/build download webarena/reddit      # fetch + sha256-verify datasets into datasets/
bin/build build webarena/reddit         # docker build, datasets passed as a build context
bin/build smoke webarena/reddit         # compose up, poll health, compose down
bin/build push webarena/reddit          # tag + push (--registry overrides ghcr.io/shkolnik)
```

Targets are `all`, `<benchmark>`, or `<benchmark>/<service>`, discovered by
globbing `images/*/*/image.toml` — adding a service is creating a directory,
removing one is deleting it. Each `image.toml` is that image's entire
configuration: dataset URLs, mirrors and checksums, prepare/derive steps, and
the smoke-test healthcheck.

Fair warning: several images require enormous inputs (a 67 GB Magento tar, an
88.7 GB Wikipedia ZIM), and some upstream mirrors serve them at ~3.6 MB/s. The
builder caches *derived* build inputs in GHCR so routine rebuilds skip those
fetches, but a cold rebuild of the big WebArena images is a plan-your-day affair.
CI builds run on a self-hosted runner for the same reason
([`.github/workflows/build.yml`](.github/workflows/build.yml)).

## Learn more

- [`docs/design.md`](docs/design.md) — full design: goals, repo layout, caching
  and cache-invalidation model, CI.
- [`docs/service-contract.md`](docs/service-contract.md) — what every runnable
  image must declare: a `HEALTHCHECK` (readiness, in-container) and required
  `HTTP_HOST`/`HTTP_PORT` (the published address clients use).
- [`deploy/README.md`](deploy/README.md) — standing the fleet up on one host,
  ports, proxy mode, sizing.
- [`docs/registry-limits.md`](docs/registry-limits.md) — empirical findings on
  pushing very large images to GHCR and Docker Hub.
- [`docs/build-data-path.md`](docs/build-data-path.md) — measured behaviour of
  `tar`, `ADD` and buildkit that the data path is built around. Every trap in it
  fails silently: the build goes green and the image is wrong.
