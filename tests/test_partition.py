import importlib.util
import os
import stat
from pathlib import Path
import pytest

# Loaded by path, not imported: the module ships INTO images via the `stagelib`
# build context, so it lives in a directory that is deliberately not a package.
_SRC = Path(__file__).resolve().parents[1] / "builder" / "stage-lib" / "partition-tree.py"
_spec = importlib.util.spec_from_file_location("partition_tree", _SRC)
pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pt)


def write(root: Path, rel: str, kb: int):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * (kb * 1024))


def test_small_tree_yields_children_not_root(tmp_path):
    # THE regression. A tree that fits in one bucket used to yield ROOT itself,
    # and relpath(ROOT, ROOT) == '.' makes shutil.move nest the whole tree as
    # bucket-00/<basename> — the image then gets /app/app/... and every request
    # 404s. Found by booting an image, not by any test; this is that test.
    write(tmp_path, "public/index.php", 4)
    write(tmp_path, "vendor/lib.php", 4)
    assignments, sizes = pt.plan(str(tmp_path), 1024 * 1024, 4)
    assert len(sizes) == 1
    assert {Path(p).name for p, _ in assignments} == {"public", "vendor"}
    assert str(tmp_path) not in {p for p, _ in assignments}


def test_descends_into_oversized_directory(tmp_path):
    for i in range(6):
        write(tmp_path, f"uploads/img{i}.jpg", 100)
    write(tmp_path, "app.php", 4)
    # 250K limit forces uploads/ (600K) to be split file by file.
    assignments, sizes = pt.plan(str(tmp_path), 250, 10)
    names = sorted(Path(p).name for p, _ in assignments)
    assert names == ["app.php"] + [f"img{i}.jpg" for i in range(6)]
    assert all(kb <= 250 for kb in sizes)
    assert len(sizes) >= 3


def test_du_batching_chunks_past_the_command_line_limit(tmp_path):
    # 84,148 paths on one `du` command line is E2BIG, and one `du` per path is
    # 84,148 subprocesses. Sizes must come back for every path either way, so
    # this crosses the 2000-path chunk boundary rather than testing under it.
    paths = []
    for i in range(2500):
        p = tmp_path / f"f{i}.bin"
        p.write_bytes(b"\0" * 1024)
        paths.append(str(p))
    sizes = pt.du_kb_many(paths)
    assert set(sizes) == set(paths)
    assert all(kb > 0 for kb in sizes.values())


def test_paths_with_spaces_survive_batching(tmp_path):
    # du's output is split on the FIRST tab, so a name containing spaces (or
    # anything but a tab) must round-trip intact.
    p = tmp_path / "an item 84143.jpg"
    p.write_bytes(b"\0" * 1024)
    assert list(pt.du_kb_many([str(p)])) == [str(p)]


def test_refuses_when_buckets_run_out(tmp_path):
    for i in range(4):
        write(tmp_path, f"d{i}/f.bin", 100)
    try:
        pt.plan(str(tmp_path), 150, 2)
    except SystemExit as e:
        assert "outgrew 2 buckets" in str(e)
    else:
        raise AssertionError("expected SystemExit when the tree outgrows the bucket count")


def test_refuses_a_single_file_over_the_limit(tmp_path):
    write(tmp_path, "huge.bin", 500)
    try:
        pt.plan(str(tmp_path), 100, 10)
    except SystemExit as e:
        assert "over the layer limit" in str(e)
    else:
        raise AssertionError("expected SystemExit for a file larger than one layer")


def test_recreated_parents_keep_the_source_mode(tmp_path):
    # THE gitlab regression. Only the piece that MOVES keeps its metadata; every
    # directory recreated on the way to it used to come back 0755 root-owned,
    # and buckets become COPY layers, so the image shipped a git-owned tree
    # under parents git could not write. nginx still bound its port and served
    # 502 over the dead upstream for 17 minutes before the healthcheck gave up.
    root, staging = tmp_path / "var", tmp_path / "staging"
    # Two leaves, so @hashed exceeds the limit and must be DESCENDED into --
    # a tree that fits is yielded whole and keeps its metadata for free, which
    # is why this bug survived: it only appears above the layer target.
    write(root, "git-data/repositories/@hashed/ab/f.bin", 100)
    write(root, "git-data/repositories/@hashed/cd/f.bin", 100)
    for d in ["git-data", "git-data/repositories", "git-data/repositories/@hashed"]:
        (root / d).chmod(0o2770)
    pt.main(["partition-tree.py", "150", "4", str(root), str(staging)])
    for d in ["git-data", "git-data/repositories", "git-data/repositories/@hashed"]:
        moded = stat.S_IMODE((staging / "bucket-00" / d).stat().st_mode)
        assert moded == 0o2770, f"{d} shipped as {oct(moded)}, not the source's 0o2770"


