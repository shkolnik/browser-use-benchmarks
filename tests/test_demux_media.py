"""demux-media.py: one archive stream in, N bucket tars out, no tree on disk."""

import importlib.util
import io
import os
import tarfile

import pytest

_spec = importlib.util.spec_from_file_location(
    "demux_media",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "builder", "stage-lib", "demux-media.py"))
dm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dm)


def build_archive(path, entries, root="media"):
    """Write a tar shaped like the real one: a stripped root, dirs in DFS order.

    `entries` is a list of (relpath, size) for files; every directory on the way
    is emitted before its contents, which is what `tar c <subtree>` does and
    what the demux relies on for its ancestor stack.
    """
    seen = set()
    with tarfile.open(path, "w") as tf:
        d = tarfile.TarInfo(root)
        d.type, d.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(d)
        for rel, size in entries:
            parts = rel.split("/")[:-1]
            for i in range(len(parts)):
                anc = "/".join(parts[:i + 1])
                if anc in seen:
                    continue
                seen.add(anc)
                di = tarfile.TarInfo(f"{root}/{anc}")
                di.type, di.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(di)
            ti = tarfile.TarInfo(f"{root}/{rel}")
            ti.size, ti.mode = size, 0o644
            tf.addfile(ti, io.BytesIO(b"x" * size))
    return path


def read_buckets(outdir, max_buckets):
    """{bucket index: {name: TarInfo}} for every emitted tar."""
    out = {}
    for i in range(max_buckets):
        with tarfile.open(os.path.join(outdir, f"bucket-{i:02d}.tar")) as tf:
            out[i] = {m.name: m for m in tf}
    return out


def test_every_file_lands_in_exactly_one_bucket(tmp_path):
    src = build_archive(tmp_path / "media.tar",
                        [(f"cache/{i // 8}/img-{i:03d}.jpg", 4096) for i in range(64)])
    out = tmp_path / "out"
    out.mkdir()
    n, nbytes, entries, used, _ = dm.demux([str(src)], str(out), "media", 64 * 1024, 6)

    buckets = read_buckets(out, 6)
    files = [name for b in buckets.values() for name, m in b.items() if m.isfile()]
    assert sorted(files) == sorted(f"cache/{i // 8}/img-{i:03d}.jpg" for i in range(64))
    assert len(files) == len(set(files)), "a file shipped in more than one layer"
    assert used > 1, "the limit should have forced a roll, or this proves nothing"
    assert nbytes == 64 * 4096


def test_every_bucket_carries_the_ancestors_of_its_own_members(tmp_path):
    # The invariant with no downstream symptom: ADD --chown skips directories a
    # member's path only implies, so they land root:root and php-fpm cannot
    # write its image cache. A bucket boundary inside a directory is the case
    # that breaks it, so the limit here is set to force several.
    src = build_archive(tmp_path / "media.tar",
                        [(f"cache/{i // 4}/{i % 4}/img-{i:03d}.jpg", 4096)
                         for i in range(64)])
    out = tmp_path / "out"
    out.mkdir()
    dm.demux([str(src)], str(out), "media", 48 * 1024, 8)

    for idx, members in read_buckets(out, 8).items():
        for name in members:
            parts = name.split("/")[:-1]
            for i in range(len(parts)):
                anc = "/".join(parts[:i + 1])
                assert anc in members, f"bucket-{idx:02d} holds {name} without {anc}"


def test_ancestors_are_carried_at_their_real_depth(tmp_path):
    # Regression: the ancestor stack holds the TarInfo objects themselves, so
    # stripping the archive root at flush time rather than on arrival strips an
    # entry again each time it is carried into a further bucket — turning
    # cache/2/1 into 2/1, which passes a "some directory was emitted" check.
    src = build_archive(tmp_path / "media.tar",
                        [(f"cache/{i // 4}/{i % 4}/img-{i:03d}.jpg", 4096)
                         for i in range(32)])
    out = tmp_path / "out"
    out.mkdir()
    dm.demux([str(src)], str(out), "media", 24 * 1024, 8)

    for members in read_buckets(out, 8).values():
        for name in members:
            assert not name.startswith("cache/") or name.count("/") <= 3
            assert name == "cache" or name.startswith("cache/"), \
                f"{name} lost its leading path element"


