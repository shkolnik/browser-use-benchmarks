"""Split one big directory tree into N staging buckets, one per image layer.

Four images in this fleet ship a tree too big for one layer — shopping 45G of
pub/media, reddit 38.5G of submission_images, classifieds 73G of
oc-content/uploads, gitlab 30G of /var/opt/gitlab. A registry rejects a layer
that size, so the tree is partitioned at build time into buckets under a fixed
byte target and the final stage takes one COPY per bucket.

It lives here, in a build context shared by every image, because the same bug
was fixed in two separate copies of it on one afternoon (the ROOT-nesting trap
below). The copy that was not shared went on to ship a fourth bug of its own:
gitlab kept a private fork of this file and lost directory ownership through it
for three CI runs (the mkdir_mirroring note below).

Usage, from an image's restore stage:
    COPY --from=stagelib partition-tree.py /partition-tree.py
    python3 /partition-tree.py <limit_kb> <max_buckets> <root> <staging_dir>

Leaves <staging_dir>/bucket-NN/ populated and <root> empty. Exits non-zero, with
the offending path named, if the tree cannot be split under those bounds — a
build that would push an oversized layer fails here rather than at push time.
"""

import errno
import os
import shutil
import stat
import sys
import time

BLOCK = 512


class Ticker:
    """Prints at most every INTERVAL seconds, flushed.

    Both halves of this script are long and were silent: the measuring walk
    stats every inode in the tree, and the move loop then rewrites most of it,
    since a piece coming from a lower overlay layer is copied rather than
    renamed (see move_preserving). On gitlab's 30G that is minutes in which
    nothing prints, and a silent build stage is indistinguishable from a hung
    one.

    Flushed because stdout is a pipe under buildkit, so Python block-buffers
    it — the progress would otherwise all surface at the end, which is exactly
    when it is no longer progress.
    """

    INTERVAL = 30

    def __init__(self):
        self.started = self.last = time.monotonic()

    def tick(self, msg):
        now = time.monotonic()
        if now - self.last >= self.INTERVAL:
            self.last = now
            print(f'  partition: {msg} after {now - self.started:.0f}s',
                  flush=True)


def entry_blocks(size):
    """What one member costs a tar: its header, plus the payload padded out.

    The same accounting as demux-media.py, and for the same reason: a bucket
    becomes a COPY layer, a layer is a tar, and the bound that matters is what
    the tar writes. `du -sk` answers a different question — allocated blocks —
    and is wrong in both directions. Measured on three trees of 2,000 entries:

        2,000 x 100B   du 8,000K   tar 2,010K   (du over by 4x: 4K per file)
        2,000 x 4096B  du 8,000K   tar 9,010K   (du under: 512B header each)
        one 64M sparse du     0K   tar 65,540K  (du under by the whole file)

    Apparent size is not the answer either — it misses the header and padding,
    and reports the first tree as 196K against a real 2,010K.
    """
    return BLOCK + (size + BLOCK - 1) // BLOCK * BLOCK


def measure(root):
    """{path: bytes tar would write for it and everything under it}.

    One walk for the whole tree, so a directory descended into is not measured
    again at each level. Sparse files count their apparent size, which is what
    tar writes: it has no --sparse here, so a hole becomes zeroes in the layer.

    Hard links are charged in full rather than as the link entry tar would
    actually emit, and a name over 100 bytes is charged its GNU long-name
    header. Both err high. A bucket that measures larger than it lands is a
    build that packs slightly loose; one that measures smaller is a layer the
    registry refuses after the whole tree has already been built.
    """
    sizes = {}
    ticker = Ticker()

    def walk(path, name):
        st = os.lstat(path)
        # ././@LongLink: a header plus the padded name, ahead of the real entry.
        extra = 0 if len(name.encode()) <= 100 else entry_blocks(len(name.encode()) + 1)
        if stat.S_ISDIR(st.st_mode):
            total = BLOCK + extra
            for e in sorted(os.scandir(path), key=lambda e: e.path):
                total += walk(e.path, os.path.join(name, e.name))
        elif stat.S_ISLNK(st.st_mode):
            total = BLOCK + extra
        else:
            total = entry_blocks(st.st_size) + extra
        sizes[path] = total
        # After the entry is recorded, so the count is of entries finished
        # rather than of frames on the way down.
        ticker.tick(f'measured {len(sizes):,} entries')
        return total

    walk(root, os.path.basename(root.rstrip(os.sep)))
    return sizes


