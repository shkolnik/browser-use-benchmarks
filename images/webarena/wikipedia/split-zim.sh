#!/bin/bash
# Prepare step for the wikipedia (kiwix ZIM server) image: partition the pinned
# upstream ZIM into layer-sized parts whose boundaries keep its Xapian indexes
# whole.
#
# Why this exists at all: the archive is 88.7 GiB and a registry caps a layer far
# below that, so it cannot be one COPY. Docker layers are per-file, so the parts
# have to be files on disk before `docker build` runs — which is why this is a
# [prepare] step and not a RUN inside the Dockerfile. A RUN that split or
# reassembled the archive in-image would add a second 88.7 GiB to the image
# rather than avoiding the oversized layer.
#
# Why the .zimaa/.zimab/... names: libzim opens a split archive by that
# convention, so kiwix-serve is handed a <stem>.zim path with no file at it and
# reassembles the parts itself. Removing a single part makes the server refuse to
# start ("Unable to add the ZIM file ... to the internal library") rather than
# serve a partial archive, so a lost part cannot silently degrade the benchmark.
#
# WHY THE BOUNDARIES ARE COMPUTED AND NOT JUST SPACED — the expensive lesson:
# libzim hands Xapian a file descriptor plus an offset, which it can only do when
# an index's blob lies entirely inside ONE part. An evenly-spaced split cut this
# archive's fulltext index across two parts, and the result served every article
# byte-identically while search returned 404 "The fulltext search engine is not
# available for this content". The title index fails WORSE: silently, with
# /suggest still answering 200 off an alphabetical prefix scan instead of Xapian
# ranking. Neither is visible in a build log, so the containment check below is
# the gate. See README.md for the measurements.
#
# THIS SCRIPT USED TO SAY there was no GHCR derived cache here, because the
# outputs are a byte-for-byte partition of the input: caching them trades an
# 88.7 GiB download for an 88.7 GiB download while permanently doubling this
# benchmark's GHCR footprint. That reasoning rested on one factual claim — "on a
# miss we re-fetch from archive.org, which is fast" — and the first CI run
# measured it false. Run 31256297478 moved the runner's disk 264G -> 268G in
# 65 minutes: ~1.05 MB/s, about 24 HOURS for this file. Both public mirrors are
# slow to this runner, so the cache is now the only path that makes a cold
# rebuild finish in a working day, and the doubled footprint is the price.
#
# The cache holds the PARTS, not the archive: they are what the build consumes,
# they are already layer-sized, and a cache hit therefore never needs the whole
# 88.7 GiB file on disk at all — it skips the split as well as the download.
#
# Inputs (env, set by builder/docker.py's run_prepare):
#   DATASETS_DIR          where the verified upstream ZIM lives and outputs must land
#   REPO_ROOT, IMAGE      to fetch the lazy prepare_input dataset
#   PREPARE_INPUT_FILE    the pinned ZIM's filename, from the manifest
#   PREPARE_INPUT_SHA256  the pinned ZIM's sha256 — half the cache key
#   REGISTRY              e.g. ghcr.io/shkolnik
set -euo pipefail

: "${DATASETS_DIR:?run_prepare must export DATASETS_DIR}"
: "${REPO_ROOT:?run_prepare must export REPO_ROOT}"
: "${IMAGE:?run_prepare must export IMAGE}"
# The filename comes from the manifest via run_prepare, never a second copy
# here — same single-source-of-truth rule as the pinned sha256 elsewhere.
: "${PREPARE_INPUT_FILE:?run_prepare must export PREPARE_INPUT_FILE}"
: "${PREPARE_INPUT_SHA256:?run_prepare must export PREPARE_INPUT_SHA256}"
: "${REGISTRY:?run_prepare must export REGISTRY}"

# builder/stage-lib/derive-cache.sh: gives the READ side dual-format support
# (an oras artifact preferred, the legacy `FROM scratch` image as fallback).
# The WRITE side below stays on the legacy image on purpose — see the note
# at "push derived-inputs cache" further down.
. "$REPO_ROOT/builder/stage-lib/derive-cache.sh"

