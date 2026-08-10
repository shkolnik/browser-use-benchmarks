#!/bin/bash
# The arming half of gitlab's #73 contract; runit-finish.sh is the other half.
#
# Run in the background by entrypoint.sh with the published port as $1. It
# watches the boot, and when the stack is genuinely up it creates the sentinel
# that switches the finish hooks from "record and forgive" to "any exit kills
# the container".
#
# WHY ARMING EXISTS. Measured across a full boot of the published image with
# observe-only hooks: sidekiq exits 1 once while the stack settles, and
# `gitlab-ctl reconfigure` restarts puma, nginx and gitlab-kas — each of which
# handles SIGTERM and therefore exits 0, signal 0, indistinguishable by exit
# status alone from a service dying on its own. Boot is a distinct phase, as the
# image's HEALTHCHECK --start-period already says. After arming, ANY exit is
# fatal, including a clean one.
#
# WHY SERVING IS NOT ENOUGH. reconfigure queues DELAYED restarts that land after
# it returns: measured, nginx was already answering when chef then restarted
# puma, and arming on the HTTP probe alone failed the container on that restart.
# So: every service up AND the same pids in two samples ten seconds apart.
#
# WHY A STACK THAT NEVER SETTLES IS A FAILURE. This used to arm anyway and log a
# warning. That made "nginx never started" — or any service that never starts —
# look like a slow boot: the container stayed up, and it only died later if some
# service happened to exit, which a service that never started never does. The
# EADDRINUSE collision of #84 is the worked example: twenty minutes of restart
# loop, reporting `starting` for most of it, to describe a stack that was never
# going to come up. A boot that has not finished in 15 minutes has not finished;
# this image boots to healthy in 61s measured. Name what is down and fail the
# container the same way a crash does, so the operator gets a diagnosis instead
# of a wait.
set -uo pipefail

PORT=$1
FAILED=/run/gitlab-service-failed
ARMED=/run/gitlab-services-armed

# Both knobs exist for the same reason SUPERVISOR_PID does in runit-finish.sh:
# so the timeout branch can be tested in milliseconds rather than a quarter of
# an hour. In the image they are always the defaults — 90 x 10s.
TRIES=${ARM_TRIES:-90}
NAP=${ARM_SLEEP:-10}

# "svc:pid" for every service, so two samples can be compared for stability.
services_snapshot() {
  gitlab-ctl status 2>/dev/null | sed 's/;.*//' | awk '{print $1 $2 $4}'
}

down_services() {
  gitlab-ctl status 2>/dev/null | awk '/^down:/ {gsub(/:/, "", $2); print $2}' | tr '\n' ' '
}

prev=''
served=no
for _ in $(seq 1 "$TRIES"); do
  sleep "$NAP"
  curl -fsS -o /dev/null --max-time 5 \
    "http://127.0.0.1:${PORT}/users/sign_in" 2>/dev/null || continue
  served=yes
  now=$(services_snapshot)
  case $now in *down:*|'') prev=''; continue ;; esac
  if [ -n "$prev" ] && [ "$now" = "$prev" ]; then
    touch "$ARMED"
    echo "gitlab-services: serving and quiesced; the no-restart contract is now armed"
    exit 0
  fi
  prev=$now
done

# Never settled. Report which half failed — an operator reads a very different
# problem into "nothing ever answered on the port" than into "it answers but the
# service list keeps changing", and the down list names the culprit directly.
down=$(down_services)
if [ "$served" = no ]; then
  why="nothing answered http://127.0.0.1:${PORT}/users/sign_in"
else
  why="it served, but the service list never held still for ${NAP}s"
fi
[ -n "$down" ] && why="$why; services down: ${down% }"

# The same sentinel the finish hook writes, in the same shape: PID 1 prints it
# and parses code=/signal= out of it to choose the container's exit status. Only
# if nothing has claimed it — a real crash that beat us here is the better
# explanation of the two.
if [ ! -e "$FAILED" ]; then
  printf 'gitlab never finished starting within %ss: %s (code=1 signal=0)\n' \
    "$((TRIES * NAP))" "$why" > "$FAILED"
fi
echo "gitlab-services: FATAL: $(cat "$FAILED")" >&2

# Deliberately NOT armed: the container is going down, and arming here would
# let the shutdown's own exits race to overwrite the sentinel above.
kill -TERM "${SUPERVISOR_PID:-1}"
