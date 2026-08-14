"""Split an inert media tree into one tar per image layer, on the CI host.

The sibling partition-tree.py does the same partition inside a restore stage,
moving files into bucket-NN/ directories that the final stage COPYs out. That is
the right shape for a tree the build PRODUCES (gitlab, map-nominatim), where the
files do not exist until the stage runs.

An inert tree does not need to be in a build stage at all. Partitioning it here
— on the host, no docker — lets the final stage ADD the tars directly, so its
entries are walked once by tar instead of once into the restore snapshot and
again out of it. Entry count, not bytes, is what a bucket COPY costs.

    python3 bucket-media.py <limit_kb> <max_buckets> <root> <outdir>

Leaves <outdir>/bucket-NN.tar and <root> UNTOUCHED — unlike partition-tree.py,
which moves. Copying is not self-evidencing the way moving is (nothing is left
behind to show a file was missed), which is what audit() below is for.

The first-fit planner is imported from partition-tree.py rather than copied; see
that module's docstring for why a shared copy matters here.
"""

import concurrent.futures
import importlib.util
import os
import stat
import subprocess
import sys
import time

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

    Explicit rather than letting tar recurse: a bucket holds only SOME children
    of a shared parent, so recursing into that parent would sweep in pieces
    belonging to other buckets.
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

    Every ancestor directory is included EXPLICITLY, which is the whole reason
    this function exists. `ADD --chown` applies to tar members; a directory that
    exists only implicitly in a member's path is created by the extractor and is
    NOT chowned. So a tar of 'sub/deep/leaf' lands the leaf app-owned and both
    'sub' and 'sub/deep' root:root 0755.

    Nothing downstream can sample its way to that fact — every file is correct —
    and a root-owned directory under pub/media is a runtime failure, because
    php-fpm runs as app and writes resized-image caches beneath it.
    """
    members = set()
    for rel in pieces:
        members.update(ancestors(rel))
        members.update(expand(root, rel))
    members.discard('.')
    members.discard('')
    if not members:
        # An unused bucket still needs a tar, and it must not be empty: docker
        # does not recognise a zero-member tar as an archive, so ADD copies it
        # verbatim and drops a literal bucket-NN.tar into the media tree.
        #
        # Top-level directories are the safe padding. They already appear in
        # other buckets (directories may repeat, unlike files), they carry their
        # real mode, and re-extracting one changes nothing.
        return sorted(e for e in os.listdir(root)
                      if os.path.isdir(os.path.join(root, e))
                      and not os.path.islink(os.path.join(root, e)))
    return sorted(members)


def write_bucket(root, members, out_tar, listfile):
    """tar the exact member list, in the exact order given, without recursion."""
    with open(listfile, 'w') as fh:
        # No trailing newline on an empty list: a lone '\n' is an empty filename,
        # which tar rejects. Unused buckets are normal — max_buckets is a ceiling.
        fh.write('\n'.join(members) + ('\n' if members else ''))
    # --no-recursion: the member list is already complete and exact; letting tar
    #   recurse would pull in siblings belonging to other buckets.
    # --verbatim-files-from: a media filename may legitimately begin with '-'.
    # --numeric-owner: nothing downstream resolves these — ADD --chown in the
    #   Dockerfile overrides ownership for every member — but it keeps the tar
    #   byte-identical across hosts with different passwd databases.
    #
    # mtimes are NOT pinned: the source tree comes from a sha256-pinned cache
    # artifact, so real mtimes are already stable, and pinning them would make
    # this path's media tree differ from the COPY path's — which is the
    # comparison that shows the two are equivalent.
    subprocess.run(
        ['tar', 'cf', out_tar, '-C', root, '--no-recursion',
         '--verbatim-files-from', '--numeric-owner', '-T', listfile],
        check=True)


def write_one(root, members, outdir, idx):
    """One bucket, start to finish, for a worker thread.

    Returns (idx, bytes, seconds) rather than printing: interleaved progress
    from concurrent tars would be unreadable, and the caller prints on
    completion. The list file is per-index, so nothing here is shared.
    """
    out_tar = os.path.join(outdir, f'bucket-{idx:02d}.tar')
    listfile = os.path.join(outdir, f'.list-{idx:02d}')
    started = time.monotonic()
    write_bucket(root, members, out_tar, listfile)
    os.remove(listfile)
    return idx, os.path.getsize(out_tar), time.monotonic() - started


def audit(root, all_members):
    """Host-side checks: full coverage, no docker, cheap because it is local.

    A bucket that silently omits files is undetectable downstream — the image
    just ships a smaller media tree — so completeness is asserted here, against
    the source tree, rather than inferred later.
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
    # Files only. A directory appears in every bucket holding anything beneath
    # it — the ancestor-member rule requires that, and re-extracting a directory
    # is idempotent. A duplicated FILE means the same bytes ship in two layers.
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

    # Every phase below walks a 45G tree of small files and is minutes long.
    # flush on all of them: stdout is a pipe here, so Python block-buffers it
    # and the whole run would surface at once, after the fact. The elapsed
    # times are what say which phase to work on next.
    started = time.monotonic()
    print(f'planning media buckets under {root}', flush=True)
    assignments, sizes = partition_tree.plan(root, limit_kb, max_buckets)
    by_bucket = {}
    for piece, idx, _ in assignments:
        by_bucket.setdefault(idx, []).append(os.path.relpath(piece, root))
    print(f'planned in {time.monotonic() - started:.0f}s', flush=True)

    # Every index up to the ceiling gets a tar. The Dockerfile's ADD lines are
    # static, so each must match a file that exists; max_buckets is a ceiling
    # with headroom, not a target.
    all_members, plans = [], []
    print(f'{len(sizes)} of {max_buckets} media buckets used:', flush=True)
    for idx in range(max_buckets):
        members = members_for(root, by_bucket.get(idx, []))
        all_members.extend(members)
        used = sizes[idx] if idx < len(sizes) else 0
        print(f'  bucket-{idx:02d}.tar: {len(members)} entries, '
              f'{used / 2**30:.1f}G', flush=True)
        plans.append((idx, members))

    # Written concurrently because each of these is a million-odd small reads
    # issued one at a time, and the tree is far larger than page cache — so
    # almost every read is a round trip to the volume and the phase runs at
    # device latency, not device throughput. Measured at ~1000 entries/s per
    # tar, ~1ms each, against a volume provisioned for thousands of concurrent
    # IOPS. Concurrency is what spends that headroom; buffer sizes cannot,
    # since a file smaller than the record is already one read.
    #
    # All of them at once, and deliberately not capped at the CPU count: a tar
    # blocked on a read and the thread waiting on it both burn no CPU, so the
    # contended resource is the volume's queue, not the scheduler. max_buckets
    # is already a small static ceiling — it has to match the Dockerfile's ADD
    # lines — so this cannot run away.
    workers = len(plans)
    print(f'writing {len(plans)} bucket tars, {workers} at a time', flush=True)
    write_started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        # Submitted in order, so the first `workers` buckets start together and
        # each completion frees a slot — no barrier between them.
        futures = {pool.submit(write_one, root, members, outdir, idx): idx
                   for idx, members in plans}
        for future in concurrent.futures.as_completed(futures):
            idx, size, secs = future.result()
            print(f'  bucket-{idx:02d}.tar: {size} bytes in {secs:.0f}s', flush=True)
    print(f'wrote {len(plans)} bucket tars in '
          f'{time.monotonic() - write_started:.0f}s', flush=True)

    # After writing, so the audit sees exactly the member set that shipped.
    print(f'auditing {len(all_members)} members', flush=True)
    audit_started = time.monotonic()
    audit(root, all_members)
    print(f'media audit passed: {len(all_members)} entries across {len(sizes)} '
          f'buckets in {time.monotonic() - audit_started:.0f}s '
          f'({time.monotonic() - started:.0f}s total)', flush=True)


if __name__ == '__main__':
    main(sys.argv)
