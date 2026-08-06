# VWA classifieds — scoped, not yet built (2026-08-06)

Still carries no `image.toml` (discovery is by `image.toml` glob, so this directory stays
invisible to the driver until the build lands). But the earlier framing here — "not
buildable via either manifest kind, decision deferred" — was **wrong about the hard part**,
and the correction is worth recording.

## The provenance question is answered, and it is clean

The upstream app image `jykoh/classifieds:latest` was pulled and inspected. **Every layer
has a real buildkit history entry, all the way down to the official `php:8.1.27-cli`
base.** There are no hand-made `docker commit` layers anywhere — contrast
`../../webarena/reddit`, whose top four layers are commits whose history reads literally
`supervisord -n -j /supervisord.pid`, i.e. a container someone modified by hand with no
record of how. Classifieds has no such hole: the app image is reproducible from public,
Apache-2.0 sources with roughly a ten-line Dockerfile.

The full upstream recipe, read off the image:

1. `php:8.1.27-cli` (docker-library/php; PHP built from source with `PHP_URL`,
   `PHP_SHA256` and GPG keys pinned in ENV)
2. `RUN apt-get install libfreetype6-dev libjpeg62-turbo-dev libpng-dev && docker-php-ext-configure gd --with-freetype --with-jpeg && docker-php-ext-install -j$(nproc) gd`
3. `RUN apt-get install default-mysql-client`
4. `RUN docker-php-ext-install mysqli`
5. `COPY osclass-v8.1.2 /usr/src/myapp`, `WORKDIR /usr/src/myapp`, `CMD ["php","-S","0.0.0.0:9980"]`

Verified by running the image: `OSCLASS_VERSION` = **8.1.2**, `php -v` = **PHP 8.1.27**,
licence **Apache-2.0**. Upstream is github.com/osclass/Osclass — tag **v8.1.2** is the
parity pin. The rebuild should fetch that tag and pin its sha256, **not** copy the app
tree out of `jykoh/classifieds`; copying the blob would re-import exactly the opacity
this program exists to remove.

`config.php` reads `WEB_PATH` from `getenv("CLASSIFIEDS")`, so the site's base URL is
injected at **runtime**, not baked into the image. Serving is PHP's built-in dev server
on port 9980 (no nginx/apache). DB coordinates are `osclass` / `root` / `password` at
host `db`.

## ⚠️ `classifieds_restore.sql` is the RESET script, not seed data

19,001 bytes, present in BOTH the upstream zip (`mysql/02_classifieds_restore.sql`) and
inside the app image at `/usr/src/myapp/classifieds_restore.sql`. Its body is
`DELETE FROM <table> WHERE fk_i_item_id >= 84143` per table — it is what the
`RESET_TOKEN` endpoint executes to return the site to a known state between benchmark
tasks. It must remain present and executable in any rebuilt image: dropping it would
silently break task isolation, with nothing failing loudly at build time.

The actual seed data is `mysql/osclass_craigslist.dump` (**84,608,226 bytes**) — small
enough that no staging-bucket partitioning is needed, unlike shopping's 45G of media.

## What is still an open call

Not the provenance — the packaging shape:

- **(a) Single self-contained appliance** (the lean): PHP base + a MySQL server +
  supervisord, dump restored at **build** time, staged through the same
  `restore` → `audited` (its own RUN layer) → final pattern the Magento images use.
  Consistent with every other image in this fleet, one healthcheck, no new manifest kind,
  and boots do no restore work at all.
- **(b) A third manifest kind wrapping an upstream compose project.** Honest to upstream's
  two-service shape, but introduces a new manifest concept and a multi-container runtime
  contract for exactly one benchmark.

Sub-call inside (a): upstream's db is `mysql:8.1`, while the php base's Debian bookworm
ships MariaDB. Parity policy prefers **MySQL 8.1 from Oracle's apt repo, version-pinned**;
MariaDB is acceptable only as a **named, documented deviation** here (Osclass uses plain
mysqli with no server-specific features, so the risk is low — but the deviation must be
stated, not silently taken).

Full task spec, including validation pins to measure before building:
`~/beep-scratch/browser-use-benchmarks/classifieds/SPEC.md`.