def test_a_split_archive_reads_as_one_stream(tmp_path):
    src = build_archive(tmp_path / "media.tar",
                        [(f"cache/img-{i:03d}.jpg", 4096) for i in range(32)])
    raw = src.read_bytes()
    cut = len(raw) // 3
    parts = []
    for i, start in enumerate(range(0, len(raw), cut)):
        p = tmp_path / f"media.tar.part-{i:02d}"
        p.write_bytes(raw[start:start + cut])
        parts.append(str(p))
    assert len(parts) > 1, "the archive must actually be split for this to test anything"

    out = tmp_path / "out"
    out.mkdir()
    n, _, _, _, _ = dm.demux(parts, str(out), "media", 1024 * 1024, 4)
    assert n == 33  # 32 files plus the cache directory


def test_unused_buckets_are_valid_non_empty_tars(tmp_path):
    # docker does not recognise a zero-member tar as an archive: ADD would copy
    # it in verbatim and leave a literal bucket-NN.tar inside the media tree.
    src = build_archive(tmp_path / "media.tar",
                        [(f"cache/img-{i:03d}.jpg", 1024) for i in range(4)])
    out = tmp_path / "out"
    out.mkdir()
    _, _, entries, used, _ = dm.demux([str(src)], str(out), "media", 1024 * 1024, 5)

    assert used == 1
    for idx, members in read_buckets(out, 5).items():
        assert members, f"bucket-{idx:02d} is empty"
        if idx:
            assert all(m.isdir() for m in members.values()), \
                "padding must be directories, which are safe to re-extract"


def test_a_tree_larger_than_the_buckets_fails_rather_than_dropping_files(tmp_path):
    src = build_archive(tmp_path / "media.tar",
                        [(f"cache/img-{i:03d}.jpg", 4096) for i in range(32)])
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SystemExit, match="outgrew 2 buckets"):
        dm.demux([str(src)], str(out), "media", 8 * 1024, 2)


def test_a_hard_link_is_refused(tmp_path):
    # Its target has to be in the same tar, and a bucket boundary can separate
    # the pair — with the bytes already behind us in a stream that does not
    # rewind. The resulting bucket extracts to a dangling link.
    src = tmp_path / "media.tar"
    with tarfile.open(src, "w") as tf:
        d = tarfile.TarInfo("media")
        d.type, d.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(d)
        ti = tarfile.TarInfo("media/real.jpg")
        ti.size, ti.mode = 8, 0o644
        tf.addfile(ti, io.BytesIO(b"x" * 8))
        ln = tarfile.TarInfo("media/link.jpg")
        ln.type, ln.linkname, ln.mode = tarfile.LNKTYPE, "media/real.jpg", 0o644
        tf.addfile(ln)

    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SystemExit, match="hard link"):
        dm.demux([str(src)], str(out), "media", 1024 * 1024, 3)


def test_a_symlink_is_kept_and_not_read_as_world_writable(tmp_path):
    # A symlink's own mode is conventionally 0777 and means nothing — the
    # target's mode governs — so a naive permission check condemns every
    # symlink in the tree.
    src = tmp_path / "media.tar"
    with tarfile.open(src, "w") as tf:
        d = tarfile.TarInfo("media")
        d.type, d.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(d)
        ti = tarfile.TarInfo("media/real.jpg")
        ti.size, ti.mode = 8, 0o644
        tf.addfile(ti, io.BytesIO(b"x" * 8))
        ln = tarfile.TarInfo("media/link.jpg")
        ln.type, ln.linkname, ln.mode = tarfile.SYMTYPE, "real.jpg", 0o777
        tf.addfile(ln)

    out = tmp_path / "out"
    out.mkdir()
    dm.demux([str(src)], str(out), "media", 1024 * 1024, 3)
    members = read_buckets(out, 3)[0]
    assert members["link.jpg"].issym()
    assert members["link.jpg"].linkname == "real.jpg"


def test_a_symlink_out_of_the_tree_is_refused(tmp_path):
    src = tmp_path / "media.tar"
    with tarfile.open(src, "w") as tf:
        d = tarfile.TarInfo("media")
        d.type, d.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(d)
        ln = tarfile.TarInfo("media/escape")
        ln.type, ln.linkname, ln.mode = tarfile.SYMTYPE, "../../etc/passwd", 0o777
        tf.addfile(ln)

    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SystemExit, match="escapes the tree"):
        dm.demux([str(src)], str(out), "media", 1024 * 1024, 3)


