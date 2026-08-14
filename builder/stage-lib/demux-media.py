"""Split a media archive into one tar per image layer without unpacking it.

The sibling bucket-media.py does the same partition against an extracted tree.
That costs three passes over the whole dataset — write the tree, walk it to
plan, read it back to tar — and the read-back is the expensive one: 45G of
small files is far larger than page cache, so nearly every read is a round trip
to the volume and the phase runs at device latency rather than throughput.

Nothing needs the tree. The archive was written by `tar c <subtree>`, so its
entries already arrive in depth-first order, and depth-first order is exactly
what the partition wants: fill a bucket until it reaches the limit, then move
to the next one. There is no plan to compute, so there is nothing to walk.

    python3 demux-media.py <limit_kb> <max_buckets> <min_entries> <strip>
                           <outdir> <part>...

The parts are read directly rather than through `cat`, so a split archive costs
no join. Bucket assignment is sequential and never revisits a bucket, which is
what makes "one file, exactly one bucket" true by construction.

bucket-media.py audits completeness by walking the tree afterwards and
comparing. There is no tree to compare against here, so completeness is
structural instead: every entry read is written exactly once, and a bucket is
only ever moved forward. What that structure cannot give — the per-entry mode,
type and path checks — is applied to the headers as they go past.
"""

import os
import sys
import tarfile
import time

BLOCK = 512


class Parts:
    """The parts read end to end, as one stream. What `cat a b c |` is.

    tarfile in stream mode only ever reads forward, so this needs no seek — and
    not implementing one keeps a future change from silently making the reads
    random against a volume this workload is already latency-bound on.
    """

    def __init__(self, paths):
        self.paths, self.i = list(paths), 0
        self.fh = open(self.paths[0], 'rb')

    def read(self, n=-1):
        buf = b''
        while n < 0 or len(buf) < n:
            chunk = self.fh.read(n - len(buf) if n > 0 else -1)
            if chunk:
                buf += chunk
                continue
            if self.i + 1 >= len(self.paths):
                break
            self.fh.close()
            self.i += 1
            self.fh = open(self.paths[self.i], 'rb')
        return buf

    def close(self):
        self.fh.close()


def strip_prefix(name, strip):
    """Drop the archive's leading subtree, the way --strip-components does.

    Returns '' for the stripped root itself, which has no member to emit: its
    destination directory already exists in the image.
    """
    if not strip:
        return name
    depth = len(strip.split('/'))
    parts = name.split('/')
    return '/'.join(parts[depth:])


def entry_blocks(size):
    """What one member costs a tar: its header, plus the payload padded out.

    limit_kb is sized against dockerd's heap while the bucket is ADDed, so the
    ceiling has to be measured in what the file will hold. On a tree of
    millions of small entries the 512-byte headers and the padding are hundreds
    of megabytes a bucket — enough to overshoot the limit outright.
    """
    return BLOCK + (size + BLOCK - 1) // BLOCK * BLOCK


def record(problems, category, detail):
    """Count every occurrence of a problem; keep the first few as evidence.

    The scan runs to the end of the archive rather than stopping at the first
    failure. Reaching this point in CI costs twelve minutes of dataset
    download, so a run that is going to fail should report everything wrong
    with the tree, not the first ten entries' worth. The example cap is what
    keeps a tree that trips one check on every entry from accumulating
    millions of strings on the way there.
    """
    p = problems.setdefault(category, {'n': 0, 'examples': []})
    p['n'] += 1
    if len(p['examples']) < 3:
        p['examples'].append(detail)


