#!/bin/sh
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container. They
# are NOT the address the server binds — nginx listens on 80 inside this image,
# and compose publishes that on 9999. Those are different numbers on purpose,
# and this is the image where confusing them is easiest.
set -eu

missing=
[ -n "${HTTP_HOST:-}" ] || missing="$missing HTTP_HOST"
[ -n "${HTTP_PORT:-}" ] || missing="$missing HTTP_PORT"
if [ -n "$missing" ]; then
  cat >&2 <<EOF
error:$missing not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container: the PUBLISHED
  side of the port mapping. This image listens on 80 INSIDE the container, so
  the published port is normally a different number.

  Run it like:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=9999 -p 9999:80 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

echo "serving http://${HTTP_HOST}:${HTTP_PORT}/ (listening on 80 in-container)"
exec supervisord -c /etc/supervisord.conf -n
