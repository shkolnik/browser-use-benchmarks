# shellcheck shell=bash
#
# Shared derived-inputs cache protocol for this fleet's seven derive scripts.
# Sourced, not executed — every function here must behave correctly whether
# the caller runs under `set -euo pipefail` (every derive-backup.sh does) or
# a looser mode, so a failure that should be fatal is a bare statement or an
# explicit `exit`.
#
# ONE FORMAT: an oras artifact. This fleet's cache entries used to be `FROM
# scratch` Docker images read back with `docker create` + `docker export | tar`
# (#79 is why that extract needed a wildcard filter), and for one wave this
# library read BOTH formats, re-pushing a legacy hit as oras so the fleet
# converted on ordinary rebuilds. That wave is over: all seven entries in
# builder/derived-cache.lock were confirmed `artifactType` artifacts against the
# live registry on 2026-08-11, so the legacy reader, its migration push and the
# DCACHE_NO_MIGRATE opt-out are gone rather than kept as untested code that
# nothing on the fleet can reach. A tag that is somehow NOT an artifact is now
# a loud failure, not a silent miss — see dcache_pull.
#
# Usage, from a derive script (after `set -euo pipefail`):
#
#   . "$REPO_ROOT/builder/stage-lib/derive-cache.sh"
#   if dcache_pull "$CACHE" "$DATASETS_DIR"; then
#     ... reassemble split parts, as before ...
#     exit 0
#   fi
#   ... derive ...
#   dcache_push "$CACHE" "$work" "${files[@]}"
#
# Env:
#   DCACHE_TOOL_DIR          where the pinned oras binary is cached (default
#                             $HOME/.cache/derive-cache)
#   DCACHE_SKIP_ORAS_INSTALL set to skip dcache_ensure_oras entirely — tests
#                             put a fake `oras` on PATH and must never reach
#                             for the network
#   DCACHE_RETRY_SLEEP       seconds between push retries (default 30) —
#                             overridable so a test does not sleep 90s
#   DCACHE_MIN_BPS           bytes/sec below which a transfer counts as stalled
#                             (default 102400 = 100 KB/s)
#   DCACHE_STALL_SECS        how long it must stay below that before curl gives
#                             up and the pass resumes it (default 60)
#   DCACHE_PROGRESS_SECS     seconds between progress lines (default 30; 0
#                             disables the watcher)

set -u

# Pinned: oras v1.3.3 (released 2026-07-10), linux/amd64. The checksum below
# is copied from the release's own published checksums file —
# https://github.com/oras-project/oras/releases/download/v1.3.3/oras_1.3.3_checksums.txt
# — and was independently re-verified by downloading the tarball and running
# `sha256sum` on it locally (matched, 2026-08-08). Never bump this pin without
# repeating both checks: it is the only thing standing between a compromised
# release asset and every derive script on the fleet.
_DCACHE_ORAS_VERSION=1.3.3
_DCACHE_ORAS_SHA256=9ce999f8d2de03fc03968b29d743077a58783e545e5eaa53917ca177352d0e59
_DCACHE_ORAS_URL="https://github.com/oras-project/oras/releases/download/v${_DCACHE_ORAS_VERSION}/oras_${_DCACHE_ORAS_VERSION}_linux_amd64.tar.gz"

dcache__die() { echo "derive-cache: $*" >&2; exit 1; }