def children(path, limit, sizes):
    """Yield (path, bytes) pieces each <= limit, descending into oversized dirs."""
    entries = sorted(os.path.join(path, e) for e in os.listdir(path))
    if not entries:
        raise SystemExit(f'{path} is {sizes[path]}B with no children to descend into')
    for entry in entries:
        # Measured once, up front, so a directory that already fits is yielded
        # whole without re-walking it — the common case by far, and the reason
        # 84k item directories partition in seconds rather than minutes.
        if sizes[entry] <= limit:
            yield entry, sizes[entry]
        elif os.path.isdir(entry) and not os.path.islink(entry):
            yield from children(entry, limit, sizes)
        else:
            yield entry, sizes[entry]


def plan(root, limit_kb, max_buckets):
    """Return (assignments, bucket_sizes) — first-fit over the tree's pieces.

    An assignment is (piece, bucket_index, bytes). The size is carried out
    rather than recomputed: the move loop reports progress against the tree's
    total, and re-measuring a piece to say how big it was would walk the whole
    tree a second time to learn something already known here.
    """
    limit = limit_kb * 1024
    sizes = measure(root)
    buckets = []  # [used_bytes, index]
    assignments = []
    # children(), never partition(): ROOT must not be yielded as a piece of
    # itself. relpath(ROOT, ROOT) is '.', so the move lands at bucket-NN/. and
    # shutil nests the whole tree as bucket-NN/<basename> — the image then gets
    # /app/app/... and the web root points at nothing. It only bites when the
    # tree fits in ONE bucket, so a shrinking dataset is all it takes; caught by
    # booting a build made with an empty media tar.
    for piece, nbytes in children(root, limit, sizes):
        if nbytes > limit:
            raise SystemExit(f'single file {piece} is {nbytes // 1024}K as a tar '
                             f'member, over the {limit_kb}K layer limit')
        for b in buckets:
            if b[0] + nbytes <= limit:
                b[0] += nbytes
                assignments.append((piece, b[1], nbytes))
                break
        else:
            if len(buckets) == max_buckets:
                raise SystemExit(f'tree outgrew {max_buckets} buckets; raise the '
                                 'count here and add COPY lines in the Dockerfile')
            buckets.append([nbytes, len(buckets)])
            assignments.append((piece, len(buckets) - 1, nbytes))
    return assignments, [used for used, _ in buckets]


def mkdir_mirroring(root, bucket, rel_dir):
    """Create <bucket>/<rel_dir>, each level owned and moded like <root>/<rel_dir>.

    os.makedirs would create them 0755 root-owned. Buckets become COPY layers,
    so that loss ships, and nothing downstream can tell it happened: the piece
    that MOVED keeps its owner, and only the directories recreated here lose
    theirs. It bites exactly the trees big enough to be descended into, which
    are the ones worth shipping.

    gitlab is the case that cannot be swept up afterwards. Its siblings each run
    one app user and follow the partition with `chown -R <user> /staging`, but
    gitlab's tree is split across git, gitlab-psql, gitlab-redis and gitlab-www,
    so there is no single owner to re-assert — the owner has to be preserved
    rather than restored.
    """
    dest, src = bucket, root
    os.makedirs(dest, exist_ok=True)
    if rel_dir in ('', os.curdir):
        return
    for part in rel_dir.split(os.sep):
        dest, src = os.path.join(dest, part), os.path.join(src, part)
        if not os.path.isdir(dest):
            os.mkdir(dest)
        # The source parent still exists: plan() only ever yields leaves, so a
        # directory it descended into is never itself moved.
        st = os.stat(src)
        os.chown(dest, st.st_uid, st.st_gid)
        # After chown, which clears setuid/setgid on some systems. git-data is
        # 2770 — dropping the setgid bit is the same class of silent breakage.
        os.chmod(dest, stat.S_IMODE(st.st_mode))


