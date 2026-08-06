"""Split one big directory tree into N staging buckets, one per image layer.

Three images in this fleet ship a small app tree glued to an enormous media
payload — shopping 45G of pub/media, reddit 38.5G of submission_images,
classifieds 73G of oc-content/uploads. A registry rejects a layer that size, so
the tree is partitioned at build time into buckets under a fixed byte target and
the final stage takes one COPY per bucket.

It lives here, in a build context shared by every image, because the same bug
was fixed in two separate copies of it on one afternoon (the ROOT-nesting trap
below). A third copy was the next thing about to be written.

Usage, from an image's restore stage:
    COPY --from=stagelib partition-tree.py /partition-tree.py
    python3 /partition-tree.py <limit_kb> <max_buckets> <root> <staging_dir>

Leaves <staging_dir>/bucket-NN/ populated and <root> empty. Exits non-zero, with
the offending path named, if the tree cannot be split under those bounds — a
build that would push an oversized layer fails here rather than at push time.
"""

import os
import shutil
import subprocess
import sys


def du_kb(path):
    return int(subprocess.check_output(['du', '-sk', path]).split()[0])


def partition(path, limit_kb):
    """Yield (path, kb) pieces each <= limit_kb, descending into oversized dirs."""
    kb = du_kb(path)
    if kb <= limit_kb:
        yield path, kb
        return
    yield from children(path, kb, limit_kb)


def children(path, kb, limit_kb):
    entries = sorted(os.path.join(path, e) for e in os.listdir(path))
    if not entries:
        raise SystemExit(f'{path} is {kb}K with no children to descend into')
    for entry in entries:
        if os.path.isdir(entry) and not os.path.islink(entry):
            yield from partition(entry, limit_kb)
        else:
            yield entry, du_kb(entry)


def plan(root, limit_kb, max_buckets):
    """Return (assignments, bucket_sizes) — first-fit over the tree's pieces."""
    buckets = []  # [used_kb, index]
    assignments = []
    # children(), never partition(): ROOT must not be yielded as a piece of
    # itself. relpath(ROOT, ROOT) is '.', so the move lands at bucket-NN/. and
    # shutil nests the whole tree as bucket-NN/<basename> — the image then gets
    # /app/app/... and the web root points at nothing. It only bites when the
    # tree fits in ONE bucket, so a shrinking dataset is all it takes; caught by
    # booting a build made with an empty media tar.
    for piece, kb in children(root, du_kb(root), limit_kb):
        if kb > limit_kb:
            raise SystemExit(f'single file {piece} is {kb}K, over the layer limit')
        for b in buckets:
            if b[0] + kb <= limit_kb:
                b[0] += kb
                assignments.append((piece, b[1]))
                break
        else:
            if len(buckets) == max_buckets:
                raise SystemExit(f'tree outgrew {max_buckets} buckets; raise the '
                                 'count here and add COPY lines in the Dockerfile')
            buckets.append([kb, len(buckets)])
            assignments.append((piece, len(buckets) - 1))
    return assignments, [used for used, _ in buckets]


def main(argv):
    limit_kb, max_buckets, root, staging = int(argv[1]), int(argv[2]), argv[3], argv[4]
    assignments, sizes = plan(root, limit_kb, max_buckets)
    for piece, idx in assignments:
        dest = os.path.join(staging, f'bucket-{idx:02d}', os.path.relpath(piece, root))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # Not os.rename: paths from lower overlayfs layers EXDEV on cross-layer
        # directory renames; shutil.move falls back to copy+delete there.
        shutil.move(piece, dest)
    print(f'{len(sizes)} buckets:')
    for idx, used in enumerate(sizes):
        print(f'  bucket-{idx:02d}: {used / 2**20:.1f}G')


if __name__ == '__main__':
    main(sys.argv)
