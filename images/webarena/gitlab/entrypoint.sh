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

# Overridable for the same reason SUPERVISOR_PID is in runit-finish.sh: so the
# validation below can be tested without an omnibus install to read. In the
# image it is always /etc/gitlab/gitlab.rb.
RB=${GITLAB_RB:-/etc/gitlab/gitlab.rb}

# One port is RESERVED in this image, and the failure it causes is silent and
# slow.
#
# Rails runs behind nginx here, and puma binds a TCP port of its own on the
# loopback for it, independent of the port nginx serves on. This is also the ONE
# image whose nginx listener follows HTTP_PORT (every other image in the fleet
# has a fixed internal port and moves only the published side), so an HTTP_PORT
# equal to puma's points both at the same bind. nginx gets there first, puma
# dies with EADDRINUSE, runit restarts it forever, and the container serves 502s
# while looking like it is starting.
#
# Measured on the published image, whose puma was still on omnibus's default of
# 8080, with HTTP_PORT=8080: nginx stable on one pid, a new puma pid every few
# seconds, 12 x `Errno::EADDRINUSE bind(2) for "127.0.0.1" port 8080` in four
# minutes, and — because the stack never settles — the arming fallback below
# firing at 15 minutes, at which point the next puma exit finally takes the
# container down. Twenty minutes to report a misconfiguration knowable here, in
# a millisecond, before anything binds.
#
# restore-stage.sh moves puma off 8080 precisely so that the reserved port is
# one nobody wants to publish. Read from gitlab.rb rather than hardcoded: a
# constant here would silently become a lie the moment that value changed, and
# would then reserve a port puma is not on while admitting the one it is.
puma_port=$(sed -n "s/^[[:space:]]*puma\['port'\][[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p" \
            "$RB" 2>/dev/null | head -n 1)
puma_port=${puma_port:-8080}
if [ "$HTTP_PORT" = "$puma_port" ]; then
  cat >&2 <<EOF
error: HTTP_PORT=$HTTP_PORT collides with GitLab's internal puma port ($puma_port).

  Unlike the rest of the fleet, this image's nginx listens on HTTP_PORT itself,
  and puma already holds $puma_port on the loopback behind it. Both would bind
  the same port: nginx wins, puma crashloops with EADDRINUSE, and the container
  never serves.

  Pick any other port for the PUBLISHED side and keep the mapping symmetric:
    docker run -e HTTP_HOST=$HTTP_HOST -e HTTP_PORT=8023 -p 8023:8023 <image>

  Behind deploy/compose.proxy.yml this is PROXY_PORT, which gitlab follows:
    BENCH_HOST=... PROXY_PORT=8081 docker compose -f deploy/compose.yml \\
        -f deploy/compose.proxy.yml up -d --wait
EOF
  exit 1
fi

want="http://${HTTP_HOST}:${HTTP_PORT}"
have=$(sed -n "s/^external_url[[:space:]]*'\([^']*\)'.*/\1/p" "$RB" | head -n 1)

# sshd refuses to start without its privilege-separation directory and runit
# restarts it about once a second, forever. Measured on the published image:
# 193 failed starts inside four minutes, "Missing privilege separation
# directory: /run/sshd", nothing listening on 22, and a container reporting
# healthy throughout — the crashloop this image has been shipping unnoticed,
# and the reason #78 exists. /run is not persisted, so this belongs here rather
# than only in the Dockerfile.
mkdir -p /run/sshd

# ---- the contract (#73), gitlab's version --------------------------------
# The rest of the fleet gets "a dead service fails the container" from
# run-services.sh. gitlab keeps runit, because runit is omnibus's own
# supervision and gitlab-ctl/reconfigure are built on it, so the same contract
# is implemented with runit's per-service `finish` hook instead.
FAILED=/run/gitlab-service-failed
ARMED=/run/gitlab-services-armed
STOPPING=/run/gitlab-stopping
rm -f "$FAILED" "$ARMED" "$STOPPING"

