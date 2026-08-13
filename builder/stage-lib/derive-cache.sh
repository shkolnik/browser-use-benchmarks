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
#   DCACHE_RETRY_SLEEP       base seconds between retries (default 30) — the
#                             first wait, doubled per fruitless pull pass —
#                             overridable so a test does not sleep 90s
#   DCACHE_RETRY_MAX_SLEEP   ceiling for that doubling, and for a Retry-After
#                             the registry asks for (default 300)
#   DCACHE_PULL_TRIES        pull passes before a miss is fatal (default 10)
#   DCACHE_RETRY_JITTER      percent of random spread applied to each wait
#                             (default 25; 0 makes waits exact, for tests)
#   DCACHE_MIN_BPS           bytes/sec below which a transfer counts as stalled
#                             (default 102400 = 100 KB/s)
#   DCACHE_STALL_SECS        how long it must stay below that before curl gives
#                             up and the pass resumes it (default 60)
#   DCACHE_PROGRESS_SECS     seconds between progress lines (default 30; 0
#                             disables the watcher)
#   DCACHE_LOCK              path to the digest lock (default the checked-in
#                             builder/derived-cache.lock) — an override so a
#                             test can pin a fake ref without editing the
#                             fleet's real lock

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

# dcache__fetch_blobs REF DEST MANIFEST
#
# The resumable replacement for `oras pull`: fetches each layer separately,
# skips any that is already on disk and verifies, and resumes a partial one at
# the byte it stopped on, so an interrupted transfer keeps everything it
# finished — within this run and across runs.
#
# The engine itself is builder/derive_cache_fetch.py, and its header says why
# it is not `oras pull` and why the unit of progress is the byte rather than
# the blob. It is Python because the job outgrew what shell holds safely: HTTP
# range resumption, redirects that must drop credentials, token acquisition, a
# progress reporter that used to be a subprocess reaped by PID, digest
# verification and a backoff state machine. The last of those is the clearest
# case — a rate limit is a fact about WHY a fetch failed, and the shell version
# could not carry it out of the transfer at all, because the fetch ran in a
# command substitution: the registry's Retry-After had to be printed into the
# log and regex'd back out by the retry loop that needed it.
#
# python3 is not a new dependency: this library already shelled out to it for
# the manifest parser, the Docker credential reader and the token parser, and
# nothing on the fleet builds at all without builder/cli.py.
dcache__fetch_blobs() {
  local ref=$1 dest=$2 mf=$3
  # Resolved from this file's own location so it is found from every derive
  # script regardless of cwd: builder/stage-lib/ -> the repo root, two up.
  local root; root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
  # The manifest goes over stdin rather than argv: it is a few KB of JSON that
  # has already been through one shell variable, and putting registry-supplied
  # bytes on a command line is how quoting bugs become injection bugs.
  printf '%s' "$mf" | PYTHONPATH="$root" python3 -m builder.derive_cache_fetch "$ref" "$dest"
}

# Resolved relative to this file's own location, not the caller's cwd, so the
# lock is found from every derive script regardless of where it runs from.
# builder/stage-lib/derive-cache.sh -> ../derived-cache.lock.
_DCACHE_LOCK="${DCACHE_LOCK:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/derived-cache.lock}"