# dcache__transfer LABEL TRIES CMD...
#
# Run CMD, retrying a failure up to TRIES times. The last attempt's combined
# output is left in _DCACHE_LAST_OUT so the caller can put it in a diagnostic;
# it is NOT printed on success, because a 13-layer docker pull's progress
# chatter is noise once it worked.
#
# TRIES is a parameter rather than a constant because the caller knows
# something this function cannot: whether the thing being transferred is known
# to exist. Retrying an absent entry is pure latency.
_DCACHE_LAST_OUT=
dcache__transfer() {
  local label=$1 tries=$2; shift 2
  local i
  for (( i=1; i<=tries; i++ )); do
    if _DCACHE_LAST_OUT=$("$@" 2>&1); then
      [ "$i" = 1 ] || echo "derive-cache: $label succeeded on attempt $i" >&2
      return 0
    fi
    # The WHOLE output, and on the LAST attempt too. Both were wrong in run
    # 31460767854: it burned ~30 minutes failing wikipedia's pull three times
    # and the log still could not say how far any attempt got, because this
    # printed `tail -n 3` of a CONCURRENT `oras pull` — three progress lines
    # chosen by interleaving, not the three that mattered — and printed
    # nothing at all for the final attempt, the one whose failure is fatal.
    # A transfer worth retrying for half an hour is worth its ~30 lines.
    echo "derive-cache: $label failed (attempt $i/$tries)" >&2
    printf '%s\n' "$_DCACHE_LAST_OUT" >&2
    [ "$i" = "$tries" ] || sleep "${DCACHE_RETRY_SLEEP:-30}"
  done
  return 1
}

# dcache__layers — reads a manifest on stdin, prints one
# `digest<TAB>size<TAB>filename` line per layer that names a file.
#
# Only layers carrying `org.opencontainers.image.title` become files; that is
# oras's own rule, and matching it is what keeps a blob-by-blob fetch
# equivalent to `oras pull`.
#
# The title comes from the registry, and it is used as a PATH. A title of
# `../../etc/cron.d/x` would otherwise write outside DEST_DIR, so anything
# that is not a bare filename is refused rather than sanitised — every entry
# this fleet pushes is a basename, so a title that isn't one means something
# is wrong that quietly stripping slashes would hide.
dcache__layers() {
  python3 -c '
import json, sys
try:
    m = json.load(sys.stdin)
except ValueError as e:
    sys.exit("derive-cache: manifest is not JSON: %s" % e)
for layer in m.get("layers", []):
    title = (layer.get("annotations") or {}).get("org.opencontainers.image.title")
    if not title:
        continue
    if "/" in title or title in (".", ".."):
        sys.exit("derive-cache: refusing layer title %r: not a bare filename" % title)
    print("%s\t%d\t%s" % (layer["digest"], layer["size"], title))
'
}

# dcache__scheme REGISTRY
#
# localhost is implicitly insecure — the same rule Docker itself applies, and
# what lets the round-trip test run against a plain-HTTP `registry:2`.
dcache__scheme() {
  case $1 in
    localhost|localhost:*|127.0.0.1|127.0.0.1:*) echo http ;;
    *) echo https ;;
  esac
}

# dcache__registry_creds REGISTRY — prints `user:password`, or nothing.
#
# Read from the Docker config `docker login` already writes, so this library
# needs no credentials of its own; on the runner that is the GITHUB_TOKEN the
# `login to ghcr` step logs in with. Nothing is printed when the entry is
# missing or held by a credential helper, and an anonymous token is tried
# instead — which is enough for a public package, and for a private one the
# fetch then fails loudly rather than writing a 401 body into a blob file.
dcache__registry_creds() {
  python3 - "$1" <<'PY'
import base64, json, os, sys
registry = sys.argv[1]
path = os.path.join(os.environ.get("DOCKER_CONFIG") or
                    os.path.expanduser("~/.docker"), "config.json")
try:
    with open(path) as fh:
        auths = (json.load(fh) or {}).get("auths") or {}
except (OSError, ValueError):
    sys.exit(0)
for key in (registry, "https://" + registry, "https://" + registry + "/v1/"):
    entry = auths.get(key) or {}
    if entry.get("auth"):
        try:
            print(base64.b64decode(entry["auth"]).decode())
        except (ValueError, UnicodeDecodeError):
            pass
        break
PY
}