# builder/docker.py calls `bin/build download` as a child python process whose
# stdout is a pipe, so it is block-buffered: when the first wikipedia run was
# cancelled mid-fetch, the buffer died with it and the log never said WHICH
# mirror it had been talking to for an hour. That is the one line needed to tell
# a fast mirror from a slow one, so make the child unbuffered. Exported here
# rather than fixed in builder/ deliberately — builder/ is a shared input, and
# editing it rebuilds all eight images to fix a wikipedia log line.
export PYTHONUNBUFFERED=1

HERE="$(cd "$(dirname "$0")" && pwd)"
ZIM="$DATASETS_DIR/$PREPARE_INPUT_FILE"
STEM="${PREPARE_INPUT_FILE%.zim}"
# The target size for an ordinary part. Parts are NOT all this size: a part that
# opens with an Xapian index runs to that index's end instead of being cut. ZIM
# payloads are already compressed, so a layer does not shrink on push — 9 GiB on
# disk is ~9 GiB over the wire.
PART_BYTES=$((9 * 1024 * 1024 * 1024))
# The largest layer this fleet has actually pushed (docs/registry-limits.md's M0
# staircase). GitHub documents 10 GB per compressed layer for GHCR; the probe
# accepted 10.003 GiB. A part over this cannot be published, so fail here rather
# than after a multi-hour build.
MAX_PART_BYTES=$((10 * 1024 * 1024 * 1024 + 3 * 1024 * 1024))
# A part that has to be bigger than the ceiling is not a mistake to be fixed by
# choosing a different boundary: it is one Xapian index, and search needs it
# whole. Such a part ships as `.partNN` sub-files of this size, which the
# entrypoint concatenates back into the part before kiwix-serve starts. The
# archive on disk is unchanged; only its delivery is. See README.md.
SUB_BYTES=$((8 * 1024 * 1024 * 1024))

# Bump RECIPE whenever what this script EMITS changes — different boundaries,
# different sub-file sizes, different names. It strands the previous revision's
# entries instead of silently re-serving parts that no longer match the split
# this script would now produce.
RECIPE=r1
CACHE="$REGISTRY/webarena-wikipedia-derived:${PREPARE_INPUT_SHA256:0:12}-$RECIPE"
# Written into the cache image at push time and checked on pull. `docker export`
# piped into tar under `set -o pipefail` already fails loudly on a truncated
# stream, but that only proves the transfer ended cleanly, not that the parts
# add up to an archive. This is the cheap version of the containment check for
# the path where the source ZIM is never downloaded and so cannot be re-read.
SIZES=.zim-parts.sizes

echo "=== checking derived-inputs cache: $CACHE ==="
# dcache_pull tries the oras artifact first and falls back to the legacy
# `FROM scratch` image; either way the ONE covering wildcard is unchanged —
# `*.zim*` covers parts, sub-files and the sizes manifest, and cannot match
# anything docker's legacy export injects (#79). A legacy hit's local image is
# already reclaimed by the library (holding it costs roughly TWICE its content
# size — measured 183 GB on this runner — and nothing reads it after export:
# the wikipedia Dockerfile COPYs the parts from the datasets build context,
# never from this image).
if dcache_pull "$CACHE" "$DATASETS_DIR" "*.zim*"; then
  echo "split-zim: cache hit ($DCACHE_HIT_FORMAT format)"
  if [ ! -f "$DATASETS_DIR/$SIZES" ]; then
    echo "split-zim: cache entry has no $SIZES manifest — refusing to trust it" >&2
    exit 1
  fi
  # `du`-style two-column compare rather than trusting the tar: a part that
  # arrived short is the failure this catches, and it is invisible to both the
  # builder's existence check and the size check in prepare_reuse_check, which
  # compares against a stamp this run has not written yet.
  if ! (cd "$DATASETS_DIR" && while read -r want name; do
          got=$(stat -c %s "$name" 2>/dev/null || echo missing)
          if [ "$got" != "$want" ]; then
            echo "split-zim: $name is $got bytes, cache manifest says $want" >&2
            exit 1
          fi
        done < "$SIZES"); then
    echo "split-zim: cached parts do not match their manifest — discarding them" >&2
    # Leaving them would let the NEXT run reuse them through prepare_reuse_check,
    # which cannot see content and would stamp them as good.
    rm -f -- "$DATASETS_DIR/$STEM".zim??  "$DATASETS_DIR/$STEM".zim??.part?? \
             "$DATASETS_DIR/$SIZES"
    exit 1
  fi
  rm -f -- "$DATASETS_DIR/$SIZES"
  echo "split-zim: cache hit — parts extracted and verified against their manifest;"
  echo "           the 88.7 GiB upstream fetch and the split are both skipped"
  ls -l "$DATASETS_DIR/$STEM".zim??*
  exit 0