def _clone_meta(src_st, dest):
    """Give dest the owner, mode and times of the source it was copied from."""
    os.chown(dest, src_st.st_uid, src_st.st_gid)
    # chmod AFTER chown, which clears setuid/setgid. git-data is 2770.
    os.chmod(dest, stat.S_IMODE(src_st.st_mode))
    os.utime(dest, (src_st.st_atime, src_st.st_mtime))


def copy_preserving(src, dest):
    """Recursive copy that keeps ownership, which shutil's copytree does not."""
    st = os.lstat(src)
    if stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(src), dest)
        os.chown(dest, st.st_uid, st.st_gid, follow_symlinks=False)
        return
    if stat.S_ISDIR(st.st_mode):
        os.mkdir(dest)
        for name in os.listdir(src):
            copy_preserving(os.path.join(src, name), os.path.join(dest, name))
        # After the children: creating them moves this directory's mtime.
        _clone_meta(st, dest)
        return
    shutil.copyfile(src, dest)
    _clone_meta(st, dest)


def move_preserving(src, dest):
    """Move src onto dest with owner and mode intact, whatever the filesystem.

    os.rename keeps everything, but only within one filesystem. The pieces moved
    here usually come from a LOWER overlayfs layer, where renaming a directory
    raises EXDEV — and shutil.move's fallback is copytree, which copies mode and
    times but NOT ownership. That is silent and path-dependent: a leaf written
    during the build renames cleanly and keeps its owner, while a leaf inherited
    from the base image is copied and arrives root-owned.

    gitlab shipped exactly that. /var/opt/gitlab/prometheus is gitlab-prometheus
    in the base image, so it took the copy path and reached the final image
    root-owned; runit runs prometheus as gitlab-prometheus, which then panicked
    on its TSDB and was restarted forever. /var/opt/gitlab/git-data, created by
    the restore in the upper layer, renamed and kept git:git — which is why the
    loss looked like it was confined to one directory rather than to one
    CODE PATH. Fixing the recreated parents (mkdir_mirroring) did not touch it:
    that fix covers the directories on the way to a leaf, this one covers the
    leaf itself.
    """
    try:
        os.rename(src, dest)
        return
    except OSError as err:
        if err.errno != errno.EXDEV:
            raise
    copy_preserving(src, dest)
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.rmtree(src)
    else:
        os.remove(src)


def main(argv):
    limit_kb, max_buckets, root, staging = int(argv[1]), int(argv[2]), argv[3], argv[4]
    assignments, sizes = plan(root, limit_kb, max_buckets)
    total = sum(sizes)
    ticker, moved = Ticker(), 0
    for piece, idx, nbytes in assignments:
        rel = os.path.relpath(piece, root)
        bucket = os.path.join(staging, f'bucket-{idx:02d}')
        mkdir_mirroring(root, bucket, os.path.dirname(rel))
        # Not shutil.move: its cross-filesystem fallback drops ownership, and
        # cross-layer directory renames DO hit EXDEV here. See move_preserving.
        move_preserving(piece, os.path.join(bucket, rel))
        moved += nbytes
        # Bytes, not pieces: a piece is a whole subtree that fit under the limit,
        # so they run from one file to most of a bucket and a count of them says
        # nothing about how much is left.
        ticker.tick(f'moved {moved / 2**30:.1f}G of {total / 2**30:.1f}G')
    print(f'{len(sizes)} buckets, sized as the tar each one becomes:')
    for idx, used in enumerate(sizes):
        print(f'  bucket-{idx:02d}: {used / 2**30:.1f}G')


if __name__ == '__main__':
    main(sys.argv)
