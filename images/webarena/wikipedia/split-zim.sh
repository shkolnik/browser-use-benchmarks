#!/bin/bash
# Prepare step for the wikipedia (kiwix ZIM server) image: partition the pinned
# upstream ZIM into layer-sized parts.
#
# Why this exists at all: the archive is 88.7 GiB and GHCR caps a layer at
# 10 GB, so it cannot be one COPY. Docker layers are per-file, so the parts have
# to be files on disk before `docker build` runs — which is why this is a
# [prepare] step and not a RUN inside the Dockerfile. A RUN that split or
# reassembled the archive in-image would add a second 88.7 GiB to the image
# rather than avoiding the oversized layer.
#
# Why the .zimaa/.zimab/... names: libzim opens a split archive by that
# convention, so kiwix-serve is handed a <stem>.zim path with no file at it and
# reassembles the parts itself. Verified live against kiwix-serve 3.3.0 on
# 2026-08-07: article bytes and full-text search results are byte-identical
# whole-vs-split, and removing a single part makes the server refuse to start
# ("Unable to add the ZIM file ... to the internal library") rather than serve a
# partial archive. That refusal is why a lost part cannot silently degrade into
# a half-populated benchmark.
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

ZIM="$DATASETS_DIR/$PREPARE_INPUT_FILE"
STEM="${PREPARE_INPUT_FILE%.zim}"
# 9 GiB keeps every layer inside GHCR's 10 GB ceiling with real headroom, and
# inside the envelope docs/registry-limits.md already proved (that probe pushed
# 10.003 GiB layers successfully). ZIM payloads are already compressed, so a
# layer does not shrink on push: 9 GiB on disk is ~9 GiB over the wire.
PART_BYTES=$((9 * 1024 * 1024 * 1024))

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

# Split into a staging dir and move the parts into place only once they verify.
# Writing them straight into DATASETS_DIR would leave a truncated part under a
# correct name if this died mid-write, and the builder's output check only
# tests that each named file exists — a short part would pass it and ship a
# corrupt archive.
STAGING="$DATASETS_DIR/.split-zim-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
trap 'rm -rf "$STAGING"' EXIT

echo "=== splitting $PREPARE_INPUT_FILE ($SRC_BYTES bytes) at $PART_BYTES bytes/part ==="
split -b "$PART_BYTES" -a 2 -- "$ZIM" "$STAGING/$STEM.zim"

# Verify the partition covers the source exactly. The builder separately checks
# that every part named in image.toml's prepare.outputs exists; this check is
# the other half — it catches a part that is short, and an EXTRA part beyond the
# manifest's list, which the builder's existence check cannot see and which the
# Dockerfile would silently drop (it COPYs the named parts only, so an
# unlisted 11th part means kiwix-serve opens a truncated archive).
shopt -s nullglob
PARTS=("$STAGING/$STEM.zim"??)
TOTAL=0
for p in "${PARTS[@]}"; do TOTAL=$((TOTAL + $(stat -c %s "$p"))); done
if [ "$TOTAL" -ne "$SRC_BYTES" ]; then
  echo "split-zim: ${#PARTS[@]} parts total $TOTAL bytes, source is $SRC_BYTES;" \
       "the split is incomplete — refusing to publish it" >&2
  exit 1
fi
echo "split-zim: ${#PARTS[@]} parts, $TOTAL bytes, matching the source exactly"

for p in "${PARTS[@]}"; do
  mv -f -- "$p" "$DATASETS_DIR/"
done
echo "=== parts in place ==="
ls -l "$DATASETS_DIR/$STEM".zim??