def check(m, name, problems):
    """The per-entry checks, run on the header rather than on a file on disk.

    Same predicates bucket-media.py's audit applies to a tree, against the only
    description of the entry a stream has.

    Reading the archive sees modes an extraction would have altered: `tar x`
    applies the caller's umask, and the kernel drops setgid when the caller is
    not in the entry's group. The tree walk therefore audited laundered modes,
    and which ones depended on the runner's umask. These are the archive's own,
    so what ships is now the same on any host — and an unsafe mode is refused
    rather than quietly masked off on the way past.
    """
    if name.startswith('/') or os.pardir in name.split('/'):
        record(problems, 'absolute or traversing path', name)
    if m.islnk():
        # A hard link's target has to be in the same tar, and the target may
        # already have gone into an earlier bucket — where this one cannot
        # follow it, because the bytes are behind us in a stream that does not
        # rewind. Refuse rather than emit a bucket that fails to extract.
        record(problems, 'hard link', f'{name} -> {m.linkname}')
        return
    if m.issym():
        target = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname))
        if os.path.isabs(m.linkname) or target.startswith(os.pardir):
            record(problems, 'symlink escapes the tree', f'{name} -> {m.linkname}')
        # No mode checks past here: a symlink's own mode is conventionally 0777
        # and means nothing — the target's mode governs — so testing it would
        # report every symlink in the tree as world-writable.
        return
    if not (m.isfile() or m.isdir()):
        record(problems, 'not a regular file, directory or symlink', name)
        return
    if m.isfile() and m.mode & (0o4000 | 0o2000):
        # Files only. On a directory the setgid bit means "new entries inherit
        # this group", which is what Magento's own filesystem-permissions
        # procedure sets across pub/media — a convention, not a privilege. It
        # is a privilege on an executable, and there should be none here.
        record(problems, 'setuid/setgid', f'{name} ({m.mode:04o})')
    if m.mode & 0o002:
        record(problems, 'world-writable', f'{name} ({m.mode:04o})')


def demux(paths, outdir, strip, limit, max_buckets):
    """Read the archive once, writing each entry into exactly one bucket tar."""
    stream = Parts(paths)
    # GNU rather than tarfile's default PAX: it is the format the extracted-tree
    # path produced, so the two remain byte-comparable per entry, and it spends
    # no extended header on entries that do not need one.
    buckets = [tarfile.open(os.path.join(outdir, f'bucket-{i:02d}.tar'), 'w|',
                            format=tarfile.GNU_FORMAT)
               for i in range(max_buckets)]

    idx, used, entries = 0, 0, [0] * max_buckets
    stack, opened, top_dirs, top_names, problems = [], set(), [], set(), {}
    n, nbytes, started, last = 0, 0, time.monotonic(), time.monotonic()

    with tarfile.open(fileobj=stream, mode='r|') as src:
        for m in src:
            name = strip_prefix(m.name, strip)
            if not name:
                continue
            check(m, name, problems)
            depth = name.count('/')
            # Renamed before the ancestor stack takes it: the stack holds these
            # very objects, so a strip deferred to flush time would strip an
            # entry again every time it is carried into a further bucket.
            m.name = name
            if m.isdir():
                del stack[depth:]
                stack.append(m)
                if depth == 0 and name not in top_names:
                    # By name, because an archive is free to carry a directory
                    # entry more than once and this list is padding: repeats
                    # would inflate an unused bucket without adding anything.
                    # Through a set, because `strip` decides what counts as top
                    # level — stripping one level too deep promotes every item
                    # directory in the tree, and a linear membership test would
                    # then be quadratic in the whole archive.
                    top_names.add(name)
                    top_dirs.append(m)
            elif m.isfile() and used and used + entry_blocks(m.size) > limit:
                # Forward only. One file therefore lands in exactly one bucket,
                # and depth-first order means the next bucket resumes where this
                # one stopped rather than interleaving unrelated subtrees.
                idx, used = idx + 1, 0
                if idx >= max_buckets:
                    raise SystemExit(f'error: tree outgrew {max_buckets} buckets '
                                     f'at {name}; raise max_buckets or limit_kb')
            if idx not in opened:
                # `ADD --chown` applies to tar MEMBERS. A directory that exists
                # only implicitly in a member's path is created by the extractor
                # and is NOT chowned, so it lands root:root — and php-fpm runs as
                # app and writes resized-image caches beneath these. Every
                # ancestor of a bucket's first entry is carried in explicitly.
                opened.add(idx)
                for anc in stack[:depth]:
                    buckets[idx].addfile(anc)
                    entries[idx] += 1
            buckets[idx].addfile(m, src.extractfile(m) if m.isreg() else None)
            entries[idx] += 1
            used += entry_blocks(m.size)
            n += 1
            nbytes += m.size
            now = time.monotonic()
            if now - last >= 30:
                # stdout is a pipe here, so Python block-buffers it and a phase
                # this long would otherwise surface all at once, after the fact.
                print(f'  media demux: {n} entries, {nbytes / 2**30:.1f}G, '
                      f'{n / (now - started):,.0f} entries/s after '
                      f'{now - started:.0f}s', flush=True)
                last = now

    unused = [i for i in range(max_buckets) if i not in opened]
    for i in unused:
        # An unused bucket still needs a tar, and it must not be empty: docker
        # does not recognise a zero-member tar as an archive, so ADD copies it
        # verbatim and drops a literal bucket-NN.tar into the tree. Top-level
        # directories are safe padding — they already appear in other buckets,
        # they carry their real mode, and re-extracting one changes nothing.
        # When there are none to pad with, the refusal below is what fires.
        for d in top_dirs:
            buckets[i].addfile(d)
            entries[i] += 1
    for b in buckets:
        b.close()
    stream.close()

    # Ahead of the sizing check below, which is a question about this image's
    # max_buckets. What the audit found is a question about the tree itself, and
    # it is the more useful thing to be told when both are true.
    if problems:
        for cat, p in problems.items():
            print(f"  media audit: {p['n']} x {cat}, e.g. "
                  + '; '.join(p['examples']), file=sys.stderr)
        raise SystemExit('media demux refused the tree: '
                         + ', '.join(f"{cat} x{p['n']}"
                                     for cat, p in problems.items()))
    if not n:
        raise SystemExit('error: the media archive held no entries')
    if unused and not top_dirs:
        # Padding is the only thing keeping an unused bucket from being that
        # zero-member tar, and there is nothing to pad with when the archive's
        # top level is all files — which is what `strip` leaves behind when it
        # points at a directory holding no subdirectories. Refused here rather
        # than shipped, because the failure is silent downstream: the build is
        # green and the image carries a tar where the tree should be.
        raise SystemExit(
            f'error: {len(unused)} of {max_buckets} buckets went unused and the '
            f'archive has no top-level directory to pad them with. Lower '
            f'max_buckets to {len(opened)} — the media path cannot emit an empty '
            f'bucket, and a padded one is what the ADD lines count on.')
    return n, nbytes, entries, len(opened), time.monotonic() - started


