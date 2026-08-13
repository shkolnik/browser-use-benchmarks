#!/bin/bash
# Serve all three WebArena routing profiles from one container.
#
# Upstream runs three separate containers off the SAME image, differing only in
# which /data directory is mounted and which host port maps to 5000. The binary
# and arguments are identical, so three containers buy nothing but a second and
# third copy of the same base image. We run three processes instead and keep the
# port numbers upstream's clients expect (car 5000, bike 5001, foot 5002).
#
# Requires bash >= 5.1 for `wait -n` WITH pid arguments (5.0's wait -n ignores
# them and waits on any child). The base image ships 5.1.4 — verified by running
# it — so this is satisfied but has no margin: re-check on any base-image bump.
#
# Fail-loud: if ANY profile dies the container exits non-zero rather than
# serving a partial routing backend. A benchmark that silently loses foot
# routing would score tasks wrong instead of erroring, which is the exact
# failure mode this repo exists to prevent.
set -uo pipefail

declare -A PORTS=([car]=5000 [bike]=5001 [foot]=5002)
pids=()

for profile in car bike foot; do
  data="/data/$profile/us-northeast-latest.osrm"
  if [ ! -f "$data.mldgr" ]; then
    echo "entrypoint: $data.mldgr missing — the $profile profile was not baked into this image" >&2
    exit 1
  fi
  osrm-routed --algorithm mld --port "${PORTS[$profile]}" "$data" &
  pids+=("$!")
  echo "entrypoint: started $profile on ${PORTS[$profile]} (pid $!)"
done

# `wait -n` returns as soon as the FIRST child exits. Any exit is a failure:
# osrm-routed only returns on error or signal, never on success.
wait -n "${pids[@]}"
status=$?
echo "entrypoint: a routing process exited (status $status) — shutting down" >&2
kill "${pids[@]}" 2>/dev/null
exit "${status:-1}"
