#!/bin/bash
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container. They
# are NOT the address the server binds — Flask listens on 3000 inside the image.
#
# WebShop emits exclusively RELATIVE URLs (no _external / url_root / host_url
# anywhere in web_agent_site), so nothing here is rewritten from these values.
# They are still REQUIRED: the contract is uniform across the fleet so an
# operator never has to remember which images care, and the failure this
# prevents — an image quietly serving somebody else's hostname — is one this
# fleet has already shipped once.
set -euo pipefail

missing=()
[ -n "${HTTP_HOST:-}" ] || missing+=(HTTP_HOST)
[ -n "${HTTP_PORT:-}" ] || missing+=(HTTP_PORT)
if [ "${#missing[@]}" -gt 0 ]; then
  cat >&2 <<EOF
error: ${missing[*]} not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container: the PUBLISHED
  side of the port mapping, not the port the server listens on inside.

  Run it like:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=3000 -p 3000:3000 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

echo "serving http://${HTTP_HOST}:${HTTP_PORT}/ (listening on 3000 in-container)"
exec python -m web_agent_site.app
