#!/usr/bin/env python3
"""Choose split boundaries for a ZIM archive that keep its Xapian indexes whole.

Splitting a ZIM is free for article reads and NOT free for search. libzim hands
Xapian a file descriptor plus an offset, which it can only do when the index's
blob lies entirely inside ONE part file. Cut it and kiwix-serve keeps serving
every article correctly while search degrades — the fulltext index loudly
("The fulltext search engine is not available for this content", HTTP 404), the
title index SILENTLY: /suggest still answers 200, having fallen back from
Xapian relevance ranking to an alphabetical prefix scan.

Measured on kiwix-serve 3.3.0, 2026-08-08, part COUNT held constant so the
boundary was the only variable:

    boundary inside the fulltext index -> search 404, articles 200
    boundary just before it            -> search 200, articles 200

So boundaries are PLACED, not spaced. libzim derives part offsets from the
actual file sizes, so parts need not be equal — verified live with a
7 MiB/190 MiB/125 MiB split whose article bytes and search page were both
byte-identical to the unsplit archive.

Reads only a few KB: the ZIM header, one dirent, and one cluster header.

Usage:
    zim_layout.py extents <file.zim>              # what must stay whole, and where
    zim_layout.py boundaries <file.zim> <bytes>   # cut offsets, one per line
"""
import struct
import sys

# The entries that must not be cut. Both are Xapian databases reached by direct
# access; everything else in a ZIM is read through libzim's own reader, which
# spans parts happily.
PROTECTED = ("X/fulltext/xapian", "X/title/xapian")


class Zim:
    def __init__(self, path):
        self.f = open(path, "rb")
        magic, self.major, self.minor = struct.unpack("<IHH", self._rd(0, 8))
        if magic != 72173914:
            raise SystemExit(f"{path}: not a ZIM (magic={magic})")
        self.entry_count, self.cluster_count = struct.unpack("<II", self._rd(24, 8))
        (self.url_ptr_pos, _title_ptr_pos, self.cluster_ptr_pos, _mime) = struct.unpack(
            "<QQQQ", self._rd(32, 32)
        )

    def _rd(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def _dirent(self, idx):
        (doff,) = struct.unpack("<Q", self._rd(self.url_ptr_pos + 8 * idx, 8))
        head = self._rd(doff, 16)
        (mimetype, _parmlen, ns) = struct.unpack("<HBc", head[:4])
        if mimetype == 0xFFFF:  # redirect: no blob of its own
            return None
        (cluster, blob) = struct.unpack("<II", head[8:16])
        url = self._rd(doff + 16, 512).split(b"\0")[0].decode()
        return ns.decode(), url, cluster, blob

    def extent(self, path):
        """(start, end) of an entry's blob, or None if it isn't in this archive."""
        want = tuple(path.split("/", 1))
        lo, hi = 0, self.entry_count - 1
        found = None
        while lo <= hi:  # entries are sorted by (namespace, url)
            mid = (lo + hi) // 2
            d = self._dirent(mid)
            if d is None:
                lo = mid + 1
                continue
            key = (d[0], d[1])
            if key == want:
                found = d
                break
            if key < want:
                lo = mid + 1
            else:
                hi = mid - 1
        if found is None:
            return None

        _ns, _url, cluster, blob = found
        (cl_off,) = struct.unpack(
            "<Q", self._rd(self.cluster_ptr_pos + 8 * cluster, 8)
        )
        info = self._rd(cl_off, 1)[0]
        compression, extended = info & 0x0F, bool(info & 0x10)
        if compression not in (0, 1):
            # A compressed cluster cannot be direct-accessed at all, so the blob
            # has no byte extent to protect and search would be unavailable
            # however we split. Refuse rather than emit a layout that looks safe.
            raise SystemExit(
                f"{path}: cluster {cluster} is compressed (type {compression}); "
                "this archive cannot serve that index by direct access"
            )
        wsz, code = (8, "Q") if extended else (4, "I")
        (first,) = struct.unpack(f"<{code}", self._rd(cl_off + 1, wsz))
        n = first // wsz - 1
        offs = struct.unpack(f"<{n + 1}{code}", self._rd(cl_off + 1, wsz * (n + 1)))
        return cl_off + 1 + offs[blob], cl_off + 1 + offs[blob + 1]


def boundaries(zim, total, part_bytes, protected):
    """Cut offsets at most part_bytes apart, none landing inside a protected range.

    A boundary that would fall inside a range is pulled back to the range's
    start, so the range opens a part instead of straddling two. Pulling BACK
    rather than pushing forward keeps every part at or under part_bytes except
    the ones a protected range makes unavoidably larger.
    """
    out = []
    pos = 0
    while total - pos > part_bytes:
        cut = pos + part_bytes
        for start, end in protected:
            if start < cut < end:
                cut = start
                break
        if cut <= pos:
            # The range starts at or before this part's own start, i.e. it is
            # bigger than part_bytes: it has to be its own oversized part.
            cut = next(e for s, e in protected if s <= pos < e)
        out.append(cut)
        pos = cut
    return out


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    cmd, path = sys.argv[1], sys.argv[2]
    z = Zim(path)
    ext = [(p, z.extent(p)) for p in PROTECTED]

    if cmd == "extents":
        for name, e in ext:
            print(f"{name}\t{e[0]}\t{e[1]}\t{e[1] - e[0]}" if e else f"{name}\tabsent")
        return
    if cmd == "boundaries":
        import os

        part_bytes = int(sys.argv[3])
        total = os.path.getsize(path)
        for b in boundaries(z, total, part_bytes, [e for _n, e in ext if e]):
            print(b)
        return
    raise SystemExit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    main()