# dcache__token REGISTRY REPO_PATH — prints a pull-scope bearer token, or
# nothing when the registry does not use token auth (a local `registry:2`
# wants no Authorization header at all, and sending one is not an error).
dcache__token() {
  local registry=$1 repo_path=$2 creds out url
  url="$(dcache__scheme "$registry")://$registry/token?service=$registry&scope=repository:$repo_path:pull"
  creds=$(dcache__registry_creds "$registry")
  # Bounded, because this runs before every blob fetch and a hung token request
  # stalls the pull just as completely as a hung blob does — with none of the
  # progress reporting, since no file is growing to watch.
  local -a t=(--connect-timeout 20 --max-time 60)
  if [ -n "$creds" ]; then
    out=$(curl -fsS "${t[@]}" -u "$creds" "$url" 2>/dev/null) || return 0
  else
    out=$(curl -fsS "${t[@]}" "$url" 2>/dev/null) || return 0
  fi
  printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
print(d.get("token") or d.get("access_token") or "")
'
}

# dcache__fetch_blob REPO DIGEST SIZE OUT
#
# One blob, resumed from whatever is already in OUT. This is the level below
# `oras blob fetch`, and it exists because blob granularity turned out not to
# be fine enough — see the note on dcache__fetch_blobs.
#
# The resume is a byte range and the check is the whole digest, which is the
# only safe combination: a `Range` request can be answered by a different
# backend than served the first half, a leftover file can be anything, and
# appending to the wrong prefix produces a file of exactly the right SIZE and
# the wrong content. So nothing is trusted until sha256 over the assembled
# file matches, and a file that fails is deleted rather than resumed again —
# otherwise a bad prefix is immortal, re-appended every pass forever.
dcache__fetch_blob() {
  local repo=$1 digest=$2 size=$3 out=$4
  local registry=${repo%%/*} repo_path=${repo#*/}
  local url have token code rc got
  url="$(dcache__scheme "$registry")://$registry/v2/$repo_path/blobs/$digest"

  have=0
  # An `&&` one-liner here would return 1 when the file is absent, and every
  # caller of this library runs under `set -e`.
  if [ -f "$out" ]; then have=$(stat -c %s "$out"); fi
  # At or past the full length and still not verified (the caller checked
  # before calling), so the bytes are wrong, not incomplete. Resuming from
  # here would ask for a range past the end and 416; start clean instead.
  if [ "$have" -ge "$size" ]; then
    rm -f "$out"
    have=0
  fi

  local -a auth=()
  token=$(dcache__token "$registry" "$repo_path")
  [ -z "$token" ] || auth=(-H "Authorization: Bearer $token")

  # -C - resumes at the current file length; without it curl truncates, which
  # is the whole bug. -L because a registry answers a blob GET with a redirect
  # to storage (curl drops the Authorization header across hosts, which is
  # correct — the redirect URL carries its own signature).
  #
  # --speed-limit/--speed-time is what makes the resume above REACHABLE. curl
  # has no transfer timeout by default, so a connection that is blackholed
  # rather than reset — which is what a link failing over mid-flow does to a
  # connection that already exists — leaves curl blocked in recv() on a socket
  # the kernel still thinks is open, for however long tcp_retries2 takes to
  # expire (~15 min, unbounded if the peer keeps the window alive). Nothing has
  # failed, so nothing retries: the pass counter never advances and the job
  # looks hung rather than slow. Falling under 100 KB/s for a solid minute is
  # not a slow link — a healthy transfer here runs orders of magnitude above
  # that — it is a dead one, and the right response is to drop the socket and
  # resume, which now costs only the bytes in flight.
  local -a limit=(--connect-timeout 30
                  --speed-limit "${DCACHE_MIN_BPS:-102400}"
                  --speed-time "${DCACHE_STALL_SECS:-60}")
  : >>"$out"
  if code=$(curl -fsS -L -C - "${limit[@]}" "${auth[@]}" -w '%{http_code}' -o "$out" "$url"); then
    rc=0
  else
    rc=$?
  fi

  if [ "$rc" != 0 ]; then
    # 416 means the server would not honour the range, and 33 is curl's own
    # "cannot resume". Both leave a file that can never complete, so drop it
    # and let the next pass start from zero rather than loop on it.
    case "$code:$rc" in
      416:*|*:33)
        rm -f "$out"
        echo "derive-cache: range request refused (HTTP $code, curl $rc) —" \
             "restarting $(basename "$out") from zero next pass" >&2
        ;;
    esac
    return "$rc"
  fi

  got=$(stat -c %s "$out")
  if [ "$got" != "$size" ]; then
    echo "derive-cache: $(basename "$out") ended at $got of $size bytes" >&2
    return 1
  fi
  if [ "$(sha256sum <"$out" | cut -d' ' -f1)" != "${digest#sha256:}" ]; then
    rm -f "$out"
    echo "derive-cache: $(basename "$out") completed but its sha256 does not" \
         "match $digest — discarded, not resumed" >&2
    return 1
  fi
  return 0
}

