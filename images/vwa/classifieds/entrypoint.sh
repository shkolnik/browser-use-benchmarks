#!/bin/bash
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container. They
# are NOT the address the server binds — nginx's listen port is fixed inside the
# image — so publishing on a different port (-p 8080:9980 with HTTP_PORT=8080)
# is a supported thing to do.
#
# Osclass needs this: config.php resolves WEB_PATH from the CLASSIFIEDS
# variable, and WEB_PATH is what the app emits into links and asset URLs. That
# value used to be hardcoded to http://127.0.0.1:9980/ in BOTH the image's ENV
# and the compose file, which is a wrong hostname waiting to happen the first
# time anything is published anywhere else.
set -euo pipefail

missing=()
[ -n "${HTTP_HOST:-}" ] || missing+=(HTTP_HOST)
[ -n "${HTTP_PORT:-}" ] || missing+=(HTTP_PORT)
if [ "${#missing[@]}" -gt 0 ]; then
  cat >&2 <<EOF
error: ${missing[*]} not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container: the PUBLISHED
  side of the port mapping, not the port the server listens on inside. Osclass
  bakes them into every link and asset URL it serves.

  Run it like:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=9980 -p 9980:9980 <image>

  Publishing on another port is fine — match HTTP_PORT to the PUBLISHED side:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=8080 -p 8080:9980 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

# config.php reads this with getenv() on every request, so exporting it here is
# the whole configuration step — there is no cached copy to invalidate.
export CLASSIFIEDS="http://${HTTP_HOST}:${HTTP_PORT}/"
echo "serving ${CLASSIFIEDS} (listening on 9980 in-container)"

# Two services, no supervisor daemon: if either exits, so does this container,
# with that service's exit code (#73). Both were `autorestart=true` under
# supervisord, which meant a crashed mysqld came back as an empty-ish server
# while the container still reported healthy.
. /run-services.sh

# The datadir was populated at BUILD time, so this is a plain start: no initdb,
# no dump load. mysqld drops to the mysql user itself, hence no --user here.
svc_start mysqld -- /usr/sbin/mysqld --user=mysql --datadir=/var/lib/mysql \
  --skip-networking

# PHP's built-in server, which is what upstream serves with — no nginx or
# apache anywhere in the upstream image. It handles one request at a time;
# upstream has the same property, and PHP_CLI_SERVER_WORKERS is the knob if
# that ever needs revisiting. `-t` states the document root that supervisord's
# `directory=` used to set as the working directory; for `php -S` those are the
# same thing, and naming it beats depending on an inherited cwd.
svc_start php --user www-data -- \
  /usr/local/bin/php -S 0.0.0.0:9980 -t /usr/src/myapp

svc_supervise