install_finish_hooks() {
  local d n=0
  for d in /opt/gitlab/sv/*/; do
    install -m 0755 /runit-finish.sh "$d/finish"
    n=$((n + 1))
  done
  echo "gitlab-services: finish hook installed for $n services"
}

# The one weakness of a per-service hook is that it fails OPEN: a service dir
# with no `finish` is simply not covered, and nothing says so. reconfigure
# regenerates service directories, and an omnibus upgrade can add services, so
# coverage is asserted rather than assumed.
assert_hook_coverage() {
  local d missing=()
  for d in /opt/gitlab/sv/*/; do
    [ -x "$d/finish" ] || missing+=("$(basename "$d")")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "gitlab-services: FATAL: no finish hook for: ${missing[*]}" >&2
    echo "  Those services would restart forever on a crash without failing" >&2
    echo "  the container, which is the state #73 removed." >&2
    exit 1
  fi
}

# "svc:pid" for every service, so two samples can be compared for stability.
services_snapshot() {
  gitlab-ctl status 2>/dev/null | sed 's/;.*//' | awk '{print $1 $2 $4}'
}

# Why the hook is armed rather than always live. Measured across a full boot of
# the published image with observe-only hooks: sidekiq exits 1 once while the
# stack settles, and reconfigure restarts puma, nginx and gitlab-kas — each of
# which handles SIGTERM and therefore exits **0, signal 0**, indistinguishable
# by exit status alone from a service dying on its own. Boot is a distinct
# phase, exactly as the image's HEALTHCHECK --start-period already says; the
# contract governs a container that has finished coming up. After arming, ANY
# exit is fatal, including a clean one.
arm_when_serving() {
  local i prev='' now=''
  for i in $(seq 1 90); do
    sleep 10
    curl -fsS -o /dev/null --max-time 5 \
      "http://127.0.0.1:${HTTP_PORT}/users/sign_in" 2>/dev/null || continue
    now=$(services_snapshot)
    # Serving is not enough. `gitlab-ctl reconfigure` queues DELAYED restarts
    # that land after it returns: measured, nginx was already answering when
    # chef then restarted puma, and arming on the HTTP probe alone failed the
    # container on that restart. Require every service up AND the same pids in
    # two samples ten seconds apart, so the boot has actually stopped moving.
    case $now in *down:*|'') prev=''; continue ;; esac
    if [ -n "$prev" ] && [ "$now" = "$prev" ]; then
      touch "$ARMED"
      echo "gitlab-services: serving and quiesced; the no-restart contract is now armed"
      return 0
    fi
    prev=$now
  done
  # Never settled. Arm anyway: a container that hides crashes is worse than one
  # that exits, and the healthcheck reports the rest.
  touch "$ARMED"
  echo "gitlab-services: WARNING: never settled within 15m; arming anyway" >&2
}

on_term() {
  # Non-reentrant, and it says so on disk. Measured: a plain `docker stop`
  # exited 1 instead of 143, because `gitlab-ctl stop` below makes every
  # service exit — the graceful ones with code 0 — and each of those hooks
  # signalled PID 1 again, re-entering this handler, which then found the
  # sentinel a shutdown had just written and reported it as a crash.
  trap '' TERM INT
  touch "$STOPPING"

  if [ -f "$FAILED" ]; then
    echo "gitlab-services: $(cat "$FAILED")" >&2
    gitlab-ctl stop >/dev/null 2>&1 || true
    # The hook records `code=<n> signal=<s>`; report the service's own code,
    # or 128+signal when it was killed, the way a shell does.
    local code sig
    code=$(sed -n 's/.*code=\([-0-9]*\).*/\1/p' "$FAILED")
    sig=$(sed -n 's/.*signal=\([0-9]*\).*/\1/p' "$FAILED")
    if [ "${sig:-0}" -gt 0 ] 2>/dev/null; then exit $((128 + sig)); fi
    [ "${code:-1}" -gt 0 ] 2>/dev/null && exit "$code"
    exit 1
  fi
  echo "gitlab-services: SIGTERM — stopping gitlab"
  gitlab-ctl stop >/dev/null 2>&1 || true
  exit 143
}
trap on_term TERM INT

install_finish_hooks

if [ "$want" = "$have" ]; then
  echo "external_url already '$have' — skipping reconfigure, booting directly"
  assert_hook_coverage
  /opt/gitlab/embedded/bin/runsvdir-start &
  runsvdir_pid=$!
  arm_when_serving &
  wait "$runsvdir_pid"
  exit $?
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
# After chef has rewritten the service tree, not before: reconfigure is the
# thing most likely to have replaced a service directory out from under a hook.
install_finish_hooks
assert_hook_coverage
arm_when_serving &
wait "$runsvdir_pid"
