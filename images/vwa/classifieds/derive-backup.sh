#!/bin/bash
# Prepare step for the vwa/classifieds image: derive the item photos — and only
# the photos — from the upstream image. Every executable byte in the final image
# comes from osclass-v8.1.2.zip plus a committed patch instead (see image.toml
# and README.md).
#
# The photos are the one payload with no other source: they are not in the
# archive.org compose zip (dump + reset script only) and not in the Osclass
# release. Measured, oc-content/uploads is 73 GB across 84,148 per-item
# directories, 336,634 files.
#
# Same mechanism and the same ≤8G splitting as ../../webarena/reddit and
# ../../webarena/shopping.
#
# Inputs (env, set by builder/docker.py run_prepare):
#   DATASETS_DIR  where the verified datasets live and outputs must land
#   REGISTRY      e.g. ghcr.io/shkolnik
#   REPO_ROOT     to source the shared derive-cache library
set -euo pipefail

# builder/stage-lib/derive-cache.sh: read prefers an oras artifact, falls back
# to the legacy `FROM scratch` image, and pushes new entries as oras.
. "$REPO_ROOT/builder/stage-lib/derive-cache.sh"

# Pinned by digest rather than mirrored: the image is 76.86 GB, so hosting a tar
# of it ourselves buys nothing a digest does not already guarantee.
UPSTREAM=jykoh/classifieds@sha256:a2a794da92f62a8d7ffd02314e4fab40ba6c7fc08f568371608f88f0ef605e43
# The cache key covers the RECIPE as well as the input, the lesson from reddit
# r1: this script decides WHAT it extracts, so two recipes over one input are
# two different artifacts. Bumping RECIPE also strands any bad entry from the
# previous revision instead of silently re-serving it.
RECIPE=r1
UPSTREAM_SHA=${UPSTREAM#*@sha256:}
CACHE="$REGISTRY/vwa-classifieds-derived:${UPSTREAM_SHA:0:12}-$RECIPE"

reassemble_outputs() {
  # dcache_pull's filter (#79) already wrote the split parts straight into
  # DATASETS_DIR for both hit formats — this just puts them back together.
  cat "$DATASETS_DIR"/classifieds_uploads.tar.part-* > "$DATASETS_DIR/classifieds_uploads.tar"
  rm -f "$DATASETS_DIR"/classifieds_uploads.tar.part-*
}

echo "=== checking derived-inputs cache: $CACHE ==="
if dcache_pull "$CACHE" "$DATASETS_DIR" 'classifieds_*'; then
  reassemble_outputs
  echo "derive: cache hit ($DCACHE_HIT_FORMAT), outputs extracted"
  exit 0
fi
# #42: distinguish "never cached" (fine, derive) from "was cached,
# now missing" (fatal unless explicitly waived) using the checked-in
# digest lock.
dcache_require "$CACHE"
echo "cache miss — deriving from the upstream image"

echo "=== pull + start upstream image ==="
docker pull "$UPSTREAM"
# No database is needed: the photos are files on disk, so the container only has
# to exist. `php -S` is the image's own CMD and it stays up without a db, which
# is exactly what a `docker cp` needs.
docker run -d --name classifieds-derive "$UPSTREAM" >/dev/null
trap 'docker rm -f classifieds-derive >/dev/null 2>&1 || true' EXIT

echo "=== tar the item photos ==="
# Two steps, not `tar … | cat > f`: in a pipeline the shell reports the LAST
# command's status, so a tar that dies partway still "succeeds". That is exactly
# how reddit's first derivation produced a valid-looking empty artifact, cached
# it, and only failed 35 minutes later.
docker exec classifieds-derive tar cf /tmp/classifieds_uploads.tar \
    -C /usr/src/myapp/oc-content uploads
docker cp classifieds-derive:/tmp/classifieds_uploads.tar "$DATASETS_DIR/"

# Second net, behind the exit code above. A FLOOR well under the measured 73 GB,
# not an equality check — the data may legitimately grow, it may not collapse.
actual=$(stat -c %s "$DATASETS_DIR/classifieds_uploads.tar")
floor=60000000000
if [ "$actual" -lt "$floor" ]; then
  echo "derive: classifieds_uploads.tar is $actual bytes, below the $floor-byte floor — the derivation produced a truncated or empty artifact; refusing to cache it" >&2
  exit 1
fi
echo "derive: classifieds_uploads.tar = $actual bytes (floor $floor)"

docker rm -f classifieds-derive
trap - EXIT
# Free the 76.86 GB upstream image before the build stage needs disk.
docker image rm -f "$UPSTREAM"

echo "=== push derived-inputs cache ==="
# The scratch dir sits NEXT TO the datasets, never in /tmp: a runner's /tmp can
# be far smaller than the volume already holding the datasets, which is how
# ../../webarena/shopping died as `split: No space left on device` mid-derivation
# with its artifacts already verified on disk. Check the room first so a failure
# names the resource instead of surfacing after the copy.
avail=$(df -B1 --output=avail "$DATASETS_DIR" | tail -1)
if [ "$avail" -lt "$((actual + actual / 20))" ]; then
  echo "derive: staging the derived-inputs cache needs ~$((actual >> 30))G beside the datasets in $DATASETS_DIR; only $((avail >> 30))G available" >&2
  exit 1
fi
work=$(mktemp -d "$DATASETS_DIR/.derive-work.XXXXXX")
trap 'rm -rf "$work"' EXIT
# 73 GB in one blob is a bad layer even where a registry tolerates it, and the
# reassembly on the cache-hit path is a `cat`.
split -b 8G -d "$DATASETS_DIR/classifieds_uploads.tar" "$work/classifieds_uploads.tar.part-"
files=()
for f in "$work"/classifieds_uploads.tar.part-*; do
  files+=("$(basename "$f")")
done
# dcache_push retries 3x then fails the build (#80) — see the library.
dcache_push "$CACHE" "$work" "${files[@]}"
echo "derive complete"
