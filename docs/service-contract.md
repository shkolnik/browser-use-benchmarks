# The fleet service contract

Every runnable image in this repo declares two things. They answer different questions, at
different addresses, and confusing them is the most common way to break an image here.

| | Who owns it | Which address | Read by |
|---|---|---|---|
| **`HEALTHCHECK`** (Dockerfile) | readiness — *is the app serving?* | the **in-container listen port** | `docker compose up --wait` |
| **`[service].healthcheck`** (image.toml) | reachability — *does the port mapping work?* | the **published address** | `poll_health`, after `up --wait` |
| **`HTTP_HOST` / `HTTP_PORT`** (compose) | the address clients use | the **published address** | the image's `entrypoint.sh` |

`HTTP_HOST`/`HTTP_PORT` are **required and have no defaults.** An image with a baked-in hostname
serves somebody else's links the first time it is published anywhere else, silently. This fleet
shipped that bug once: classifieds carried `ENV CLASSIFIEDS=http://127.0.0.1:9980/`, which is
Osclass's `WEB_PATH` — the hostname it writes into every link and asset URL.

## The address table

Regenerated from the tree, not copied from the plan.

| Image | `EXPOSE` | `HEALTHCHECK` probes | Published | `image.toml` healthcheck |
|---|---|---|---|---|
| `miniwob/server` | 8399 | `localhost:8399/miniwob/click-button.html` | `8399:8399` | `localhost:8399/miniwob/click-button.html` |
| `vwa/classifieds` | 9980 | `localhost:9980/` | `9980:9980` | `localhost:9980/` |
| `webarena/shopping` | 7770 | `localhost:7770/` | `7770:7770` | `127.0.0.1:7770/` |
| `webarena/shopping-admin` | 7780 | `127.0.0.1:7780/admin` | `7780:7780` | `127.0.0.1:7780/admin` |
| `webarena/reddit` | 80 | `localhost:80/forums` | **`9999:80`** | `localhost:9999/forums` |
| `webshop/server` | 3000 | `localhost:3000/` | `3000:3000` | `localhost:3000/` |
| `webarena/gitlab` | 8023 | `gitlab-healthcheck` (not HTTP) | `8023:8023` | `localhost:8023/explore` |
| `webarena/wikipedia` | 80 | `127.0.0.1:80/<landing>` | **`8888:80`** | `127.0.0.1:8888/<landing>` |

`images/probe/synthetic/` is deliberately absent: it has no Dockerfile and no runnable service.
`bin/build smoke probe` failing on the missing healthcheck is correct, and CI excludes the
benchmark by name in `build.yml`'s discover step.

### Reddit is the worked example

Reddit is the only image whose published and listen ports differ — `9999:80`. So:

- its `HEALTHCHECK` probes **`localhost:80`**, because it runs *inside* the container, where
  nothing is bound to 9999;
- its `image.toml` names **`localhost:9999`**, because that check runs on the *host*;
- `HTTP_PORT` is **9999**, the published side, because that is what a client would type.

Copying the `image.toml` URL into the `HEALTHCHECK` is the easy mistake, and it fails in the
confusing direction: the container never becomes healthy while the app is working fine.

## Why `poll_health` still exists

`up --wait` evaluates health *inside* the container. It structurally cannot see a broken port
mapping. In #66, shopping was healthy in-container while the host got `ECONNREFUSED` on 7770 for
901s. So after `--wait` returns, the driver still probes the published address — with a small
budget (`reachability_timeout_s`, default 30s), because readiness has already been proven and this
is only asking whether the mapping works.

`healthcheck_timeout_s` is retired and is now a hard error: leaving a stale 900 in a file, doing
nothing, while everyone assumed it was still the budget, is worse than a loud failure.

## Tuning a HEALTHCHECK

**`--timeout` is the decisive parameter, not the total budget.** A probe that gives up before the
app's slowest single response can never succeed at *any* number of retries — webshop's pre-fork
first request took 57s against a 10s probe, and no total budget would have rescued it. Docker
never overlaps probes, so a generous `--timeout` costs nothing when things are healthy.

- `--start-period` failures do not count toward `--retries`; a success during it marks the
  container healthy immediately. So an over-long start period is not free — it delays *detecting a
  stuck container*, it does not slow a healthy one.
- Worst case to unhealthy ≈ `start-period + retries × (timeout + interval)`.
- Size it from a measurement. shopping-admin's 900s was inherited from an assumption about a
  first-request DI compile it turned out not to perform (its `generated/` classes arrive with the
  restored `/opt/magento` tree); measured, it reaches healthy in **15s**, so it now uses 300s.
- **`curl` is the fleet's one probe binary — with one measured exception.** `wget` is missing from
  shopping-admin; `curl` is missing from wikipedia's Alpine-based kiwix base. That image uses
  busybox `wget`, which needs two extra cautions (both checked in-container, 2026-08-08):
  it exits non-zero on a plain 404, but **following a redirect it prints the error and still exits
  0** — so it must probe a path that answers 200 in one hop; and it tries `::1` first for
  `localhost`, so against a server bound to `0.0.0.0` (IPv4-only, as kiwix-serve is) it gets
  ECONNREFUSED. **Use `127.0.0.1` in a wget-based probe**, or the container never goes healthy
  while the app is fine.
- **`-L` is load-bearing wherever the probed path redirects.** `curl -f` fails only on >=400, so
  an unfollowed 302 passes against a container that rendered nothing.
- **Probe a path that touches the app, not just the router.** Reddit probes `/forums`, not `/`,
  because a document root nested one level too deep still answers `/` and 404s every real page —
  a defect that shipped once already.
- **The host in the URL can matter.** Magento's adminhtml is pinned to the configured `base_url`
  (#70). Measured in-container on shopping-admin: `Host: localhost` → **404** (it falls through to
  the frontend area), `Host: 127.0.0.1` → **200**. The storefront serves any Host, the admin does
  not.

## Why `entrypoint.sh` is duplicated per image

There are several near-identical validation blocks, and the DRY instinct says extract them into
`builder/`. **Don't.** `discover` takes the `shared build inputs changed: building all` branch on
any change under `builder/`, so a typo fix in a shared entrypoint would rebuild the entire fleet —
hours of CI, serialized. The duplication is the cheaper side of that trade, and it is deliberate.

## gitlab: no longer an exception, but the dearest adopter

gitlab now takes `HTTP_HOST`/`HTTP_PORT` like the rest of the fleet (#10, #12). It is worth
recording why it was the last to, because the cost is real and still paid on every boot.

Its served URL comes from `external_url` in `gitlab.rb`, and the only reader of
`ENV['EXTERNAL_URL']` in the whole omnibus tree of `gitlab-ce:15.7.5` is
`/opt/gitlab/embedded/service/omnibus-ctl/upgrade.rb` — reachable only via `gitlab-ctl
reconfigure`. So unlike every sibling, applying these variables means running chef at container
start: **~28s** for a genuine `external_url` change (`6/741 resources updated`, restarting puma,
sidekiq, gitlab-kas, nginx). The entrypoint skips it when the address already matches, but the
restored dataset bakes CMU's own host, so in practice it always runs — and that is also what stops
the image serving links pointing at `metis.lti.cs.cmu.edu`.

**One consequence is unique to gitlab and easy to trip over:** because nginx derives
`listen *:<port>` from `external_url`, `HTTP_PORT` moves the *in-container* listener too. Published
and container port must therefore be equal for gitlab, which is why its `EXPOSE 8023` goes stale
the moment `HTTP_PORT` is anything else.