def test_a_setgid_directory_ships_with_its_mode_intact(tmp_path):
    # Magento's own permissions procedure sets 2775 across pub/media, so the
    # archive is full of these. On a directory the bit means group inheritance,
    # not privilege. The extracted-tree path never saw it — `tar x` chmods, and
    # the kernel drops setgid when the caller is not in the entry's group — so
    # this is the first time the real mode reaches a bucket.
    src = tmp_path / "media.tar"
    with tarfile.open(src, "w") as tf:
        for name, mode in (("media", 0o2775), ("media/catalog", 0o2775)):
            d = tarfile.TarInfo(name)
            d.type, d.mode, d.gid = tarfile.DIRTYPE, mode, 33
            tf.addfile(d)
        ti = tarfile.TarInfo("media/catalog/img.jpg")
        ti.size, ti.mode = 8, 0o644
        tf.addfile(ti, io.BytesIO(b"x" * 8))

    out = tmp_path / "out"
    out.mkdir()
    dm.demux([str(src)], str(out), "media", 1024 * 1024, 3)
    members = read_buckets(out, 3)[0]
    assert members["catalog"].mode == 0o2775
    assert members["catalog/img.jpg"].mode == 0o644


def test_a_setuid_file_is_refused(tmp_path):
    # The bit this check exists for: a privilege on an executable, which has no
    # business in an inert media tree.
    src = tmp_path / "media.tar"
    with tarfile.open(src, "w") as tf:
        d = tarfile.TarInfo("media")
        d.type, d.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(d)
        ti = tarfile.TarInfo("media/sneaky")
        ti.size, ti.mode = 8, 0o4755
        tf.addfile(ti, io.BytesIO(b"x" * 8))

    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SystemExit, match="setuid/setgid"):
        dm.demux([str(src)], str(out), "media", 1024 * 1024, 3)


def test_the_whole_archive_is_scanned_before_it_is_refused(tmp_path, capsys):
    # Reaching this point in CI costs twelve minutes of dataset download, so a
    # failing run reports everything wrong with the tree rather than the first
    # entries' worth — counted in full, with a few examples each.
    src = tmp_path / "media.tar"
    with tarfile.open(src, "w") as tf:
        d = tarfile.TarInfo("media")
        d.type, d.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(d)
        for i in range(20):
            ti = tarfile.TarInfo(f"media/loose-{i:02d}")
            ti.size, ti.mode = 8, 0o666
            tf.addfile(ti, io.BytesIO(b"x" * 8))
        ln = tarfile.TarInfo("media/escape")
        ln.type, ln.linkname, ln.mode = tarfile.SYMTYPE, "/etc/passwd", 0o777
        tf.addfile(ln)

    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SystemExit) as exc:
        dm.demux([str(src)], str(out), "media", 1024 * 1024, 3)
    # Both kinds, and the count covers entries past the tenth.
    assert "world-writable x20" in str(exc.value)
    assert "symlink escapes the tree x1" in str(exc.value)
    assert len(capsys.readouterr().err.strip().splitlines()) == 2


def test_a_world_writable_file_is_refused(tmp_path):
    src = tmp_path / "media.tar"
    with tarfile.open(src, "w") as tf:
        d = tarfile.TarInfo("media")
        d.type, d.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(d)
        ti = tarfile.TarInfo("media/loose.jpg")
        ti.size, ti.mode = 8, 0o666
        tf.addfile(ti, io.BytesIO(b"x" * 8))

    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SystemExit, match="world-writable"):
        dm.demux([str(src)], str(out), "media", 1024 * 1024, 3)


def test_an_empty_archive_is_refused(tmp_path):
    src = build_archive(tmp_path / "media.tar", [])
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SystemExit, match="no entries"):
        dm.demux([str(src)], str(out), "media", 1024 * 1024, 3)


def test_a_bucket_tar_stays_under_the_limit(tmp_path):
    # The limit is sized against dockerd's heap while the bucket is ADDed, so
    # it has to bound the FILE. Counting payload alone ignores a 512-byte header
    # per entry and the padding to a 512 boundary, which on a tree of millions
    # of small files is hundreds of megabytes a bucket — over the ceiling.
    limit = 64 * 1024
    src = build_archive(tmp_path / "media.tar",
                        [(f"cache/{i // 16}/img-{i:03d}.jpg", 700) for i in range(256)])
    out = tmp_path / "out"
    out.mkdir()
    _, _, _, used, _ = dm.demux([str(src)], str(out), "media", limit, 10)

    assert used > 1, "the limit must force a roll, or this proves nothing"
    for i in range(used):
        size = os.path.getsize(out / f"bucket-{i:02d}.tar")
        # tar closes with two zero blocks and pads to its 10240-byte record.
        assert size - 10240 <= limit, f"bucket-{i:02d} is {size} bytes, over {limit}"