# dcache__watch FILE SIZE LABEL PARENT_PID
#
# Runs in the background for the duration of one blob and reports how far the
# file has got, so that a slow transfer and a dead one are DIFFERENT LINES in
# the log rather than the same silence. Before this, a 95 GB pull printed
# nothing between "resuming" and its outcome — for wikipedia that is a
# multi-hour gap in which "working at 3 MB/s", "blocked on a blackholed
# socket" and "hashing a 9.6 GB file" are indistinguishable from outside.
#
# It watches the FILE rather than parsing curl's progress meter because curl's
# output goes into dcache__transfer's capture buffer and is only ever printed
# on failure — the file length is the one signal available from out here, and
# it happens to be the one that matters: bytes on disk.
dcache__watch() {
  local f=$1 size=$2 label=$3 parent=$4
  local every=${DCACHE_PROGRESS_SECS:-30}
  [ "$every" -gt 0 ] || return 0
  local last=0 now stalled=0 rate pct sleeper=
  if [ -f "$f" ]; then last=$(stat -c %s "$f"); fi

  # A bare `sleep "$every"` in the loop below would OUTLIVE the kill that stops
  # this watcher: the signal reaches this subshell, not its child. The orphaned
  # sleep goes on holding the job's stderr open, so every layer would end with
  # a dead pause of up to one poll interval before anything downstream saw the
  # output. Run it as a job this watcher can take down with itself.
  trap 'kill "$sleeper" 2>/dev/null; exit 0' TERM

  while :; do
    sleep "$every" & sleeper=$!
    wait "$sleeper" 2>/dev/null || true
    # Never outlive the pull. Without this a watcher survives a dcache__die and
    # keeps printing into a job that has already failed.
    kill -0 "$parent" 2>/dev/null || return 0
    now=0
    if [ -f "$f" ]; then now=$(stat -c %s "$f"); fi
    pct=$(( size > 0 ? now * 100 / size : 0 ))
    if [ "$now" -le "$last" ]; then
      stalled=$(( stalled + every ))
      echo "derive-cache: $label STALLED at $now/$size bytes (${pct}%) —" \
           "no new bytes in ${stalled}s" >&2
    else
      rate=$(( (now - last) / every / 1024 ))
      stalled=0
      echo "derive-cache: $label $now/$size bytes (${pct}%, ${rate} KB/s)" >&2
    fi
    last=$now
  done
}

