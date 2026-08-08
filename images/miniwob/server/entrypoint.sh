#!/bin/sh
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container. They
# are NOT the address the server binds — nginx's listen port is fixed inside the
# image — so publishing on a different port (-p 8080:8399 with HTTP_PORT=8080)
# is a supported thing to do.
#
# miniwob serves static task HTML whose asset paths are all relative, so nothing
# here is rewritten from these values. They are still REQUIRED: the contract is
# uniform across the fleet so that an operator never has to remember which
# images care, and a silently-ignored variable is how a wrong hostname reaches
# an agent unnoticed.
#
# /bin/sh, not bash: this is nginx:alpine and there is no bash.
set -eu

missing=
[ -n "${HTTP_HOST:-}" ] || missing="$missing HTTP_HOST"
[ -n "${HTTP_PORT:-}" ] || missing="$missing HTTP_PORT"
if [ -n "$missing" ]; then
  cat >&2 <<EOF
error:$missing not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container: the PUBLISHED
  side of the port mapping, not the port the server listens on inside.

  Run it like:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=8399 -p 8399:8399 <image>

  Publishing on another port is fine — match HTTP_PORT to the PUBLISHED side:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=8080 -p 8080:8399 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

echo "serving http://${HTTP_HOST}:${HTTP_PORT}/ (listening on 8399 in-container)"
exec nginx -g 'daemon off;'
