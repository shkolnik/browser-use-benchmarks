# Vendored from markshust/docker-magento (MIT — see LICENSE.md)

Source: https://github.com/markshust/docker-magento, checkout at commit 2d28984
(v53-era, cloned 2026-08-06 to ~/beep-scratch/.../shopping-community-base/docker-magento).
Same vendoring as `../../shopping/vendor-markshust/` — copied here (not shared) so each
image directory's provenance trail is self-contained.

Files vendored (only what this image actually uses):

- `nginx.conf` — from `images/nginx/1.24/conf/nginx.conf`. Basis for `../conf/nginx.conf`
  (adapted: run-as-user, log paths, no image_filter module, plain HTTP).
- `default.conf` — from `images/nginx/1.24/conf/default.conf`. Basis for
  `../conf/magento-site.conf` (adapted: single plain-HTTP server on 7780, no
  SSL/xdebug/livereload, MAGE_ROOT=/opt/magento).
- `LICENSE.md` — the project's MIT license, verbatim.

Additionally the image's base, `markoshust/magento-php:8.1-fpm-9` (pinned by digest in
the Dockerfile), is the published Docker Hub image of the same project's
`images/php/8.1/Dockerfile` — used as-is by digest, not rebuilt, so that Dockerfile is
not vendored here.
