"""Split an inert media tree into one tar per image layer, on the CI host.

The sibling partition-tree.py does this inside a restore stage, moving files
into bucket-NN/ directories that the final stage then COPYs. That is correct for
trees the build PRODUCES (gitlab, map-nominatim). It is wasteful for trees the
build only THREADS THROUGH: shopping's 45G of pub/media enters and leaves
byte-identical, yet gets walked entry-by-entry into the restore snapshot and
again out of it. Measured on run 31735955132, the bucket COPYs cost 2841s.

This script produces the same partition as tars on the host, with no docker
involved, so the final stage can `ADD` them directly:

    python3 bucket-media.py <limit_kb> <max_buckets> <root> <outdir>

Leaves <outdir>/bucket-NN.tar and <root> UNTOUCHED — unlike partition-tree.py,
which moves. That difference is why the assertions at the bottom exist: moving
is self-evidencing (a leftover file is still in the root), copying is not.

The first-fit planner is imported from partition-tree.py rather than copied. Its
own docstring records that the same bug was fixed in two separate copies of it
on one afternoon, and that the copy which was not shared went on to ship a
fourth bug of its own.
"""

import importlib.util
import os
import stat
import subprocess
import sys

_spec = importlib.util.spec_from_file_location(
    'partition_tree', os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'partition-tree.py'))
partition_tree = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(partition_tree)


def ancestors(rel):
    """'a/b/c' -> ['a', 'a/b']. The directories tar would otherwise imply."""
    parts = rel.split(os.sep)[:-1]
    return [os.sep.join(parts[:i + 1]) for i in range(len(parts))]


def expand(root, rel):
    """Every path at or under <root>/<rel>, as paths relative to root.

    Explicit rather than letting tar recurse, because a bucket holds only SOME
    children of a shared parent — telling tar to recurse into that parent would
    sweep in the pieces that belong to other buckets.
    """
    out = [rel]
    full = os.path.join(root, rel)
    if os.path.isdir(full) and not os.path.islink(full):
        for dirpath, dirnames, filenames in os.walk(full):
            base = os.path.relpath(dirpath, root)
            for name in dirnames + filenames:
                out.append(os.path.normpath(os.path.join(base, name)))
    return out


def members_for(root, pieces):
    """Full, sorted, de-duplicated member list for one bucket.

    Every ancestor directory is included EXPLICITLY. This is the whole reason
    the function exists. `ADD --chown` applies to tar members; directories that
    exist only implicitly in a member's path are created by the extractor and
    are NOT chowned, so a tar of 'sub/deep/leaf' lands leaf as app:app and both
    'sub' and 'sub/deep' as root:root 0755.

    That failure is invisible to any check that samples files — every file it
    can find is correct — and for Magento it is a live bug rather than a
    cosmetic one: php-fpm runs as app and writes resized-image caches under
    pub/media/catalog/product/cache/ at request time. The in-build validation
    greps HTML and would not see it.

    It is also newly load-bearing. Today restore-stage.sh follows the partition
    with a blanket `chown -R app:app /staging`, which papers over exactly this;
    moving to tars removes that sweep.
    """
    members = set()
    for rel in pieces:
        members.update(ancestors(rel))
        members.update(expand(root, rel))
    members.discard('.')
    members.discard('')
    return sorted(members)


def write_bucket(root, members, out_tar, listfile):
    """tar the exact member list, in the exact order given, without recursion."""
    with open(listfile, 'w') as fh:
        fh.write('\n'.join(members) + '\n')
    # --no-recursion: the member list is already complete and exact; letting tar
    #   recurse would pull in siblings belonging to other buckets.
    # --verbatim-files-from: a media filename may legitimately begin with '-'.
    # --numeric-owner: nothing downstream resolves these — ADD --chown in the
    #   Dockerfile overrides ownership for every member — but it keeps the tar
    #   byte-identical across hosts with different passwd databases.
    #
    # mtimes are deliberately NOT pinned. An earlier draft called for --mtime on
    # reproducibility grounds; that is wrong here. The source tree comes from a
    # sha256-pinned cache artifact, so real mtimes are already stable, and
    # pinning them would make the new path's media tree differ from the old
    # path's on the one comparison that proves this change is safe.
    subprocess.run(
        ['tar', 'cf', out_tar, '-C', root, '--no-recursion',
         '--verbatim-files-from', '--numeric-owner', '-T', listfile],
        check=True)


