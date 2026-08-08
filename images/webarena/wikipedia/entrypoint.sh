#!/bin/sh
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container. They
# are NOT the address the server binds — the base image's start.sh hardcodes
# `kiwix-serve --port=80`, so this container always listens on 80 and compose
# publishes that on 8888 (WebArena's port for this service).
#
# Unlike the Magento images, nothing here is rewritten from these values:
# kiwix-serve emits only RELATIVE links. Verified 2026-08-08 against the real
# archive — the welcome page, an article and a search-results page contain no
# self-referencing absolute URL, only the XHTML namespace and a kiwix.org
# credit. They are still required, because "which address do clients use" is a
# question every image in this fleet answers the same way, and an image that
# quietly accepts no answer is the one that later ships somebody else's links.
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
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=8888 -p 8888:80 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

# Build the archive if it is not already there. The command is a separate,
# documented one — `assemble-zim` — because this can cost ~632 s on a first boot,
# and an operator may want to spend that deliberately beforehand rather than on a
# boot somebody is waiting on:
#
#   docker run --rm -v wiki-zim:/zim --entrypoint assemble-zim <image>
#
# Doing it here as well means a plain `docker run` still just works. The script is
# idempotent, so having done it already costs nothing here.
assemble-zim "${1:-}"

echo "serving http://${HTTP_HOST}:${HTTP_PORT}/ (listening on 80 in-container)"
# Hand back to the base image's own chain, with the ZIM path this image's CMD
# supplies still in "$@". dumb-init is what reaps kiwix-serve's children.
exec /usr/bin/dumb-init -- /usr/local/bin/start.sh "$@"