fi
echo "cache miss — fetching from the pinned upstream mirrors"

# The download step skips prepare_input datasets, so fetch it here. This is a
# no-op when a sha256-verified copy is already in DATASETS_DIR.
echo "=== fetching the pinned upstream ZIM (no-op if already verified) ==="
"$REPO_ROOT/bin/build" download --prepare-inputs "$IMAGE" --datasets-dir "$DATASETS_DIR"

SRC_BYTES=$(stat -c %s "$ZIM")

# Fail here, naming the shortfall, rather than part-way through a multi-hour
# split with a truncated last part on disk. The parts are a second full copy of
# the archive: the input is kept, because deleting it would send the next
# re-derive back to the mirror for 88.7 GiB.
AVAIL_BYTES=$(df --output=avail -B1 "$DATASETS_DIR" | tail -1 | tr -d ' ')
# Always logged, not only on failure. This is the largest artifact the fleet
# builds and the runner's disk is its scarce resource, so the headroom at this
# moment is the number to look at first when a wikipedia job dies — and the one
# nobody can reconstruct afterwards.
echo "split-zim: $DATASETS_DIR has $((AVAIL_BYTES / 1073741824)) GiB free;" \
     "the parts need $((SRC_BYTES / 1073741824)) GiB beside the $((SRC_BYTES / 1073741824)) GiB input"
if [ "$AVAIL_BYTES" -lt "$SRC_BYTES" ]; then
  echo "split-zim: need $SRC_BYTES bytes free in $DATASETS_DIR for the parts," \
       "have $AVAIL_BYTES; refusing to start a split that cannot finish" >&2
  exit 1
fi

echo "=== what must stay whole ==="
python3 "$HERE/zim_layout.py" extents "$ZIM"

echo "=== choosing boundaries (target ${PART_BYTES} bytes/part) ==="
# A file, not a pipe: the splitter below reads its own program on stdin, so the
# boundaries have to arrive by path.
BOUNDS_FILE="$(mktemp)"
trap 'rm -f "$BOUNDS_FILE"' EXIT
python3 "$HERE/zim_layout.py" boundaries "$ZIM" "$PART_BYTES" > "$BOUNDS_FILE"

# Check the plan before acting on it. Writing 88.7 GiB takes tens of minutes and
# the sizes are already known from the boundaries, so the layout — including
# which parts have to ship as sub-files — should be legible now rather than
# after the split.
echo "=== the planned layout ==="
python3 - "$ZIM" "$BOUNDS_FILE" "$MAX_PART_BYTES" "$SUB_BYTES" <<'PY'
import os, sys
total = os.path.getsize(sys.argv[1])
cuts = [0] + [int(x) for x in open(sys.argv[2]) if x.strip()] + [total]
max_part, sub = int(sys.argv[3]), int(sys.argv[4])
if sub > max_part:
    sys.exit(f'split-zim: SUB_BYTES {sub} is over the layer ceiling {max_part}')
for i, (s, e) in enumerate(zip(cuts, cuts[1:])):
    n = e - s
    note = ""
    if n > max_part:
        note = f"  -> {-(-n // sub)} sub-files (over the {max_part/2**30:.3f} GiB layer ceiling)"
    print(f"  part {i:2d}  {s:>14,} .. {e:>14,}  {n/2**30:6.2f} GiB{note}")
PY

# Split into a staging dir and move the parts into place only once they verify.
# Writing them straight into DATASETS_DIR would leave a truncated part under a
# correct name if this died mid-write, and the builder's output check only
# tests that each named file exists — a short part would pass it and ship a
# corrupt archive.
STAGING="$DATASETS_DIR/.split-zim-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
trap 'rm -rf "$STAGING"' EXIT

