# shellcheck shell=bash
#
# Minimal service supervisor for this fleet's all-in-one images. Sourced, not
# executed: the image's entrypoint stays PID 1 and this adds the verbs.
#
# THE CONTRACT (#73). Every service here is expected to run forever. If any one
# of them exits — any code, any signal, expected or not — the container exits
# too, with that service's exit code, after naming it. There is no restart.
#
# Why no restart: Docker's ecosystem assumes one core process per container, so
# "the container is running" is supposed to mean "the thing it serves is up".
# These images violate the one-process rule for good reasons, and a supervisor
# that restarts a crashed service (or, worse, gives up on it and keeps running)
# breaks that equivalence: the container stays green while serving from a
# degraded state. For a benchmark fixture that is the worst outcome available —
# results computed against half a stack are wrong rather than missing. Exiting
# hands recovery back to whoever started the container, which is exactly what
# the single-process images in this fleet already do for free.
#
# Usage, from an entrypoint:
#
#   . /run-services.sh
#   svc_start mariadb --user mysql --log /var/log/supervisor/mariadb.log -- \
#     /usr/sbin/mariadbd
#   svc_start nginx -- /usr/sbin/nginx -g 'daemon off;'
#   svc_supervise                      # never returns
#
# Build stages that drive services by hand (load a dump, then shut down
# cleanly) use the same verbs without svc_supervise:
#
#   svc_stop nginx php-fpm    svc_stop_all    svc_status

set -u

declare -A SVC_PID=()   # name -> pid
declare -A SVC_OF=()    # pid  -> name
SVC_ORDER=()            # start order, for a reverse-order shutdown
SVC_PRE=()              # scratch: privilege-drop prefix for the next start

svc__die() { echo "run-services: $*" >&2; exit 1; }

# Fills SVC_PRE with the words that run a command as another user.
#
# Every candidate must REPLACE itself with the service (exec, no fork), so that
# $! is the service's own pid: a wrapper that forks and waits would still
# forward the exit code, but signals sent on shutdown would land on the wrapper
# instead of the service. su-exec and util-linux's setpriv exec; runuser is
# last because it may fork to run a PAM session.
#
# Each candidate is PROBED, not merely found on PATH. Alpine ships busybox's
# setpriv, a different program that rejects --reuid outright — measured in the
# reddit image, where trusting `command -v setpriv` would have failed every
# service that drops privileges. Probing as the real target user also turns a
# misspelled or missing account into a loud failure at startup.
svc__priv_prefix() {
  local user=$1 tool
  for tool in su-exec setpriv runuser; do
    command -v "$tool" >/dev/null 2>&1 || continue
    case $tool in
      su-exec) SVC_PRE=(su-exec "$user") ;;
      setpriv) SVC_PRE=(setpriv --reuid "$user" --regid "$user" --init-groups --) ;;
      runuser) SVC_PRE=(runuser -u "$user" --) ;;
    esac
    "${SVC_PRE[@]}" true >/dev/null 2>&1 && return 0
  done
  svc__die "cannot run services as '$user': no working su-exec, setpriv or runuser"
}

# svc_start NAME [--user USER] [--log FILE] -- command...
svc_start() {
  local name=$1; shift
  local user='' log=''
  while [ $# -gt 0 ]; do
    case $1 in
      --user) user=$2; shift 2 ;;
      --log)  log=$2;  shift 2 ;;
      --)     shift; break ;;
      *)      svc__die "svc_start $name: unexpected argument '$1'" ;;
    esac
  done
  [ $# -gt 0 ] || svc__die "svc_start $name: no command given"
  [ -z "${SVC_PID[$name]:-}" ] || svc__die "svc_start $name: already running"

  SVC_PRE=()
  [ -z "$user" ] || svc__priv_prefix "$user"

  # NEVER pipe a service. `mysqld | sed 's/^/[db] /' &` makes $! the sed
  # process, so this supervisor would watch sed and report sed's exit status —
  # the contract would keep passing its tests and silently stop holding. A
  # redirect to a file keeps $! the service, which is why --log is a redirect.
  if [ -n "$log" ]; then
    "${SVC_PRE[@]}" "$@" >>"$log" 2>&1 &
  else
    "${SVC_PRE[@]}" "$@" &
  fi

  local pid=$!
  SVC_PID[$name]=$pid
  SVC_OF[$pid]=$name
  SVC_ORDER+=("$name")
  echo "run-services: started $name (pid $pid)${log:+, logging to $log}"
}

# Stop services on purpose. Deregistering before the kill keeps svc_status and
# svc_stop_all honest about what is left; it is not what stops an intentional
# stop reading as a crash. `wait` below reaps the pid here, so svc_supervise
# never sees it again either way — measured, by deleting this unset and
# watching every test stay green.
svc_stop() {
  local name pid
  for name in "$@"; do
    pid=${SVC_PID[$name]:-}
    if [ -z "$pid" ]; then
      echo "run-services: $name is not running"
      continue
    fi
    unset "SVC_PID[$name]" "SVC_OF[$pid]"
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    echo "run-services: stopped $name"
  done
}

# Reverse start order, so a database goes down after the things talking to it.
svc_stop_all() {
  local i
  for (( i=${#SVC_ORDER[@]}-1; i>=0; i-- )); do
    [ -z "${SVC_PID[${SVC_ORDER[$i]}]:-}" ] || svc_stop "${SVC_ORDER[$i]}"
  done
}

svc_status() {
  local name pid
  for name in "${SVC_ORDER[@]}"; do
    pid=${SVC_PID[$name]:-}
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "$name: RUNNING (pid $pid)"
    else
      echo "$name: NOT RUNNING"
    fi
  done
}

svc__on_term() {
  echo "run-services: received SIGTERM, stopping services"
  svc_stop_all
  # 128+15, the code a process killed by SIGTERM reports. `docker stop` on a
  # healthy container lands here, and that is not a failure.
  exit 143
}

# Waits for the first service to exit, then takes the container down with it.
# Never returns.
svc_supervise() {
  [ ${#SVC_ORDER[@]} -gt 0 ] || svc__die "svc_supervise: no services were started"
  trap svc__on_term TERM INT

  local dead code name
  while :; do
    dead=''
    code=0
    # `|| code=$?` rather than a bare wait: every entrypoint sourcing this runs
    # under `set -e`, where a service exiting non-zero would make errexit kill
    # this shell right here — with the right code, but with no log line saying
    # which service died and no shutdown of the others.
    wait -n -p dead || code=$?
    # A signal interrupted the wait: the trap has already run and exited.
    [ -n "$dead" ] || continue
    # Belt and braces. Measured at PID 1 with eight orphaned grandchildren:
    # bash reaps them but `wait -n` reports only its own jobs, so this never
    # fires today. It costs one comparison and is what stands between a
    # grandchild's exit code and a container that dies for no reason.
    name=${SVC_OF[$dead]:-}
    [ -n "$name" ] || continue

    unset "SVC_PID[$name]" "SVC_OF[$dead]"
    if [ "$code" -eq 0 ]; then
      # Exiting cleanly is still being gone, and propagating 0 would make the
      # container indistinguishable from a `docker stop`.
      echo "run-services: FATAL: service '$name' exited 0 — services are" \
           "expected to run forever; failing the container" >&2
      code=1
    elif [ "$code" -gt 128 ]; then
      echo "run-services: FATAL: service '$name' was killed by signal" \
           "$((code - 128)); failing the container" >&2
    else
      echo "run-services: FATAL: service '$name' exited $code;" \
           "failing the container" >&2
    fi
    svc_stop_all
    exit "$code"
  done
}
