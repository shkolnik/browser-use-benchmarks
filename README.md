# browser-use-benchmarks

A builder service for runnable benchmark-server docker images (MiniWoB++, WebShop, WebArena, …):
downloads checksummed datasets, builds reproducible images, pushes them to a container registry,
and smoke-tests them via docker compose. See `docs/design.md` for the full design.

- [`docs/service-contract.md`](docs/service-contract.md) — what every runnable image must declare:
  a `HEALTHCHECK` (readiness, in-container) and required `HTTP_HOST`/`HTTP_PORT` (published address).

Usage: `bin/build list|download|build|push|smoke <target>` where target is `all`,
`<benchmark>`, or `<benchmark>/<service>`.