echo "=== splitting $PREPARE_INPUT_FILE ($SRC_BYTES bytes) ==="
python3 - "$ZIM" "$STAGING" "$STEM" "$MAX_PART_BYTES" "$BOUNDS_FILE" "$SUB_BYTES" <<'PY'
import itertools, os, string, sys

src, out, stem, max_part = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sub_bytes = int(sys.argv[6])
total = os.path.getsize(src)
cuts = [0] + [int(x) for x in open(sys.argv[5]) if x.strip()] + [total]
sfx = [a + b for a, b in itertools.product(string.ascii_lowercase, repeat=2)]
if len(cuts) - 1 > len(sfx):
    sys.exit(f"split-zim: {len(cuts)-1} parts exceeds the two-letter suffix space")


def pieces(part_bytes):
    """Byte counts of the files this part ships as — itself, or `.partNN` chunks.

    A chunked part is still ONE part to libzim: the entrypoint concatenates the
    chunks back before kiwix-serve opens the archive. Chunking is a delivery
    detail of the layer ceiling and never moves a boundary, because moving one
    is what cuts an index.
    """
    if part_bytes <= max_part:
        return [part_bytes]
    n = -(-part_bytes // sub_bytes)
    base, extra = divmod(part_bytes, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


with open(src, "rb") as f:
    for i, (s, e) in enumerate(zip(cuts, cuts[1:])):
        chunks = pieces(e - s)
        part = f"{stem}.zim{sfx[i]}"
        for j, want in enumerate(chunks):
            name = part if len(chunks) == 1 else f"{part}.part{j:02d}"
            with open(f"{out}/{name}", "wb") as o:
                left = want
                while left:
                    b = f.read(min(1 << 24, left))
                    if not b:
                        sys.exit(f"split-zim: {src} ended early writing {name}")
                    o.write(b)
                    left -= len(b)
            print(f"  part {i:2d}  {want/2**30:6.2f} GiB  {name}", flush=True)

got = sum(os.path.getsize(os.path.join(out, x)) for x in os.listdir(out))
if got != total:
    sys.exit(f"split-zim: parts total {got} bytes, source is {total}; the split is incomplete")
print(f"split-zim: {len(cuts)-1} parts, {got} bytes, matching the source exactly")
PY

# Containment is the gate the build log cannot show you. Re-derive it from the
# parts that were actually written, not from the boundaries we asked for.
echo "=== verifying every Xapian index landed inside ONE part ==="
python3 - "$ZIM" "$STAGING" "$STEM" "$HERE" <<'PY'
import os, sys

src, out, stem = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, sys.argv[4])
from zim_layout import PROTECTED, Zim  # noqa: E402

z = Zim(src)
# Sub-files sum back into the part they belong to: `.zimaj.part00` and
# `.part01` ARE `.zimaj` as far as libzim ever sees, since the entrypoint
# concatenates them before the archive is opened. Grouping here is what keeps
# this check about containment rather than about delivery.
sizes = {}
for x in sorted(os.listdir(out)):
    if not x.startswith(f"{stem}.zim"):
        continue
    part = x.split(".part")[0]
    sizes[part] = sizes.get(part, 0) + os.path.getsize(os.path.join(out, x))
edges, pos = [], 0
for p in sorted(sizes):
    edges.append((p, pos, pos + sizes[p]))
    pos += sizes[p]

bad = False
for path in PROTECTED:
    ext = z.extent(path)
    if ext is None:
        print(f"  {path}: absent from this archive")
        continue
    s, e = ext
    home = [p for p, ps, pe in edges if ps <= s and e <= pe]
    if home:
        print(f"  {path}: {(e-s)/2**30:.2f} GiB, entirely inside {home[0]}")
    else:
        spans = [p for p, ps, pe in edges if ps < e and s < pe]
        print(f"  {path}: SPANS {len(spans)} parts ({', '.join(spans)}) — search would break")
        bad = True
if bad:
    sys.exit("split-zim: refusing to publish a split that breaks search")
PY

# Also `.zim??.partNN`, hence the glob rather than `.zim??` — a previous run's
# whole part would otherwise be left behind next to this run's sub-files and
# libzim would open the stale one.
rm -f -- "$DATASETS_DIR/$STEM".zim??  "$DATASETS_DIR/$STEM".zim??.part??
for p in "$STAGING/$STEM.zim"??*; do
  mv -f -- "$p" "$DATASETS_DIR/"
done
echo "=== parts in place ==="
ls -l "$DATASETS_DIR/$STEM".zim??*

# Publish them, so no later rebuild ever pays the ~24 h upstream fetch again.
# This runs only on the miss path — a cache hit exited long before here.
#
# DELIBERATELY STILL THE LEGACY `docker build` + `docker push`, not
# dcache_push — this entry is ~88 GB, and re-pushing it as oras opportunistically
# on every future cache hit (the way the library migrates the other six) would
# add that upload to the very next fleet run for no benefit today. The read
# side above already accepts an oras artifact, so this entry converts for free
# the next time it is genuinely re-derived (a RECIPE bump or an upstream ZIM
# change) — it just isn't migrated ahead of that. See the plan's Global
# Constraints.
echo "=== push derived-inputs cache: $CACHE ==="
work=$(mktemp -d "$DATASETS_DIR/.derive-work.XXXXXX")
# Replaces the STAGING trap rather than adding to it: STAGING was already moved
# away piece by piece above, and its directory is removed here too.
trap 'rm -rf "$work" "$STAGING"' EXIT
{
  echo "FROM scratch"
  for p in "$DATASETS_DIR/$STEM.zim"??*; do
    n=$(basename "$p")
    # Hardlink, not copy: same filesystem, and a second 88.7 GiB copy is disk
    # and tens of minutes on a runner that has neither to spare.
    ln "$p" "$work/$n"
    echo "COPY $n /"
    printf '%s %s\n' "$(stat -c %s "$p")" "$n" >> "$work/$SIZES"
  done
  echo "COPY $SIZES /"
} > "$work/Dockerfile"
docker build -t "$CACHE" "$work"
# THIS USED TO BE non-fatal — `docker push || echo warning` — on the reasoning
# that the parts are already on disk, so losing the push only costs the next
# cold rebuild. That reasoning is wrong, and run 31259175714 demonstrated it:
# the cache image was built locally, never reached GHCR, and the tag still
# 404s. The push is not retryable, because builder/docker.py skips this whole
# script once prepare_reuse_check finds the outputs present and the stamp
# matching — so the ONE run that derives is the only run that ever pushes. A
# warning there leaves the cache permanently empty while every later build
# silently depends on one runner's disk keeping the parts alive, which is the
# opposite of what this cache exists to guarantee.
#
# So: retry, then fail. Retries because a transient GHCR error should not throw
# away a finished split; a hard failure because an unpushed cache must not be
# stamped as a success that no later run will revisit.
pushed=
for attempt in 1 2 3; do
  if docker push "$CACHE"; then
    pushed=yes
    break
  fi
  echo "cache push attempt $attempt/3 failed" >&2
  [ "$attempt" = 3 ] || sleep 30
done
if [ -z "$pushed" ]; then
  echo "split-zim: could not publish $CACHE after 3 attempts. Failing rather" \
       "than stamping: prepare_reuse_check would skip this script on the next" \
       "run, so nothing would ever retry the push and the cache would stay" \
       "empty for good. The parts in $DATASETS_DIR are intact and correct." >&2
  exit 1
fi

# Give the disk back BEFORE the image build that follows, not at the end of the
# job where build.yml's cleanup steps run. Run 31259175714 died here, and the
# arithmetic is entirely in copies of one incompressible 95.2 GB file that were
# all live at once: the source ZIM and the parts in DATASETS_DIR (95.2 each),
# this cache image (183 — see the note on the hit path above), and then the
# wikipedia image itself (another 183). That is 556 GB before buildkit's cache
# of a 95.2 GB context and before smoke assembles the archive again.
#
# Both reclaims are unconditional. On a failed push the local copy is no use to
# anyone either: the next run finds this cache by `docker pull`, which only ever
# consults the registry.
echo "=== reclaiming the local copies (the registry has them now) ==="
docker rmi "$CACHE" >/dev/null 2>&1 || true
# The context was 95.2 GB of parts and buildkit cached it. --reserved-space
# matches build.yml's post-job sweep so this cannot evict more than that does.
docker builder prune -f --reserved-space 20GB || true
df -h "$DATASETS_DIR" | tail -n 1
echo "split-zim: complete"