def test_recreated_parents_keep_the_source_owner(tmp_path, monkeypatch):
    # The owner half of the same regression. The suite does not run as root, so
    # the chown itself cannot be performed here — what is asserted is that each
    # recreated level is chowned to ITS OWN source's uid/gid, which is the part
    # a single `chown -R` after the fact cannot express for a tree split across
    # git, gitlab-psql and gitlab-redis.
    root, staging = tmp_path / "var", tmp_path / "staging"
    write(root, "git-data/repositories/@hashed/ab/f.bin", 100)
    write(root, "git-data/repositories/@hashed/cd/f.bin", 100)
    calls = []
    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        # Give each source level a distinct uid so a chown that mirrored the
        # WRONG level, or one blanket uid, cannot pass.
        fake_uid = {"git-data": 998, "repositories": 997, "@hashed": 996}.get(
            os.path.basename(str(path)))
        return _Stat(st, fake_uid) if fake_uid else st

    monkeypatch.setattr(pt.os, "stat", fake_stat)
    monkeypatch.setattr(pt.os, "chown", lambda p, u, g: calls.append((os.path.basename(p), u)))
    monkeypatch.setattr(pt.os, "chmod", lambda p, m: None)
    pt.main(["partition-tree.py", "150", "4", str(root), str(staging)])
    # Deduped: both leaves walk the same three parents, so each is chowned twice.
    assert list(dict.fromkeys(calls)) == [
        ("git-data", 998), ("repositories", 997), ("@hashed", 996)]


class _Stat:
    """os.stat_result with st_uid/st_gid overridden (the tuple is immutable)."""

    def __init__(self, st, uid):
        self._st, self.st_uid, self.st_gid = st, uid, uid

    def __getattr__(self, name):
        return getattr(self._st, name)


def test_move_places_every_piece_and_empties_the_root(tmp_path):
    root, staging = tmp_path / "app", tmp_path / "staging"
    for i in range(5):
        write(root, f"uploads/img{i}.jpg", 100)
    write(root, "public/index.php", 4)
    for i in range(4):
        (staging / f"bucket-0{i}").mkdir(parents=True)
    pt.main(["partition-tree.py", "250", "4", str(root), str(staging)])
    moved = sorted(p.relative_to(staging).parts[1:] for p in staging.rglob("*") if p.is_file())
    assert moved == sorted(
        [("uploads", f"img{i}.jpg") for i in range(5)] + [("public", "index.php")])
    # Every FILE is placed; what stays behind is the skeleton of a directory
    # that had to be split (uploads/), whose contents all moved. That is
    # harmless — the final image is built from the buckets, not from root — and
    # a directory that is legitimately empty is small enough to be yielded as a
    # whole piece, so it moves rather than being lost.
    assert [p for p in root.rglob("*") if p.is_file()] == []
    assert [p.name for p in root.rglob("*")] == ["uploads"]


# ---------------------------------------------------------------------------
# No image may carry its own copy of the partitioner.
#
# The same bug was fixed in two separate copies of this code on one afternoon,
# and the copy that stayed private went on to lose directory ownership through
# a third (gitlab, three CI runs). shopping and reddit kept inline copies until
# #53. Measured when they were converted: bucket assignment was byte-identical
# across 58 paths, and all 32 differences were recreated parent directories the
# inline copy created 0755 root-owned — including a 2770 setgid dir flattened
# to 755 and a 0750 flattened to 755. Their `chown -R` restores the owner but
# cannot restore a mode, so the duplication was shipping a real defect, not
# just repeated code.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent


def _partitioning_scripts():
    # BUCKET_COUNT= is set only by a stage that actually partitions. Matching
    # on "bucket-" alone also caught audit scripts that merely mention them.
    return [p for p in (REPO_ROOT / "images").glob("*/*/*.sh")
            if "BUCKET_COUNT=" in p.read_text()]


def test_some_image_actually_partitions():
    assert _partitioning_scripts(), "no partitioning restore stage was discovered"


@pytest.mark.parametrize(
    "script", _partitioning_scripts(),
    ids=lambda p: f"{p.parent.parent.name}-{p.parent.name}")
def test_no_image_carries_an_inline_partitioner(script):
    body = script.read_text()
    assert "/partition-tree.py" in body, (
        f"{script.relative_to(REPO_ROOT)} partitions into buckets but does not "
        f"call the shared builder/stage-lib/partition-tree.py. Add "
        f"`COPY --from=stagelib partition-tree.py /partition-tree.py` to the "
        f"image's restore stage and invoke it.")
    for marker in ("def partition(", "def children(", "MAX_BUCKETS ="):
        assert marker not in body, (
            f"{script.relative_to(REPO_ROOT)} still holds an inline copy of the "
            f"partitioner ({marker!r}). A private fork of this code has silently "
            f"lost directory ownership before.")