# dcache__lock_digest REF — prints the digest pinned for REF and returns 0 if
# REF appears in the lock at all, 1 if it does not. A pinned ref with no digest
# column prints nothing and still returns 0: "pinned" and "pinned to a specific
# digest" are different questions, and the two callers ask different ones —
# dcache_require only needs to know the ref was depended on before, while
# dcache__check_pin needs something to compare against.
dcache__lock_digest() {
  local ref=$1 line lref ldigest
  [ -f "$_DCACHE_LOCK" ] || return 1
  while IFS= read -r line; do
    line=${line%%#*}
    read -r lref ldigest _ <<<"$line" || true
    [ -n "${lref:-}" ] || continue
    if [ "$lref" = "$ref" ]; then
      printf '%s\n' "${ldigest:-}"
      return 0
    fi
  done < "$_DCACHE_LOCK"
  return 1
}

# dcache__check_pin REF MANIFEST_DIGEST — the other half of #42, on the HIT
# path. dcache_require can only ever police a MISS; this is what makes the
# lock's digest column a comparison rather than a record of what was true once.
#
# WHAT A MATCH BUYS. The manifest digest is the root of a hash chain that
# already covers everything else: the manifest names each layer by sha256 and
# by size, and dcache__fetch_blobs refuses any blob whose bytes do not hash to
# what the manifest said. So "this manifest is the one in the lock" extends to
# "every byte extracted into the datasets directory is the reviewed byte" —
# without it, the chain is intact but anchored to whatever the tag happens to
# point at today, and a tag is mutable. Checked BEFORE the blobs are fetched:
# an entry we are about to reject should not first cost 95 GB of transfer.
#
# A MISMATCH IS FATAL, and deliberately has no workflow_dispatch escape hatch
# next to allow_derive_cache_miss. That input means "GHCR is unwell, derive
# instead"; this means "the tag we pinned now resolves to different content",
# which is either a legitimate re-push — fixed by editing the lock, which is a
# reviewed diff, which is the entire point of checking the lock in — or
# something nobody intended. Clicking past it would use unreviewed inputs.
# ALLOW_DERIVE_CACHE_DIGEST_MISMATCH=1 exists for the person re-pinning by
# hand and is not exposed to CI.
dcache__check_pin() {
  local ref=$1 got=$2 want
  if ! want=$(dcache__lock_digest "$ref"); then
    # Not pinned: a new image or a deliberate RECIPE bump, exactly as on the
    # miss path. Print the line to add rather than making whoever pins it
    # retype a 64-hex digest — same courtesy dcache_push extends after a push.
    echo "derive-cache: $ref is not pinned. To pin it, add to" \
         "builder/derived-cache.lock:" >&2
    echo "  $ref $got" >&2
    return 0
  fi
  [ -z "$want" ] && return 0
  [ "$want" = "$got" ] && return 0

  if [ "${ALLOW_DERIVE_CACHE_DIGEST_MISMATCH:-}" = 1 ]; then
    echo "derive-cache: WARNING: $ref resolves to $got but is pinned to $want" \
         "in builder/derived-cache.lock, and" \
         "ALLOW_DERIVE_CACHE_DIGEST_MISMATCH=1 downgrades this to a warning —" \
         "using the entry the registry served." >&2
    return 0
  fi
  # Both digests on their own lines: whoever reads this is about to compare
  # 64 hex characters, and a wrapped one-liner is where that comparison goes
  # wrong.
  echo "derive-cache: $ref resolves to a manifest this fleet has not pinned:" >&2
  echo "  pinned:   $want" >&2
  echo "  resolved: $got" >&2
  dcache__die "The tag moved. Either the entry was re-pushed — in which case update" \
    "builder/derived-cache.lock to $got in a reviewed diff — or it points at" \
    "content nobody here published. Refusing to build on it either way."
}

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
  local mf mferr mfile
  mferr=$(mktemp)
  mfile=$(mktemp)
  # Written to a FILE rather than captured, because the manifest is about to be
  # hashed and an entry's digest is the sha256 of the manifest's raw bytes.
  # `$(...)` strips trailing newlines, so hashing what command substitution
  # returns would compute the digest of bytes the registry never sent — a check
  # that fails on entries that are perfectly fine. $mfile is what gets hashed;
  # $mf is only ever parsed, where a missing trailing newline cannot matter.
  oras manifest fetch -o "$mfile" "$ref" 2>"$mferr" || true
  mf=$(cat "$mfile")

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
    # Computed here rather than read from `oras manifest fetch --descriptor`:
    # the descriptor is the registry's claim about the bytes, and this is the
    # one place where checking the bytes themselves costs nothing (they are
    # already on disk, and a manifest is a few KB).
    dcache__check_pin "$ref" "sha256:$(sha256sum <"$mfile" | cut -d' ' -f1)"
    rm -f "$mferr" "$mfile"
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
    rm -f "$mferr" "$mfile"
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
  rm -f "$mferr" "$mfile"
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
  # Only presence matters here — a miss has no digest to compare, which is why
  # the digest half of the lock is enforced in dcache_pull instead.
  dcache__lock_digest "$ref" >/dev/null || return 0

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
}
