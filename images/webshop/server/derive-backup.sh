#!/bin/bash
# Prepare step for the webshop image — and the one in the fleet that derives
# nothing.
#
# WebShop's authors published the product data AS DATA (HuggingFace mirrors of
# the upstream Google Drive ids in setup.sh), so unlike gitlab/shopping/reddit/
# classifieds there is no upstream container holding bytes with no other
# source. What this step adds is the other half of what a derive step buys the
# fleet: a copy in the registry we control, so the build can be resumed from
# the cache ALONE — no third-party mirror on the path — and so the runner's
# datasets cache stays reclaimable, deleting the files costing a registry pull
# rather than a re-fetch from HuggingFace.
#
# All three product files are cached, not just the 5.1G one. A checkpoint you
# cannot rebuild forward from is not a checkpoint: two small files left outside
# it would pin the build to HuggingFace exactly as hard as the large one, for
# 3% of the bytes. That is also why builder/manifest.py now accepts more than
# one prepare_input dataset — see prepare_inputs_digest().
#
# ⚠️ NOT cached here: the Lucene index the Dockerfile builds over all ~1.18M
# products. That is webshop's genuinely expensive artifact and the true
# analogue of what the other images derive; caching its INPUTS is not caching
# it. Moving it into this step is a separate backlog item.
#
# Because these artifacts are PINNED — they are upstream files, not derived
# ones — this script does what no other derive script can: re-verify them
# against the manifest after a cache hit. Elsewhere a cache entry is trusted
# because only its own CI run could have created it; here the pins are
# checkable, so a truncated or poisoned entry fails at extraction instead of
# reaching the build.
#
# Inputs (env, set by builder/docker.py run_prepare):
#   DATASETS_DIR         where the datasets live and outputs must land
#   REGISTRY             e.g. ghcr.io/shkolnik
#   PREPARE_INPUTS_DIGEST  identity of the whole pinned input set
#   PREPARE_INPUT_PINS     those pins in `sha256sum -c` format
#   REPO_ROOT/IMAGE      so the miss path can fetch the lazy datasets
set -euo pipefail

# The pins come from image.toml via run_prepare — never a second copy here.
: "${PREPARE_INPUTS_DIGEST:?run_prepare must export the pinned input-set digest}"
: "${PREPARE_INPUT_PINS:?run_prepare must export the pins to verify against}"
# The cache key covers the RECIPE as well as the inputs, exactly as the
# deriving scripts do. Bump RECIPE whenever what this script emits changes; it
# strands the previous revision's entries instead of silently re-serving them.
RECIPE=r1
CACHE="$REGISTRY/webshop-server-derived:${PREPARE_INPUTS_DIGEST:0:12}-$RECIPE"

verify_outputs() {
  if ! printf '%s\n' "$PREPARE_INPUT_PINS" | (cd "$DATASETS_DIR" && sha256sum -c -); then
    echo "derive: cached outputs do not match their pins" >&2
    # Leaving them in place would let the next run reuse them via the size
    # check in prepare_reuse_check, which cannot see content.
    printf '%s\n' "$PREPARE_INPUT_PINS" | awk '{print $2}' \
      | while read -r f; do rm -f "$DATASETS_DIR/$f"; done
    exit 1
  fi
  echo "derive: outputs verified against their pins"
}

extract_outputs_from_cache() {
  local cid
  cid=$(docker create "$CACHE" true)
  # Filtered: `docker export` also carries the /dev, /etc, /proc, /sys and
  # /.dockerenv Docker injects into every container, which unfiltered land
  # in the shared datasets dir. ONE pattern — tar exits 2 on any pattern
  # that matches nothing, so a second shape would break every extract that
  # legitimately lacks it.
  docker export "$cid" | tar -x -C "$DATASETS_DIR" --wildcards 'items_*'
  docker rm "$cid" >/dev/null
}

echo "=== checking derived-inputs cache: $CACHE ==="
if docker pull "$CACHE" 2>/dev/null; then
  extract_outputs_from_cache
  verify_outputs
  echo "derive: cache hit, outputs extracted"
  exit 0
fi
echo "cache miss — fetching from the pinned upstream mirrors"
# Fetch HERE, not in the download step: these are declared prepare_input, so
# `bin/build download` skipped them. On a cold runner whose cache is valid we
# exit above having pulled ~5.3G from GHCR instead of from HuggingFace.
# ensure_dataset verifies each sha256 on this path already.
"$REPO_ROOT/bin/build" download --prepare-inputs "$IMAGE" --datasets-dir "$DATASETS_DIR"
verify_outputs

echo "=== push derived-inputs cache ==="
# No splitting: ~5.3G across three files is under the registry's ~10G layer
# ceiling, unlike the media tars the other scripts have to split.
work=$(mktemp -d "$DATASETS_DIR/.derive-work.XXXXXX")
trap 'rm -rf "$work"' EXIT
{
  echo "FROM scratch"
  printf '%s\n' "$PREPARE_INPUT_PINS" | awk '{print $2}' | while read -r f; do
    # Hardlink rather than copy — same filesystem, and a second 5.1G copy is
    # real time and real disk on a runner that has run out of both.
    ln "$DATASETS_DIR/$f" "$work/$f"
    echo "COPY $f /"
  done
} > "$work/Dockerfile"
docker build -t "$CACHE" "$work"
# Retry, then FAIL. builder/docker.py stamps a successful prepare and skips
# this script on every later run with matching inputs, so this is the only run
# that will ever push: a warning here leaves the cache empty permanently while
# later builds depend on it. Retries because a transient GHCR error must not
# throw away a finished fetch.
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
  echo "derive: could not publish $CACHE after 3 attempts. Failing rather than" \
       "stamping: prepare_reuse_check would skip this script on the next run," \
       "so nothing would ever retry the push and the cache would stay empty" \
       "for good. The artifacts in $DATASETS_DIR are intact and correct." >&2
  exit 1
fi
echo "derive complete"
