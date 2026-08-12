#!/bin/bash
# In-container readiness probe for the routing backend.
#
# There is no HTTP client in this image to probe with: the osrm-backend base
# ships neither curl nor wget nor nc (verified — `command -v curl wget nc
# python3` finds nothing). It does ship bash 5.1.4, so the request goes over
# bash's own /dev/tcp, which needs nothing installed.
#
# Every profile is checked, not just car. The entrypoint already exits if a
# profile DIES, but a profile that never finished loading its graph is a
# different failure, and `docker compose up --wait` is only as honest as this
# script: reporting healthy while foot routing is still unavailable would let
# smoke pass an image that cannot serve the tasks it exists for.
set -u

declare -A PORTS=([car]=5000 [bike]=5001 [foot]=5002)
# Pittsburgh CMU -> a point east: the same request upstream's own boot-init
# uses to verify the routing servers, and the one image.toml polls from the
# host. `overview=false` keeps the response small.
PATH_Q='/route/v1/driving/-79.9959,40.4406;-79.9,40.45?overview=false'

for profile in car bike foot; do
  port="${PORTS[$profile]}"
  exec 3<>"/dev/tcp/127.0.0.1/$port" || { echo "healthcheck: $profile ($port) not listening" >&2; exit 1; }
  # `Connection: close` is load-bearing, not decorative. Reading to EOF is how
  # this avoids parsing Content-Length, and osrm-routed does NOT close a
  # keep-alive-less HTTP/1.0 socket when it has finished answering — it holds it
  # for its own ~5s idle timeout first. Measured: 5s per profile, 15s for three,
  # which is precisely a 15s HEALTHCHECK timeout, so the container reported
  # unhealthy while its own log showed all three profiles answering 200 in 6ms.
  # Asking for the close makes the reply immediate.
  #
  # The `timeout` is the belt to that braces: a probe must fail on its own terms
  # rather than be killed by dockerd, whose kill leaves no message anywhere.
  printf 'GET %s HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n' "$PATH_Q" >&3
  body=$(timeout 5 cat <&3)
  exec 3<&-
  # OSRM answers 200 with {"code":"NoRoute",…} for a request it understood but
  # could not route, so the status line is not enough — match the code itself.
  case "$body" in
    *'"code":"Ok"'*) ;;
    *) echo "healthcheck: $profile ($port) did not route: ${body##*$'\r\n\r\n'}" >&2; exit 1 ;;
  esac
done
echo "healthcheck: car, bike and foot all routing"
