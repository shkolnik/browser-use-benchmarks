#!/bin/bash
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container.
#
# GitLab is the fleet's hard case. Its served URL is `external_url` in
# /etc/gitlab/gitlab.rb, and that value is not read live — it is baked into
# rendered config (nginx vhost, gitlab.yml, ...) by `gitlab-ctl reconfigure`, a
# chef run. Setting the documented EXTERNAL_URL env var does NOT help: the only
# reader of ENV['EXTERNAL_URL'] in the whole omnibus tree of 15.7.5 is
# omnibus-ctl/upgrade.rb, reachable only through that same reconfigure.
#
# So this image pays for the contract rather than faking it: when the requested
# address differs from the baked one, reconfigure. Measured cost of that chef
# run for a real external_url change: ~28s (6/741 resources updated, restarting
# puma, sidekiq, gitlab-kas and nginx). When it matches, we skip it entirely and
# boot exactly as fast as before.
#
# ORDER IS LOAD-BEARING. Upstream's own /assets/wrapper starts runsvdir-start
# FIRST and only then reconfigures, because chef's service resources restart
# through runit — reconfiguring with nothing supervising leaves those actions
# with no runit to talk to. This image normally runs runsvdir-start as its whole
# command (it ships a pre-enabled service tree so it does not have to
# reconfigure at boot), so the reconfigure path has to reproduce upstream's
# order: supervise first, reconfigure second, then hand PID 1 back to runit.
set -euo pipefail

missing=()
[ -n "${HTTP_HOST:-}" ] || missing+=(HTTP_HOST)
[ -n "${HTTP_PORT:-}" ] || missing+=(HTTP_PORT)
if [ "${#missing[@]}" -gt 0 ]; then
  cat >&2 <<EOF
error: ${missing[*]} not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container: the PUBLISHED
  side of the port mapping. GitLab bakes this into every link it serves, so a
  wrong value here is not cosmetic.

  Run it like:
    docker run -e HTTP_HOST=127.0.0.1 -e HTTP_PORT=8023 -p 8023:8023 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

RB=/etc/gitlab/gitlab.rb
want="http://${HTTP_HOST}:${HTTP_PORT}"
have=$(sed -n "s/^external_url[[:space:]]*'\([^']*\)'.*/\1/p" "$RB" | head -n 1)

if [ "$want" = "$have" ]; then
  echo "external_url already '$have' — skipping reconfigure, booting directly"
  exec /opt/gitlab/embedded/bin/runsvdir-start
fi

# The dataset this image restores from was captured with CMU's own hostname
# baked in (http://metis.lti.cs.cmu.edu:8023), so on the fleet's default
# settings this branch is the one that runs, and it is also what stops the
# image serving links that point at a third party's host.
echo "external_url: '${have:-<unset>}' -> '$want' — running gitlab-ctl reconfigure (~28s)"
if [ -n "$have" ]; then
  sed -i "s|^external_url[[:space:]].*|external_url '$want'|" "$RB"
else
  printf "external_url '%s'\n" "$want" >> "$RB"
fi

# Supervise first (see the ORDER note above), reconfigure against it, then let
# runit keep running as this script's foreground child. `wait` rather than
# `exec` because reconfigure has to run while runsvdir is already up.
/opt/gitlab/embedded/bin/runsvdir-start &
runsvdir_pid=$!
gitlab-ctl reconfigure
echo "reconfigure complete; external_url is now '$want'"
wait "$runsvdir_pid"
