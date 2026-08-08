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
# NO GHCR derived cache here, unlike every other prepare script in this fleet.
# Those scripts derive small artifacts from a huge tar, so caching turns a ~6 h
# metis fetch into a fast pull. Here the outputs are a byte-for-byte partition
# of the input: caching them would trade an 88.7 GiB download for an 88.7 GiB
# download while permanently doubling this benchmark's GHCR footprint. On a
# miss we re-fetch from archive.org, which is fast and range-resumable.
#
# Inputs (env, set by builder/docker.py's run_prepare):
#   DATASETS_DIR          where the verified upstream ZIM lives and outputs must land
#   REPO_ROOT, IMAGE      to fetch the lazy prepare_input dataset
#   PREPARE_INPUT_FILE    the pinned ZIM's filename, from the manifest
set -euo pipefail

: "${DATASETS_DIR:?run_prepare must export DATASETS_DIR}"
: "${REPO_ROOT:?run_prepare must export REPO_ROOT}"
: "${IMAGE:?run_prepare must export IMAGE}"
# The filename comes from the manifest via run_prepare, never a second copy
# here — same single-source-of-truth rule as the pinned sha256 elsewhere.
: "${PREPARE_INPUT_FILE:?run_prepare must export PREPARE_INPUT_FILE}"

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
# the sizes are already known from the boundaries, so a layout that cannot be
# published should say so now rather than after the split.
echo "=== the planned layout ==="
python3 - "$ZIM" "$BOUNDS_FILE" "$MAX_PART_BYTES" <<'PY'
import os, sys
total = os.path.getsize(sys.argv[1])
cuts = [0] + [int(x) for x in open(sys.argv[2]) if x.strip()] + [total]
max_part = int(sys.argv[3])
over = []
for i, (s, e) in enumerate(zip(cuts, cuts[1:])):
    flag = ""
    if e - s > max_part:
        over.append((i, e - s))
        flag = "  ** OVER THE LAYER CEILING **"
    print(f"  part {i:2d}  {s:>14,} .. {e:>14,}  {(e-s)/2**30:6.2f} GiB{flag}")
if over:
    lines = "\n".join(f"    part {i}: {n/2**30:.2f} GiB" for i, n in over)
    sys.exit(
        "\nsplit-zim: this archive cannot be published as layers:\n"
        f"{lines}\n"
        f"    ceiling: {max_part/2**30:.3f} GiB (docs/registry-limits.md)\n"
        "  A part is a file and a file lives in exactly one layer. A part is only this\n"
        "  big because an Xapian index is, and that index has to stay whole or search\n"
        "  breaks — so this is not something a different part size can fix.\n"
        "  See images/webarena/wikipedia/README.md for the options."
    )
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
python3 - "$ZIM" "$STAGING" "$STEM" "$MAX_PART_BYTES" "$BOUNDS_FILE" <<'PY'
import itertools, os, string, sys

src, out, stem, max_part = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
total = os.path.getsize(src)
cuts = [0] + [int(x) for x in open(sys.argv[5]) if x.strip()] + [total]
sfx = [a + b for a, b in itertools.product(string.ascii_lowercase, repeat=2)]
if len(cuts) - 1 > len(sfx):
    sys.exit(f"split-zim: {len(cuts)-1} parts exceeds the two-letter suffix space")

oversized = []
with open(src, "rb") as f:
    for i, (s, e) in enumerate(zip(cuts, cuts[1:])):
        name = f"{out}/{stem}.zim{sfx[i]}"
        with open(name, "wb") as o:
            left = e - s
            while left:
                b = f.read(min(1 << 24, left))
                o.write(b)
                left -= len(b)
        size = os.path.getsize(name)
        flag = ""
        if size > max_part:
            oversized.append((os.path.basename(name), size))
            flag = "  ** OVER THE LAYER CEILING **"
        print(f"  part {i:2d}  {size/2**30:6.2f} GiB  {os.path.basename(name)}{flag}", flush=True)

got = sum(os.path.getsize(os.path.join(out, x)) for x in os.listdir(out))
if got != total:
    sys.exit(f"split-zim: parts total {got} bytes, source is {total}; the split is incomplete")
print(f"split-zim: {len(cuts)-1} parts, {got} bytes, matching the source exactly")

if oversized:
    lines = "\n".join(f"    {n}: {s/2**30:.2f} GiB" for n, s in oversized)
    sys.exit(
        "split-zim: these parts are larger than any layer this fleet has published:\n"
        f"{lines}\n"
        f"    ceiling: {max_part/2**30:.3f} GiB (docs/registry-limits.md)\n"
        "  A part is a file and a file lives in exactly one layer, so this cannot be\n"
        "  pushed as-is. It is not a bug in the split: an Xapian index bigger than the\n"
        "  ceiling cannot be both whole (which search requires) and inside a layer.\n"
        "  See images/webarena/wikipedia/README.md for the options."
    )
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
parts = sorted(x for x in os.listdir(out) if x.startswith(f"{stem}.zim"))
edges, pos = [], 0
for p in parts:
    n = os.path.getsize(os.path.join(out, p))
    edges.append((p, pos, pos + n))
    pos += n

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

for p in "$STAGING/$STEM.zim"??; do
  mv -f -- "$p" "$DATASETS_DIR/"
done
echo "=== parts in place ==="
ls -l "$DATASETS_DIR/$STEM".zim??
