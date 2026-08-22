# Standing the fleet up on one host

> **Trusted networks only.** These images pin old, in several cases
> end-of-life, dependencies and carry known-vulnerable packages — deliberately,
> so that rendered pages stay comparable with published benchmark results. Do
> not port-forward them or put them behind a public reverse proxy. See
> [`SECURITY.md`](../SECURITY.md).

`deploy/compose.yml` brings up all eight benchmark services from the published
`:latest` images. It is separate from the per-benchmark `images/<bench>/compose.yml`
files, which exist to smoke-gate one freshly-built image on the build runner and
publish only to `127.0.0.1`.

Three more — the WebArena map back ends — sit behind `--profile map` and do not
come up with the rest. They are ~35 GB, and no benchmark task can reach them: all
128 map-tagged WebArena tasks address the OpenStreetMap front end, which this repo
does not build. Add `--profile map` if you want them anyway.

```sh
BENCH_HOST=nas.local docker compose -f deploy/compose.yml up -d --wait
```

`BENCH_HOST` is required and has no default. It is the address **clients** will
type, and Magento (`base_url`), Osclass (`WEB_PATH`) and GitLab (`external_url`)
bake it into every link and asset URL they serve. Set it to the host's LAN name
or IP — `localhost` only works if you browse from the host itself. Getting it
wrong produces a container that reports healthy while serving unfollowable links.

Bring up one suite at a time by naming services:

```sh
BENCH_HOST=nas.local docker compose -f deploy/compose.yml up -d --wait shopping reddit
```

## One hostname instead of a port per service

`deploy/compose.proxy.yml` overlays a Caddy front door, giving each service a
subdomain on a single host and port:

```sh
BENCH_HOST=depot.example.com \
  docker compose -f deploy/compose.yml -f deploy/compose.proxy.yml up -d --wait
```

Pass both `-f` flags, in that order — the overlay adds the proxy and rewrites
each service's public address, but everything else still comes from the base
file. `BENCH_HOST` is the same variable as in direct mode; here it is the base
domain, and `shopping.$BENCH_HOST`, `reddit.$BENCH_HOST` and so on route to the
services. `Caddyfile` must sit next to the compose files, since it is
bind-mounted.

**`*.$BENCH_HOST` has to resolve to this host** — a wildcard DNS record, or
per-name entries in each client's hosts file. The bare domain serves an index
of the published names; an unpublished hostname gets a 404 saying so, and a
subdomain whose service is not running gets a 502.

`PROXY_PORT` (default 80) moves the front door.

Two consequences worth knowing before you switch:

- **The direct ports still answer, but serve subdomain links.** An image can
  bake only one address, so proxy mode repoints `HTTP_HOST`/`HTTP_PORT` at the
  subdomain and the per-service ports become debug-only. Set
  `BIND_ADDR=127.0.0.1` to take them off the network and make the proxy the
  only way in.
- **GitLab's direct port stops working in proxy mode.** Its in-container
  listener follows `HTTP_PORT`, so it moves to the proxy's port while the base
  file still publishes `8023:8023` — that mapping then points at nothing.
  Reach GitLab through `gitlab.$BENCH_HOST`. Nothing else is affected; every
  other image has a fixed listen port.

To serve HTTPS instead, drop `auto_https off` and the `http://` prefixes from
the `Caddyfile`, and make sure the names are publicly resolvable or configure a
DNS-01 provider.

## Ports

| Service | Published | Override |
|---|---|---|
| `shopping` | 7770 | `SHOPPING_PORT` |
| `shopping-admin` | 7780 | `SHOPPING_ADMIN_PORT` |
| `reddit` | 9999 | `REDDIT_PORT` |
| `gitlab` | 8023 | `GITLAB_PORT` |
| `wikipedia` | 9888 | `WIKIPEDIA_PORT` |
| `classifieds` | 9980 | `CLASSIFIEDS_PORT` |
| `webshop` | 3000 | `WEBSHOP_PORT` |
| `miniwob` | 8399 | `MINIWOB_PORT` |

Under `--profile map`:

| Service | Published | Override |
|---|---|---|
| `map-tile` | 8080 | `MAP_TILE_PORT` |
| `map-osrm` (car) | 5000 | `MAP_OSRM_CAR_PORT` |
| `map-osrm` (bike) | 5001 | `MAP_OSRM_BIKE_PORT` |
| `map-osrm` (foot) | 5002 | `MAP_OSRM_FOOT_PORT` |
| `map-nominatim` | 8085 | `MAP_NOMINATIM_PORT` |

`map-osrm` is one container with three listeners: it picks the routing profile
from the port and from nothing else, so all three are published and the proxy
gives each its own subdomain. The three map services take no `HTTP_HOST` or
`HTTP_PORT` — tiles, routes and geocoding results carry no address — so their
overrides move the published side alone.

Each override moves the published port and `HTTP_PORT` together, which is what
the service contract requires — `HTTP_PORT` names the port a client types, not
the port the process binds. Wikipedia publishes 9888 rather than upstream's
8888, which is heavily squatted (OpenTelemetry collector, Jupyter).

**GitLab is the one exception:** its internal listener follows `HTTP_PORT`, so
both sides of its mapping move together. Every other image has a fixed listen
port. See `docs/service-contract.md`.

Because of that, **`18080` is the one value `HTTP_PORT` (and so `PROXY_PORT`,
which gitlab follows) may not take** — it is where puma listens behind gitlab's
nginx, and both would bind it. The entrypoint refuses it immediately and says
so. Puma was deliberately moved there off omnibus's default of 8080 so that the
reserved port is one nobody wants to publish.

## Before you pull

**These images are `linux/amd64` only.** There is no `arm64` variant, so an
ARM-based NAS cannot run them without emulation.

**Disk: ~280 GB compressed to pull, ~316 GB with the map profile, more once
unpacked.** Compressed sizes from the registry:

| Image | Compressed |
|---|---|
| `webarena-wikipedia` | 87.9 GB |
| `vwa-classifieds` | 77.0 GB |
| `webarena-shopping` | 42.9 GB |
| `webarena-reddit` | 42.4 GB |
| `webarena-gitlab` | 22.3 GB |
| `webshop-server` | 6.4 GB |
| `webarena-shopping-admin` | 1.1 GB |
| `miniwob-server` | 0.03 GB |

Under `--profile map`, another ~35 GB on top:

| Image | Compressed |
|---|---|
| `webarena-map-nominatim` | 13.1 GB |
| `webarena-map-tile` | 12.7 GB |
| `webarena-map-osrm` | 9.8 GB |

Make sure Docker's storage lives on the large volume before pulling.

**Memory.** Both Magento images run Elasticsearch alongside PHP-FPM and nginx,
and GitLab runs its full runit-supervised stack. Running the whole fleet at once
wants considerably more RAM than any one of them — bring it up suite by suite the
first time and watch, rather than assuming the whole set fits. The map profile
adds two more Postgres instances to that, for services nothing can currently
reach.

## First boot

GitLab runs `gitlab-ctl reconfigure` (~28s) whenever `external_url` changes, and
it will change on first boot here because the restored dataset carries CMU's own
hostname. That is expected, and it is also what stops the image serving links
pointing at `metis.lti.cs.cmu.edu`.

`--wait` blocks on each image's `HEALTHCHECK`, which probes the in-container
listen port. It structurally cannot see a broken port mapping, so also confirm
each service answers on the published address from another machine.
