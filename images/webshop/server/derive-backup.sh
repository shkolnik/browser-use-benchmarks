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
#   REPO_ROOT/IMAGE      so the miss path can fetch the lazy datasets, and to
#                        source the shared derive-cache library
set -euo pipefail

# builder/stage-lib/derive-cache.sh: read prefers an oras artifact, falls back
# to the legacy `FROM scratch` image, and pushes new entries as oras.
. "$REPO_ROOT/builder/stage-lib/derive-cache.sh"

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

echo "=== checking derived-inputs cache: $CACHE ==="
if dcache_pull "$CACHE" "$DATASETS_DIR" 'items_*'; then
  verify_outputs
  echo "derive: cache hit ($DCACHE_HIT_FORMAT), outputs extracted"
  exit 0
fi
# #42: distinguish "never cached" (fine, derive) from "was cached,
# now missing" (fatal unless explicitly waived) using the checked-in
# digest lock.
dcache_require "$CACHE"
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
files=()
# Process substitution, not a pipe into `while read`: a pipe runs the loop in
# a subshell, so appends to `files` there would vanish the moment the pipeline
# ends and dcache_push below would see an empty list.
while read -r f; do
  # Hardlink rather than copy — same filesystem, and a second 5.1G copy is
  # real time and real disk on a runner that has run out of both.
  ln "$DATASETS_DIR/$f" "$work/$f"
  files+=("$f")
done < <(printf '%s\n' "$PREPARE_INPUT_PINS" | awk '{print $2}')
# dcache_push retries 3x then fails the build (#80) — see the library.
dcache_push "$CACHE" "$work" "${files[@]}"
echo "derive complete"
