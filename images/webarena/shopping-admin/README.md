# WebArena shopping-admin (Magento 2 admin panel) — provenance-clear appliance image

Single all-in-one image (supervisord: mariadb, redis, elasticsearch, php-fpm, nginx) on
port **7780**, built with the exact same provenance-clear pattern as `../shopping` (Magento
2.4.6 "One Stop Market" storefront) — see that image's README.md for the full adjudication
notes (community-base vs. upstream-tar-verbatim, MariaDB 10.11/nginx 1.22/ES-deb version
deviations, why `/opt/magento` not `/var/www/html`). This doc only records what differs.

## Provenance

| Part | Source | Pin |
|---|---|---|
| PHP 8.1-fpm + Magento extensions | `markoshust/magento-php` (markshust/docker-magento, MIT — `vendor-markshust/`) | same digest as `../shopping` |
| Magento 2.4.6 code | `composer install` from `composer.lock` | **identical file** to `../shopping`'s (version parity — same dataset generation, same Magento build) |
| nginx, redis 7.0, mariadb, supervisor | Debian bookworm packages | apt (floating within bookworm) |
| Elasticsearch 7.17.9 | Elastic's official deb (bundled JDK) | same sha512 as `../shopping` |
| DB dump, pub/media, env.php | derived from the pinned upstream tar by `derive-backup.sh` (already run; cached at `ghcr.io/shkolnik/webarena-shopping-admin-derived:ad607557a79f`) | upstream tar sha256 in `image.toml` |

## Deltas from `../shopping`

- **Port 7780**, not 7770 — EXPOSE, the nginx site conf (`conf/magento-site.conf`), and the
  data's own base URLs (`http://metis.lti.cs.cmu.edu:7780`) all agree on it.
- **No bucket-partitioning stage.** This dataset's `/opt/magento` (Magento code ~2-3G + 85M
  `pub/media`, vs. shopping's 45G media) is comfortably under GHCR's ~10G per-layer ceiling,
  so the final stage does one `COPY --from=audited /opt/magento/ /opt/magento/` instead of
  shopping's 7-bucket partition scheme.
- **Ownership audit is its own build stage/layer** (`FROM restore AS audited`), not part of
  the restore `RUN`. In `../shopping`'s build the audit lived inside the restore RUN, so an
  audit failure re-ran the whole ~11-minute restore — lost two builds that way (a root-owned
  `debian-*.flag` and `mysql_upgrade_info` in `/var/lib/mysql`). Splitting it out means a
  future audit failure only re-runs this cheap check. The audit itself keeps the same
  zero-tolerance posture and the same two normalizations.
- **In-build validation targets the admin panel, not the storefront** — there is no
  unauthenticated admin page to content-check the way shopping checks the storefront
  homepage, so `restore-stage.sh` asserts `GET /admin` returns 200 *and* the response body
  contains Magento core's admin login form field (`name="login[username]"`, stable across
  2.x — more reliable than any theme-rendered copy text). Elasticsearch's post-reindex doc
  count is asserted directly via `_cat/count` (shopping's equivalent runs the unauthenticated
  `catalogsearch/result` storefront page instead, which shopping-admin doesn't have).
- **Measured DB row-count regression pins** (this dataset, not shopping's): `catalog_product_entity` = 2040,
  `sales_order` = 308.
- `conf/env.php` is this dataset's own derived `app/etc/env.php` (own crypt key, graphql
  cache salt, install date — all distinct from shopping's, since it's a different Magento
  installation snapshot), with the same `system.default.catalog.search` elasticsearch7 block
  shopping's `conf/env.php` adds on top of its own derived output (the upstream env.php
  predates enabling Elasticsearch as the search engine).
- `conf/mariadb-admin.cnf` / `conf/redis-admin.conf` are `../shopping`'s
  `mariadb-shopping.cnf` / `redis-shopping.conf` renamed (content unchanged) to keep the
  filenames distinct across the two image directories' provenance trail; `supervisord.conf`
  points `redis-server` at the renamed file. `elasticsearch.yml`'s `cluster.name`/`node.name`
  are `shopping-admin`/`shopping-admin-1` (cosmetic — separate containers, never in the same
  cluster) instead of shopping's `shopping`/`shopping-1`.

## Hard rules baked into the build (same as `../shopping`)

- **Never run `setup:upgrade` or `setup:di:compile`** against this DB — the upstream
  dataset's `setup_module` lacks core rows. `config.php` is synthesized with
  `module:enable --all` + `app:config:import`.
- DB load strips `DEFINER` clauses; base URLs in the data stay
  `http://metis.lti.cs.cmu.edu:7780` (validate with
  `curl --resolve metis.lti.cs.cmu.edu:7780:<host-ip>`).
- The restore stage boots the *shipped* supervisord config, validates the admin panel and
  measured DB row counts in-build, then requires a quiesced mariadb ("Shutdown complete")
  before the audit stage checks file ownership of every runtime datadir.
