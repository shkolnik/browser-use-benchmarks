# webarena/reddit — Postmill, rebuilt provenance-clear

Replaces the upstream `postmill-populated-exposed-withimg.tar` (53 GB, loaded and
re-tagged verbatim) with a real build. Upstream's top four layers are hand
`docker commit`s whose history entries read literally
`supervisord -n -j /supervisord.pid` — no Dockerfile, no record of how the code got
there. That is the hole this image closes.

## Where each byte comes from

- **Code** — Postmill at commit **`a6a9bbc86f44`**, built with **Postmill's own
  multi-stage Dockerfile**, which was recoverable from `/var/www/html/Dockerfile`
  inside the upstream image. No reverse-engineering was needed.
- **Data** — a `pg_dump` and a media tar derived from the upstream image's own
  running container by `derive-backup.sh`, loaded at **build** time.

⚠️ **Postmill is not on GitHub.** `github.com/postmill/Postmill` 404s. Canonical:
`codeberg.org/Postmill/Postmill`; official mirror `gitlab.com/postmill/Postmill`.

The commit was established by content-matching the shipped tree against upstream
history — `Dockerfile`, `composer.json`, `composer.lock`, `docker-compose.yml`,
`package.json` plus real source (`src/Entity/Forum.php`,
`src/Controller/FrontController.php`, `templates/base.html.twig`) are all
byte-identical at `a6a9bbc86f44`. The comparison was confirmed to **discriminate**:
the same method reports the Dockerfile differing at `f281d414cd80` and
`4b99b4b31897`.

## What this image deliberately does NOT ship

The upstream base (`adhocore/phpfpm`) bundles elasticsearch, redis, memcached,
beanstalkd, mailcatcher and adminer. **None of them were ever started** —
`supervisorctl status` in the upstream container lists only `nginx`, `php-fpm`
and `postgres`, and `:9200` answered nothing. Only those three are shipped.

## Named deviations from upstream

1. **Base tag `php:8.1-fpm-alpine3.16`, not `php:8.1-fpm-alpine`.** The floating
   tag is alpine 3.21 today, which has no `postgresql14` package at all (15 and 17
   only), and the measured upstream server is **postgres 14.7**. Parity on the
   database major version won. Alpine 3.16 is EOL — revisiting it is the job of the
   security-upgrade pass, not of a parity rebuild.
2. **A typo in upstream's Dockerfile is not reproduced.** Its php.ini line reads
   `… >> 'apc.enable_cli = On' >> "$PHP_INI_DIR/conf.d/zz-postmill.ini"`; the stray
   middle redirect writes a *file named* `apc.enable_cli = On` and only partly
   writes the intended ini. The two intended settings are written properly here.
3. **`APP_ENV=prod`, where the upstream image ran `dev`.** Symfony's dev mode
   injects the web debug toolbar into every rendered page — DOM that an agent
   benchmark would have to contend with, and that nobody deploys deliberately.
   Upstream's own Dockerfile builds a prod stage. This is the one deviation with a
   visible surface, so it is the thing a task-level A/B against the upstream
   container should check.

Single-container shape (nginx + php-fpm + postgres under supervisord) matches the
upstream appliance and the rest of this fleet, so upstream's compose-specific
nginx config needed two mechanical changes — see the comments in `nginx.conf`.

## Expect a small image

Measured inputs: **~477 MiB** of gzipped dump and **2.4 MB** of media. With an
alpine base that is low single-digit GB. Upstream's 53 GB is mostly unpacked
`node_modules` (which upstream's own Dockerfile deletes) plus the kitchen-sink base.
**A result anywhere near 50 GB is a defect signal, not a success.**

## Build-time assertions

`audit.sh` runs in its own layer and fails the build unless the restored database
matches the counts measured in the upstream container: `users` **661,782**,
`forums` **95**, `submissions` **127,391**, `comments` **2,551,513**.
