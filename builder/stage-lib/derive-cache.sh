# shellcheck shell=bash
#
# Shared derived-inputs cache protocol for this fleet's seven derive scripts.
# Sourced, not executed — every function here must behave correctly whether
# the caller runs under `set -euo pipefail` (every derive-backup.sh does) or
# a looser mode, so a failure that should be fatal is a bare statement or an
# explicit `exit`, and a failure that must NOT be fatal (the opportunistic
# migration push below) is always wrapped so it cannot trip the caller's -e.
#
# THE TWO FORMATS. Every one of the seven sites has, today, a `FROM scratch`
# Docker image whose only content is the derived files (`docker create` +
# `docker export | tar` reads it back — see #79 on why that extract must be
# filtered). This library adds a second, preferred format: an oras artifact
# at the SAME tag. A read tries oras first and falls back to the legacy image;
# a legacy hit re-pushes the bytes it just extracted as oras, non-fatally, so
# the format migrates on the next ordinary rebuild instead of a separate
# multi-tens-of-GB transfer job. Writes always go out as oras.
#
# Usage, from a derive script (after `set -euo pipefail`):
#
#   . "$REPO_ROOT/builder/stage-lib/derive-cache.sh"
#   if dcache_pull "$CACHE" "$DATASETS_DIR" '*gitlab*'; then
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

# Opportunistic: the bytes for $2 are already on disk (a legacy hit just put
# them there), so a failed migration push costs nothing but the format staying
# legacy one more rebuild — the NEXT hit tries again. That is why this is a
# single attempt with no retry/backoff (unlike dcache_push): #80's retry-then-
# fail contract exists because a failed WRITE is the only copy of a finished
# derivation; a failed migration risks nothing but trying again later, and
# never allowed to fail the caller's build.
dcache__migrate_legacy_hit() {
  local ref=$1 dest=$2
  ( cd "$dest" && oras push "$ref" $(find . -mindepth 1 -type f -printf '%P\n') ) \
    >/dev/null 2>&1 \
    || echo "derive-cache: opportunistic migration of $ref to oras failed; still legacy" >&2
}

# dcache_pull REF DEST_DIR FILTER
#
# Returns 0 on a hit with files written into DEST_DIR, 1 on a miss. Sets
# DCACHE_HIT_FORMAT to "oras" or "legacy". FILTER is REQUIRED even though only
# the legacy path uses it — #79: an unfiltered `docker export` drops Docker's
# injected /dev, /etc, /proc, /sys and /.dockerenv into the shared datasets
# dir, so there is no safe default and no caller may opt out.
dcache_pull() {
  local ref=$1 dest=$2 filter=$3
  mkdir -p "$dest"
  dcache_ensure_oras
  DCACHE_HIT_FORMAT=

  # Exit code alone is NOT a hit signal — measured live (2026-08-08) against a
  # tag that is still the legacy `FROM scratch` Docker image: `oras pull`
  # exits 0 and writes NOTHING, because oras only materializes a layer as a
  # file when it carries an `org.opencontainers.image.title` annotation, and
  # `docker build`'s COPY layers never set one. Trusting the exit code alone
  # would report a false hit with nothing written — worse than a miss, because
  # nothing downstream would know to fall back.
  #
  # Ask the MANIFEST which format the tag holds, rather than reading oras's
  # log prose or checking whether DEST_DIR gained files. Both alternatives are
  # unsound here: the log line is not a stable interface, and DEST_DIR is the
  # shared datasets directory, which is already full of other files. Measured
  # on the same pair: an artifact this library pushed carries `artifactType`,
  # the legacy image has none.
  local mf out
  mf=$(oras manifest fetch "$ref" 2>/dev/null || true)
  if printf '%s' "$mf" | grep -q '"artifactType"'; then
    if out=$(oras pull "$ref" -o "$dest" 2>&1); then
      DCACHE_HIT_FORMAT=oras
      return 0
    fi
    # The manifest says this IS our artifact, so a failed pull is a real
    # error, not a reason to go looking for a legacy image that is not there.
    printf '%s\n' "$out" >&2
    dcache__die "oras pull of $ref failed after its manifest identified it as an artifact"
  fi

  # Capture the failure instead of discarding it. Run 31268159790 is the reason:
  # webarena/shopping missed this cache and spent two hours re-downloading 41 GB
  # from metis, and the log could not say WHY it missed, because the idiom this
  # replaces was `docker pull "$CACHE" 2>/dev/null`. The entry was verified
  # intact afterwards — every blob HTTP 200 — so the cause was runner-side and
  # is now unknowable. A miss that cannot explain itself is the defect; out of
  # disk, denied and 503 must not all look like "not cached".
  local dout
  if dout=$(docker pull "$ref" 2>&1); then
    DCACHE_HIT_FORMAT=legacy
    local cid
    cid=$(docker create "$ref" true)
    # Same idiom as every derive-backup.sh today, unchanged: ONE wildcard
    # pattern, because GNU tar exits 2 on any pattern that matches nothing, so
    # a second shape would break every extract that legitimately lacks it.
    docker export "$cid" | tar -x -C "$dest" --wildcards "$filter"
    docker rm "$cid" >/dev/null
    docker rmi "$ref" >/dev/null 2>&1 || true
    dcache__migrate_legacy_hit "$ref" "$dest"
    return 0
  fi

  echo "derive-cache: no cache entry for $ref — neither format could be read." >&2
  echo "  oras manifest fetch: ${mf:-<no manifest returned>}" >&2
  echo "  docker pull: $dout" >&2
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
