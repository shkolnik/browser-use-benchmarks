#!/bin/sh
# Installed as the `finish` script of every runit service in this image.
#
# runit runs this each time a service's run process exits, with $1 = the exit
# code (-1 when it died from a signal) and $2 = the signal number (0 on a
# normal exit). Without it, runsv simply restarts the service, forever: a
# crashlooping puma leaves a container that is "up" and healthy-looking while
# serving nothing, which is #78 exactly.
#
# This is gitlab's implementation of the same contract the rest of the fleet
# gets from run-services.sh (#73): a service that dies takes the container with
# it. gitlab keeps runit rather than that script because runit is omnibus's
# own supervision — gitlab-ctl and `gitlab-ctl reconfigure` are built on it,
# and replacing it means fighting Chef.
#
svc=$(basename "$(pwd)")

# Until the entrypoint says the stack is up, exits are recorded and forgiven.
# Boot is a distinct phase — the image's HEALTHCHECK --start-period says so
# already — and a measured boot of this image contains real ones: sidekiq exits
# 1 once while settling, and reconfigure restarts puma, nginx and gitlab-kas.
#
# Exit status cannot tell those apart from a crash. This was the plan's one
# wrong assumption: `sv stop` sends SIGTERM, but a service that HANDLES SIGTERM
# exits 0 with signal 0, which is what those three did. So there is no
# "intentional stop" signature to filter on, and arming is what separates the
# phases instead.
if [ ! -e /run/gitlab-services-armed ]; then
  echo "gitlab-services: '$svc' exited (code=$1 signal=$2) during boot; runit will restart it" >&2
  exit 0
fi

# SIGTERM after arming means someone stopped the whole container; PID 1 is
# already shutting everything down and will decide the exit code.
[ "$2" = "15" ] && exit 0

# A shutdown in progress: PID 1 set this before running `gitlab-ctl stop`. The
# services that handle SIGTERM exit 0 rather than dying from it, so without
# this they look exactly like crashes and turn every `docker stop` into a
# reported failure — measured, exit 1 instead of 143.
[ -e /run/gitlab-stopping ] && exit 0

# FIRST failure wins. Measured: killing puma took the container down correctly,
# but the shutdown that followed ran every other service's hook, and the ones
# that handle SIGTERM exit 0 — each overwrote this file, so the container
# reported exit 1 for a "code=0" instead of 137 for the SIGKILL that actually
# happened. The first writer is the one that explains the failure.
[ -e /run/gitlab-service-failed ] && exit 0

printf '%s exited with code=%s signal=%s\n' "$svc" "$1" "$2" > /run/gitlab-service-failed
echo "gitlab-services: FATAL: service '$svc' exited (code=$1 signal=$2); failing the container" >&2

# PID 1 is the entrypoint, which traps this, brings runit down and exits
# non-zero. Signalling it rather than exiting here is what turns one dead
# service into a dead container. The variable exists so the decision table
# above can be tested without a container to be PID 1 of; in the image it is
# always 1.
kill -TERM "${SUPERVISOR_PID:-1}"
