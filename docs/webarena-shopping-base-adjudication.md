# WebArena shopping / shopping-admin: base-image adjudication (needs James's call)

Status: derive step (mysqldump + pub/media + env.php) implemented and validated locally
(`images/webarena/shopping/derive-backup.sh`, `images/webarena/shopping-admin/derive-backup.sh`).
The Dockerfile/restore-stage (the gitlab pattern's second half) is **not yet built** — it hinges
on the question below, per the "stop and adjudicate before a deep rabbit hole" instruction.

## What the gitlab pattern assumed, and why shopping breaks it

The gitlab image works because an **official, versioned, freely reinstallable base** exists
(`gitlab/gitlab-ce:15.7.5-ce.0` on Docker Hub) that can be booted fresh and then restored into via
gitlab's own supported `gitlab-backup restore` mechanism. The final image discards ~40G of the
upstream tar's own OS/log/packfile bloat but keeps the *code* (gitlab-ce itself) from the clean
official release, not from the upstream docker-save tar.

Shopping/shopping-admin have no equivalent clean base:

1. **The upstream image is not built on an official Magento/Adobe Commerce image.** There is no
   such official image — Adobe does not publish one. `docker history` shows the upstream tar is a
   bespoke Alpine 3.16 all-in-one LEMP stack (nginx + php-fpm + MariaDB + Redis + Elasticsearch
   6.4.3 + beanstalkd + mailcatcher + adminer, all under supervisord), built from `php:8.1-fpm-alpine`
   and then apk-installing everything else by hand. Maintainer tag in the history is Jitendra
   Adhikari (a known Alpine PHP-stack image author) — this is a generic self-hosted-PHP-app base
   image, not anything Magento-specific.
2. **WebArena itself ships no build recipe.** `environment_docker/README.md` in the upstream
   `web-arena-x/webarena` repo only documents `docker load` + `docker start` of the pre-built tar —
   there is no Dockerfile to diff against or reuse.
3. **Reinstalling Magento 2.4.6 CE from scratch requires paid/gated Adobe Commerce Marketplace
   auth keys** (`repo.magento.com` composer credentials) to `composer install` the
   `magento/product-community-edition` metapackage. Without those keys, a from-scratch
   `composer create-project` cannot even fetch `vendor/` (670M of it in the populated instance),
   let alone reproduce the exact 2.4.6 patch level and installed sample-data/extensions state.
   This is a hard blocker, not a research gap — there is no clean-room path to an official base
   that doesn't require credentials this project doesn't have and (per Magento's own terms) may
   not be able to distribute derivative-of-vendor-code images built from regardless.

So the two fallback options from the task brief are the live choice, not a formality:

## Option A (lean): base = the upstream image's own base layer, restore stage re-applies our derived state

- Final image's `FROM` is the upstream tar's own re-tagged image (or a `docker export`/`import`
  flatten of it) — i.e., we keep shipping the Alpine LEMP stack and Magento `vendor/`+`generated/`
  code tree essentially as upstream built them, but our own Dockerfile becomes the record of
  *how the populated instance's data was derived* (mysqldump + media + env.php from a clean
  `derive-backup.sh` run against the pinned upstream tar), same evidentiary shape as gitlab's
  restore stage, just without discarding the code layers.
- Pros: actually buildable without new dependencies; keeps the ~40G→~4G win on `pub/media`+DB (the
  bulk of the size difference for `shopping` is media+DB, not the code/OS layers — code/OS layers
  are ~3G total per the `du -sh /var/www/magento2` vendor+generated numbers vs. 49.7G of media).
  Layer-size-wise this is very achievable (bucket-partition media the same way gitlab buckets
  `/var/opt/gitlab`).
  Cons: the shipped image still contains 100% of upstream's OS/package layers (the bespoke Alpine
  stack, its apk package set, its supervisor confs) verbatim — "provenance-clear" only in the sense
  that *our* build step's inputs (the mysqldump/media/env.php restore) are declared and reproducible
  from the pinned upstream tar; it does not eliminate reliance on the upstream tar as a base image
  the way gitlab eliminates it.

## Option B: upstream's own base OS layer + fresh Magento 2.4.6 CE reinstall from official releases

- Would need Adobe Commerce Marketplace auth keys this project doesn't have. Even with keys, the
  reinstalled instance would need `bin/magento setup:install` against the restored DB/media, and
  matching Magento's exact 2.4.6 build/patch/extension state bit-for-bit is not guaranteed —
  correctness would need to be proven the same way gitlab's restore was (a live field-diff between
  OLD and NEW), which is expensive to build and unlikely to reach exact parity given the unknown
  extension/customization surface visible in `vendor/`.
- Not attempted; blocked on credentials this project doesn't have, so effectively not available
  without a Magento Marketplace account being provisioned.

## My lean

**Option A.** It is buildable today with no new external dependencies, delivers the layer-size win
that actually matters (media + DB, not OS/code), and is honest about what it does and doesn't
discard — it should be documented as "restore-stage record of derived state," not oversold as
"discards upstream layers" the way the gitlab entry is. Option B is blocked on secrets this
project doesn't hold and isn't just a deeper rabbit hole, it's presently infeasible.

**Needs James's call**, since it changes what "provenance-clear" means for this pair of images
relative to the gitlab precedent (a weaker, but still real, improvement) — flagging per the
reserved-for-James pattern rather than deciding unilaterally.

## What's proven vs. not (as of this write-up)

- **Proven live:** upstream Cmd/Entrypoint (`supervisord -n -j /supervisord.pid` via
  `/docker-entrypoint.sh`), base OS (Alpine 3.16, `php:8.1-fpm-alpine` heritage), Magento version
  (2.4.6 CE via `composer.lock`), DB creds (`magentodb`/`magentouser`/`MyPassword`, read from the
  populated `env.php`), dataset sizes (shopping: 49.7G media / 3.6G DB; shopping-admin: 85M media /
  350M DB), and that both instances are already fully initialized (no entrypoint bootstrap needed).
- **Not yet attempted:** the actual Dockerfile + restore-stage + final-stage bucket partitioning,
  and the live A/B proving restored data matches upstream (the equivalent of gitlab's "0 field
  diffs across 2,399 users / 175 projects" proof). These depend on the adjudication above.