def main(argv):
    limit_kb, max_buckets = int(argv[1]), int(argv[2])
    min_entries, strip, outdir = int(argv[3]), argv[4], argv[5]
    parts = argv[6:]
    if not parts:
        raise SystemExit('usage: demux-media.py <limit_kb> <max_buckets> '
                         '<min_entries> <strip> <outdir> <part>...')
    os.makedirs(outdir, exist_ok=True)
    print(f'demuxing {len(parts)} archive part(s) into {max_buckets} bucket tars',
          flush=True)
    n, nbytes, entries, used, secs = demux(
        parts, outdir, strip, limit_kb * 1024, max_buckets)
    for i in range(max_buckets):
        size = os.path.getsize(os.path.join(outdir, f'bucket-{i:02d}.tar'))
        print(f'  bucket-{i:02d}.tar: {entries[i]} entries, {size} bytes',
              flush=True)
    if n < min_entries:
        # The last point at which a short archive is visible. Past here the tree
        # is layers: it never enters a build stage, so no in-build assertion can
        # count it, and an image missing its photos builds and smokes green.
        raise SystemExit(f'error: the media archive holds {n} entries, under the '
                         f'{min_entries} this image declares — it is truncated, '
                         f'or the wrong archive')
    print(f'media demux passed: {n} entries, {nbytes / 2**30:.1f}G across {used} '
          f'buckets in {secs:.0f}s ({n / secs:,.0f} entries/s)', flush=True)


if __name__ == '__main__':
    main(sys.argv)