# dcache__fetch_blobs REF DEST MANIFEST
#
# The resumable replacement for `oras pull`. Fetches each layer separately and
# skips any that is already on disk and verifies, so an interrupted transfer
# keeps everything it finished — within this run and across runs.
#
# WHY THIS EXISTS. `oras pull` is one transfer: interrupt it at 99% and it
# starts over. That is survivable on a flaky link and fatal on a hostile one.
# Runs 31460767854 and 31464177083 failed all six of their attempts against
# wikipedia's 95 GB entry with
#
#   copy failed: stream error: stream ID N; PROTOCOL_ERROR; received from peer
#
# The interruptions arrive on a regular period, and the cause is local to the
# CI environment rather than a limit in oras, in GHCR or on the path: the same
# entry pulls clean from elsewhere in 1156 s, and --concurrency 1 changed
# nothing. Attempt 1 of the second run transferred 19 GB correctly and
# then threw all of it away, which is the actual defect — with interruptions
# arriving every 10-15 minutes and an uninterrupted pull needing ~19, whole-
# transfer retries can fail forever while making real progress every time.
#
# BLOB GRANULARITY WAS NOT ENOUGH. That was the first version's stated floor,
# and run 31497034118 hit it twice in one run: vwa/classifieds (9 layers of
# 8.59 GB) and webarena/gitlab (8.59, 8.59, 2.78) each burned all eight passes
# on part-00 and never completed a single layer. At the throughput available,
# an 8.59 GB layer took about as long as the gap between interruptions. Two
# jobs, ~2 hours, zero bytes of progress, because a pass can only skip what a
# previous pass FINISHED. Resumption coarser than the interruption period does
# not converge; it just fails more slowly.
#
# So the unit is now the byte, not the blob (dcache__fetch_blob). Progress is
# monotone regardless of how blob size compares to the interruption period: an
# interruption costs the bytes in flight and nothing else. Both problems had
# to be present to deadlock — on a faster link the same layer finishes well
# inside any window — which is exactly why the fix must not depend on either
# one.
dcache__fetch_blobs() {
  local ref=$1 dest=$2 mf=$3
  # A tagged ref's repository is everything before the LAST colon; blobs are
  # addressed by digest against the repository, not the tag. Safe for
  # `localhost:5000/x:t` because the port's colon is not the last one.
  local repo=${ref%:*}
  local passes=${DCACHE_PULL_TRIES:-8}
  local layers pass digest size title f want failed have watcher rc

  layers=$(printf '%s' "$mf" | dcache__layers) || return 1
  [ -n "$layers" ] || dcache__die "$ref carries no file layers — nothing to pull." \
    "An artifact with no titled layer would extract to an empty directory and" \
    "report a hit, which is the false-hit bug in a different costume."

  for (( pass=1; pass<=passes; pass++ )); do
    # A here-string runs the loop in THIS shell, not a subshell, so `failed`
    # survives it. With a pipe it would not, and every pass would look clean.
    failed=0
    while IFS=$'\t' read -r digest size title; do
      f="$dest/$title"
      want=${digest#sha256:}
      # Size first: it is free, and a transfer killed mid-write leaves a short
      # file, so the common case is rejected without hashing 9.6 GB. Then the
      # digest — this is the ONLY check standing between a half-written file
      # left behind by a previous run and a benchmark image built on corrupt
      # data. Nothing else verifies what a previous run left on disk, and with
      # byte-range resume that partial file is now something we deliberately
      # build on rather than something we merely tolerate.
      if [ -f "$f" ] && [ "$(stat -c %s "$f")" = "$size" ]; then
        # Announced because it is SLOW and silent: hashing wikipedia's twelve
        # layers is 95 GB of sha256 per pass, minutes in which the job looks
        # every bit as hung as a blackholed socket does.
        echo "derive-cache: verifying $title ($size bytes) against its digest" >&2
        if [ "$(sha256sum <"$f" | cut -d' ' -f1)" = "$want" ]; then
          echo "derive-cache: $title verified, already complete" >&2
          continue
        fi
      fi
      # Announced HERE, not inside dcache__fetch_blob, because dcache__transfer
      # captures its command's output and prints it only on failure — so a
      # message about work that SUCCEEDS would never be seen. This line is how
      # an operator watching a 95 GB pull can tell resumption is working,
      # rather than inferring it from the absence of a complaint.
      have=0
      if [ -f "$f" ]; then have=$(stat -c %s "$f"); fi
      if [ "$have" -gt 0 ] && [ "$have" -lt "$size" ]; then
        echo "derive-cache: resuming $title at $have/$size bytes" >&2
      else
        echo "derive-cache: fetching $title ($size bytes)" >&2
      fi

      # Started here, around the transfer, for the same reason the line above
      # is here: everything dcache__fetch_blob writes is captured.
      dcache__watch "$f" "$size" "$title" "$$" & watcher=$!
      rc=0
      dcache__transfer "fetch of $title" 1 \
        dcache__fetch_blob "$repo" "$digest" "$size" "$f" || rc=$?
      # `|| true` on both: the watcher is killed by design, so its non-zero
      # status is the expected outcome, and this runs under set -e.
      kill "$watcher" 2>/dev/null || true
      wait "$watcher" 2>/dev/null || true

      if [ "$rc" != 0 ]; then
        # Stop the pass rather than grinding through the rest: whatever is
        # killing transfers is usually still doing it a second later, and the
        # sleep before the next pass is the point.
        failed=1
        break
      fi
      echo "derive-cache: $title complete ($size bytes)" >&2
    done <<< "$layers"

    # Nothing failed means every layer either verified or was just fetched.
    [ "$failed" = 0 ] && return 0

    if [ "$pass" != "$passes" ]; then
      echo "derive-cache: pass $pass/$passes of $ref was interrupted — retrying;" \
           "completed layers are skipped and a partial one resumes where it stopped" >&2
      sleep "${DCACHE_RETRY_SLEEP:-30}"
    fi
  done
  return 1
}

# Resolved relative to this file's own location, not the caller's cwd, so
# dcache_require works from every derive script regardless of where it runs
# from. builder/stage-lib/derive-cache.sh -> ../derived-cache.lock.
_DCACHE_LOCK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/derived-cache.lock"

# dcache_ensure_oras — puts a pinned `oras` on PATH. Idempotent: a no-op if
# `oras` already resolves (a pre-provisioned runner) or if this already ran in
# this shell. Fails loudly (exits) rather than falling back to an unpinned
# `oras`, because an unpinned tool is exactly the supply-chain risk pinning
# exists to remove.
dcache_ensure_oras() {
  [ "${DCACHE_SKIP_ORAS_INSTALL:-}" = 1 ] && return 0
  command -v oras >/dev/null 2>&1 && return 0

  local dir="${DCACHE_TOOL_DIR:-$HOME/.cache/derive-cache}"
  if [ -x "$dir/oras" ]; then
    PATH="$dir:$PATH"
    export PATH
    return 0
  fi

  mkdir -p "$dir"
  local tgz="$dir/oras_${_DCACHE_ORAS_VERSION}_linux_amd64.tar.gz"
  curl -fsSL -o "$tgz" "$_DCACHE_ORAS_URL" \
    || dcache__die "could not download $_DCACHE_ORAS_URL"
  # -c reads "<hex>  <path>" lines; the checksums file's own file names don't
  # match our download path, so build the line ourselves rather than editing
  # the release's own artifact.
  echo "$_DCACHE_ORAS_SHA256  $tgz" | sha256sum -c - >/dev/null \
    || dcache__die "oras download failed checksum verification — refusing to use it"
  tar -xzf "$tgz" -C "$dir" oras
  chmod +x "$dir/oras"
  rm -f "$tgz"
  PATH="$dir:$PATH"
  export PATH
}

# dcache_pull REF DEST_DIR
#
# Returns 0 on a hit with files written into DEST_DIR, 1 on a miss. Sets
# DCACHE_HIT_FORMAT to "oras" on a hit.
dcache_pull() {
  local ref=$1 dest=$2
  mkdir -p "$dest"
  dcache_ensure_oras
  DCACHE_HIT_FORMAT=

  # Exit code alone is NOT a hit signal — measured live (2026-08-08) against a
  # tag that was still the legacy `FROM scratch` Docker image: `oras pull`
  # exits 0 and writes NOTHING, because oras only materializes a layer as a
  # file when it carries an `org.opencontainers.image.title` annotation, and
  # `docker build`'s COPY layers never set one. Trusting the exit code alone
  # would report a false hit with nothing written — worse than a miss, because
  # nothing downstream would know it had no inputs.
  #
  # So ask the MANIFEST what the tag holds, rather than reading oras's log
  # prose or checking whether DEST_DIR gained files. Both alternatives are
  # unsound here: the log line is not a stable interface, and DEST_DIR is the
  # shared datasets directory, which is already full of other files. An
  # artifact this library pushed carries `artifactType`; a Docker image, of
  # either flavour, does not.
  # Keep the manifest fetch's stderr. It is now the ONLY evidence a miss has:
  # discarding it is how run 31268159790 became unexplainable (see below), and
  # `denied`, `unauthorized`, a 503 and a genuinely absent tag all reach the
  # miss below looking identical without it.
  local mf mferr
  mferr=$(mktemp)
  mf=$(oras manifest fetch "$ref" 2>"$mferr" || true)

  # PRESENCE AND TRANSFER ARE DIFFERENT QUESTIONS, and conflating them is what
  # cost this fleet two long re-downloads. Run 31284811602's wikipedia job:
  #
  #   failed to copy: read tcp ...->185.199.111.154:443: read: connection reset by peer
  #
  # GHCR reset the connection ~4.5 min into an 88 GB pull. The entry was
  # perfectly intact; the transfer broke. With no retry that reported as "no
  # cache entry", which pre-fix meant falling through to the upstream mirrors —
  # ~24 h for wikipedia at the measured 1.05 MB/s, ~6 h for shopping. Bigger
  # entries take longer and so are MORE likely to be reset: the failure grows
  # with exactly the cost it inflicts.
  #
  # The manifest above already answers "does this exist?", so a transfer that
  # fails on an entry we KNOW is there is transient by construction and gets
  # retried. An entry with no manifest is genuinely absent — a first build of a
  # new image — and must miss immediately rather than burning three attempts.
  # How that retry happens is dcache__fetch_blobs's problem, and the reason it
  # is not simply `oras pull` with a loop around it is written out there: a
  # whole-transfer retry discards every byte it had finished, which against
  # this runner's periodic connection teardown never converges.
  if printf '%s' "$mf" | grep -q '"artifactType"'; then
    rm -f "$mferr"
    if dcache__fetch_blobs "$ref" "$dest" "$mf"; then
      DCACHE_HIT_FORMAT=oras
      return 0
    fi
    # The manifest says this IS our artifact, so a failed pull is a real
    # error, not a reason to go looking for a legacy image that is not there.
    dcache__die "pull of $ref failed after its manifest identified it as an artifact"
  fi

  # A manifest that is NOT an artifact is a hard failure, not a miss. Returning
  # 1 here would send the caller off to re-derive — for wikipedia a ~24 h,
  # ~95 GB upstream fetch — over what is really "this tag holds a format this
  # library stopped reading". Nothing on the fleet should ever reach this: it
  # means the legacy image came back, or a tag collided with something that is
  # not ours. Say so, and name the way out.
  if [ -n "$mf" ]; then
    rm -f "$mferr"
    dcache__die "$ref exists but is not an oras artifact (no artifactType)." \
      "This library no longer reads the legacy \`FROM scratch\` image format." \
      "Delete the tag and re-derive, or bump the RECIPE so a fresh entry is" \
      "pushed under a new tag."
  fi

  # Report the failure instead of discarding it. Run 31268159790 is the reason:
  # webarena/shopping missed this cache and spent two hours re-downloading 41 GB
  # from metis, and the log could not say WHY it missed, because the idiom of
  # the day was `docker pull "$CACHE" 2>/dev/null`. The entry was verified
  # intact afterwards — every blob HTTP 200 — so the cause was runner-side and
  # is now unknowable. A miss that cannot explain itself is the defect; out of
  # disk, denied and 503 must not all look like "not cached".
  echo "derive-cache: no cache entry for $ref — no manifest at that tag." >&2
  echo "  oras manifest fetch: $(cat "$mferr")" >&2
  rm -f "$mferr"
  return 1
}

# dcache_push REF DIR FILE...
#
# Pushes FILE... (paths relative to DIR) as an oras artifact at REF. Retries
# transient failures 3x, then FAILS THE BUILD — same contract as #80's fix to
# the legacy `docker push`: this script is the only run that will ever push
# (builder/docker.py's prepare_reuse_check skips a later run whose inputs
# still match), so a swallowed failure leaves the cache empty for good while
# every later build believes it is populated.
dcache_push() {
  local ref=$1 dir=$2
  shift 2
  dcache_ensure_oras

  local attempt out
  for attempt in 1 2 3; do
    if out=$(cd "$dir" && oras push "$ref" "$@" 2>&1); then
      printf '%s\n' "$out"
      DCACHE_PUSHED_DIGEST=$(printf '%s\n' "$out" \
        | grep -oE 'sha256:[0-9a-f]{64}' | tail -1)
      # #42: the digest lock is only as good as the digests actually in it.
      # Print the exact line to add rather than making whoever pins it retype
      # (or worse, copy from somewhere that isn't a live response).
      echo "derive-cache: to pin this entry, add to builder/derived-cache.lock:"
      echo "$ref $DCACHE_PUSHED_DIGEST"
      return 0
    fi
    echo "cache push attempt $attempt/3 failed:" >&2
    printf '%s\n' "$out" >&2
    [ "$attempt" = 3 ] || sleep "${DCACHE_RETRY_SLEEP:-30}"
  done
  dcache__die "could not publish $ref after 3 attempts. Failing rather than" \
    "letting prepare_reuse_check stamp this run as done — nothing would ever" \
    "retry the push and the cache would stay empty for good."
}

# dcache_require REF — called right after a `dcache_pull` miss to decide
# whether the miss is fatal. `dcache_pull`'s exit 1 alone can't tell a
# brand-new image (nothing to pin yet) apart from a broken/emptied cache
# entry this fleet has depended on before; builder/derived-cache.lock is that
# distinction, checked in so it can only change via a reviewed diff.
#
#   - ref present in the lock  -> fatal (exit 1), unless
#     ALLOW_DERIVE_CACHE_MISS=1 downgrades it to a warning on stderr.
#   - ref absent from the lock -> returns 0; the caller derives, exactly as
#     it would for a genuinely new image or a deliberate RECIPE bump.
dcache_require() {
  local ref=$1
  [ -f "$_DCACHE_LOCK" ] || return 0

  local line
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    local lref=${line%% *}
    if [ "$lref" = "$ref" ]; then
      if [ "${ALLOW_DERIVE_CACHE_MISS:-}" = 1 ]; then
        echo "derive-cache: WARNING: $ref is pinned in builder/derived-cache.lock" \
             "but missed, and ALLOW_DERIVE_CACHE_MISS=1 downgrades this to a" \
             "warning — deriving from scratch instead." >&2
        return 0
      fi
      dcache__die "$ref is pinned in builder/derived-cache.lock but missed on" \
        "pull. This entry has served builds before, so a miss now means the" \
        "cache lost it, not that this is a new image — failing rather than" \
        "silently re-deriving (and re-paying) it. If this is a deliberate" \
        "GHCR outage, re-run with ALLOW_DERIVE_CACHE_MISS=1."
    fi
  done < "$_DCACHE_LOCK"
  return 0
}