def audit(root, all_members):
    """Host-side checks. Full coverage, no docker, and cheap because it is local.

    partition-tree.py needs none of this: it MOVES, so a dropped file is still
    sitting in the root afterwards. Tarring copies, so a bucket can silently omit
    files and nothing downstream can tell. (The claim that the existing path is
    self-verifying was overstated in review — nothing actually asserts the root
    is empty either, so a dropped file is silently lost there too. This is the
    first version of this code path that checks.)
    """
    problems = []

    seen = set(all_members)
    for rel in all_members:
        for anc in ancestors(rel):
            if anc not in seen:
                problems.append(f'{rel}: ancestor {anc} is not an explicit member')
                break
        if os.path.isabs(rel) or os.pardir in rel.split(os.sep):
            problems.append(f'{rel}: absolute or traversing path')

    # Completeness: every entry under root must land in exactly one bucket.
    actual = set()
    for dirpath, dirnames, filenames in os.walk(root):
        base = os.path.relpath(dirpath, root)
        for name in dirnames + filenames:
            actual.add(os.path.normpath(os.path.join(base, name)))
    missing, extra = actual - seen, seen - actual
    if missing:
        problems.append(f'{len(missing)} entries in no bucket, e.g. {sorted(missing)[0]}')
    if extra:
        problems.append(f'{len(extra)} bucketed entries not in the tree, e.g. {sorted(extra)[0]}')
    # Duplicates are checked over FILES only. A directory legitimately appears in
    # every bucket that holds anything beneath it — that is the ancestor-member
    # rule above doing its job, and re-extracting a directory entry is idempotent.
    # A duplicated file, by contrast, means the same bytes ship in two layers.
    counts = {}
    for rel in all_members:
        counts[rel] = counts.get(rel, 0) + 1
    dupe_files = [rel for rel, n in counts.items()
                  if n > 1 and not os.path.isdir(os.path.join(root, rel))]
    if dupe_files:
        problems.append(f'{len(dupe_files)} files in more than one bucket, '
                        f'e.g. {sorted(dupe_files)[0]}')

    for rel in sorted(seen):
        full = os.path.join(root, rel)
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            continue  # already reported as `extra`
        mode = st.st_mode
        if stat.S_ISLNK(mode):
            target = os.path.realpath(full)
            if os.path.relpath(target, os.path.realpath(root)).startswith(os.pardir):
                problems.append(f'{rel}: symlink escapes the tree -> {target}')
            continue
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            problems.append(f'{rel}: not a regular file, directory or symlink')
            continue
        if stat.S_IMODE(mode) & (stat.S_ISUID | stat.S_ISGID):
            problems.append(f'{rel}: setuid/setgid ({stat.S_IMODE(mode):04o})')
        if stat.S_IMODE(mode) & stat.S_IWOTH:
            problems.append(f'{rel}: world-writable ({stat.S_IMODE(mode):04o})')

    if problems:
        for p in problems[:10]:
            print(f'  media audit: {p}', file=sys.stderr)
        raise SystemExit(f'media bucketing failed {len(problems)} check(s); '
                         f'first: {problems[0]}')


def main(argv):
    limit_kb, max_buckets, root, outdir = int(argv[1]), int(argv[2]), argv[3], argv[4]
    root = os.path.abspath(root)
    os.makedirs(outdir, exist_ok=True)

    assignments, sizes = partition_tree.plan(root, limit_kb, max_buckets)
    by_bucket = {}
    for piece, idx in assignments:
        by_bucket.setdefault(idx, []).append(os.path.relpath(piece, root))

    all_members = []
    print(f'{len(sizes)} media buckets:')
    for idx in range(len(sizes)):
        members = members_for(root, by_bucket.get(idx, []))
        all_members.extend(members)
        out_tar = os.path.join(outdir, f'bucket-{idx:02d}.tar')
        write_bucket(root, members, out_tar, os.path.join(outdir, f'.list-{idx:02d}'))
        os.remove(os.path.join(outdir, f'.list-{idx:02d}'))
        print(f'  bucket-{idx:02d}.tar: {sizes[idx] / 2**20:.1f}G, '
              f'{len(members)} entries')

    # After writing, not before: the audit is what licenses the build to trust
    # these tars, and it must see exactly what was written.
    audit(root, all_members)
    print(f'media audit passed: {len(all_members)} entries across {len(sizes)} buckets')


if __name__ == '__main__':
    main(sys.argv)
