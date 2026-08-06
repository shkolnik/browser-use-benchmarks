# WebArena shopping (Magento 2 "One Stop Market") — provenance-clear appliance image

Single all-in-one image (supervisord: mariadb, redis, elasticsearch, php-fpm, nginx) on
port **7770**, rebuilt from clean parts instead of shipping the upstream 67G docker-save
tar verbatim. Adjudicated 2026-08-06: community-base approach (markshust/docker-magento +
Mage-OS mirror) adopted over reusing the upstream tar's own base layers; experiment record
in `~/beep-scratch/browser-use-benchmarks/shopping-community-base/findings.md`.

## Provenance

| Part | Source | Pin |
|---|---|---|
| PHP 8.1-fpm + Magento extensions | `markoshust/magento-php` (markshust/docker-magento, MIT — `vendor-markshust/`) | by digest in Dockerfile |
| Magento 2.4.6 code | `composer install` from the committed `composer.lock`; every package URL is the keyless https://mirror.mage-os.org | `composer.lock` (this dir) |
| nginx, redis 7.0, mariadb, supervisor | Debian bookworm packages (base image's distro) | apt (floating within bookworm) |
| Elasticsearch 7.17.9 | Elastic's official deb (bundled JDK) | sha512 in Dockerfile |
| DB dump, 45G pub/media, env.php | derived from the pinned upstream tar by `derive-backup.sh` | upstream tar sha256 in image.toml |

`composer.json` + `composer.lock` were generated 2026-08-06 by
`composer create-project --repository-url=https://mirror.mage-os.org magento/project-community-edition=2.4.6`
(inside the pinned base image) and committed; builds run `composer install` from the lock,
so package resolution never floats.

## Deviations from the proven experiment stack (adjudication notes)

- **MariaDB 10.11 (bookworm distro), not 10.6**: no 10.6 packages exist for bookworm
  anywhere official (mariadb.org repos stop at bullseye; checked archive.mariadb.org too).
  The load is a logical SQL dump (10.6.12 origin) and 10.6→10.11 is an upstream-supported
  LTS jump; validated live by the full storefront/search/admin suite.
- **nginx 1.22 (bookworm), not 1.24**: config-identical usage (Magento's own
  `nginx.conf.sample` does the heavy lifting).
- **ES from Elastic's deb, not `markoshust/magento-elasticsearch:7.17-1`**: same 7.17
  line; the markshust image only adds analysis plugins Magento's default config never
  references. Search validated live after the build's one-time `indexer:reindex`.
- **Magento tree at `/opt/magento`, not `/var/www/html`**: the base image declares
  `VOLUME /var/www`; a 48G tree there would be copied into a fresh anonymous volume on
  every `docker run`. (A small empty anonymous volume for `/var/www` is still created —
  harmless.)
- **Redis persistence disabled** (`save ""`): it only backs Magento's cache; also
  removes the root-owned-`dump.rdb` crash-loop class the gitlab image hit.

## Hard rules baked into the build (from the experiment)

- **Never run `setup:upgrade` or `setup:di:compile`** against this DB — the upstream
  dataset's `setup_module` lacks core rows; either command would treat core modules as
  uninstalled. `config.php` is synthesized with `module:enable --all` + `app:config:import`.
- DB load strips `DEFINER` clauses; base URLs in the data stay
  `http://metis.lti.cs.cmu.edu:7770` (validate with
  `curl --resolve metis.lti.cs.cmu.edu:7770:<host-ip>`).
- The restore stage boots the *shipped* supervisord config, validates the storefront and
  catalog search in-build, then requires a quiesced mariadb ("Shutdown complete") and
  audits file ownership of every runtime datadir before partitioning `/opt/magento`
  into <=8G buckets (one final-stage COPY layer each, under GHCR's ~10G layer ceiling).
