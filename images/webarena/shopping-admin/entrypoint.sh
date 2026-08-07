#!/bin/bash
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container. They
# are NOT the address the server binds — nginx's listen port is fixed inside the
# image — so publishing on a different port (-p 8080:7780 with HTTP_PORT=8080)
# is a supported thing to do, and the links Magento emits will be right.
#
# The port is always written out. There is no "80 is implied" case to reason
# about, and no scheme to choose either: nothing in this fleet serves TLS.
#
# Requiring them is the point. Magento cannot emit host-relative links — every
# page carries an absolute base_url in BASE_URL and in every static asset URL —
# so SOME hostname is always baked into what agents receive. A default would
# just be a wrong hostname that nobody was told about, which is exactly how this
# dataset arrived: it still named metis.lti.cs.cmu.edu, silently 302'd every
# request off-host, and cost two CI builds to track down.
set -euo pipefail

missing=()
[ -n "${HTTP_HOST:-}" ] || missing+=(HTTP_HOST)
[ -n "${HTTP_PORT:-}" ] || missing+=(HTTP_PORT)
if [ "${#missing[@]}" -gt 0 ]; then
  cat >&2 <<EOF
error: ${missing[*]} not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container. Magento bakes
  them into every link and asset URL it serves, so guessing them would hand
  agents URLs that quietly point somewhere else.

  Run it like:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=7780 -p 7780:7780 <image>

  Publishing on another port is fine — match HTTP_PORT to the PUBLISHED side:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=8080 -p 8080:7780 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

BASE_URL="http://${HTTP_HOST}:${HTTP_PORT}/"
MAGE=/opt/magento

# nginx is autostart=false in supervisord.conf so that nothing can be served
# under the wrong base_url: the config below has to land first. Everything else
# starts normally, because the config is applied through Magento's CLI, which
# needs mariadb and redis up to do its work.
/usr/bin/supervisord -c /etc/supervisord.conf &
SUP_PID=$!
sctl() { supervisorctl -c /etc/supervisord.conf "$@"; }

for i in $(seq 1 60); do
  mariadb-admin --socket=/run/mysqld/mysqld.sock ping >/dev/null 2>&1 && break
  [ "$i" = 60 ] && { echo "mariadb never came up" >&2; sctl status >&2 || true; exit 1; }
  sleep 1
done

# Idempotent on purpose: a restart that changes nothing should not pay for a
# cache flush, and the shipped image is already built with a working base_url.
current=$(mariadb -N -e \
  "SELECT value FROM magentodb.core_config_data
    WHERE path='web/unsecure/base_url' AND scope='default'" 2>/dev/null || true)
if [ "$current" = "$BASE_URL" ]; then
  echo "base_url already $BASE_URL — no reconfiguration needed"
else
  echo "setting base_url $current -> $BASE_URL"
  cd "$MAGE"
  for setting in \
    "web/unsecure/base_url $BASE_URL" \
    "web/secure/base_url $BASE_URL"
  do
    # shellcheck disable=SC2086 # deliberate split: path and value are separate args
    runuser -u app -- php bin/magento config:set $setting
  done
  runuser -u app -- php bin/magento cache:flush
fi

# Only now is it safe to accept traffic.
sctl start nginx
echo "serving $BASE_URL"
wait "$SUP_PID"
