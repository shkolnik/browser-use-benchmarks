import errno
import importlib.util
import os
import re
import stat
import subprocess
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
    assert {Path(p).name for p, _, _ in assignments} == {"public", "vendor"}
    assert str(tmp_path) not in {p for p, _, _ in assignments}


def test_descends_into_oversized_directory(tmp_path):
    for i in range(6):
        write(tmp_path, f"uploads/img{i}.jpg", 100)
    write(tmp_path, "app.php", 4)
    # 250K limit forces uploads/ (600K) to be split file by file.
    assignments, sizes = pt.plan(str(tmp_path), 250, 10)
    names = sorted(Path(p).name for p, _, _ in assignments)
    assert names == ["app.php"] + [f"img{i}.jpg" for i in range(6)]
    assert all(n <= 250 * 1024 for n in sizes)
    assert len(sizes) >= 3


def test_an_assignment_carries_the_size_the_progress_line_reports(tmp_path):
    """The move loop reports bytes moved against the tree's total, and both
    numbers come from here: the per-piece size in each assignment and the sum of
    the bucket sizes. If they were measured differently the line would drift —
    finishing at 31G of 30G, or stalling at 90% — so this pins them to each
    other and to what measure() says the pieces cost.
    """
    for i in range(6):
        write(tmp_path, f"uploads/img{i}.jpg", 100)
    write(tmp_path, "app.php", 4)
    assignments, sizes = pt.plan(str(tmp_path), 250, 10)
    measured = pt.measure(str(tmp_path))
    for piece, _, nbytes in assignments:
        assert nbytes == measured[piece]
    assert sum(n for _, _, n in assignments) == sum(sizes)


def test_a_bucket_is_measured_as_the_tar_it_becomes(tmp_path):
    # The bound that matters is what the COPY layer writes, and a layer is a
    # tar. Three regimes where `du -sk` gets it wrong in different directions:
    # many tiny files (du over by 4K each), block-aligned files (du under by
    # the 512-byte header each), and a sparse file (du under by all of it).
    tree = tmp_path / "tree"
    (tree / "many").mkdir(parents=True)
    for i in range(200):
        (tree / "many" / f"tiny-{i}").write_bytes(b"x" * 100)
    for i in range(20):
        (tree / "many" / f"aligned-{i}").write_bytes(b"x" * 4096)
    with open(tree / "sparse.bin", "wb") as f:
        f.truncate(8 * 1024 * 1024)

    measured = pt.measure(str(tree))[str(tree)]
    tarball = tmp_path / "t.tar"
    subprocess.check_call(["tar", "cf", str(tarball), "-C", str(tmp_path), "tree"])
    # tar pads the archive out with zero blocks at the end; the members are what
    # this measures, so compare below the real size and within one padding run.
    actual = tarball.stat().st_size
    assert measured <= actual, "measuring under the layer is what loses a build"
    assert actual - measured <= 10240


def test_a_name_too_long_for_a_tar_header_is_charged_for(tmp_path):
    # Over 100 bytes, tar emits a ././@LongLink header plus the padded name
    # ahead of the entry. classifieds' per-item paths run past that.
    short, long = tmp_path / "s", tmp_path / "l"
    short.mkdir(), long.mkdir()
    (short / "f").write_bytes(b"x" * 512)
    (long / ("d" * 150)).write_bytes(b"x" * 512)
    assert pt.measure(str(long))[str(long)] > pt.measure(str(short))[str(short)]


def test_a_name_with_spaces_is_measured(tmp_path):
    # Sizes used to come back through `du`'s tab-separated output, so a name
    # carrying spaces had to round-trip a parse. It is an lstat now, but the
    # tree that motivated it (84,148 item directories) is still out there.
    p = tmp_path / "an item 84143.jpg"
    p.write_bytes(b"\0" * 1024)
    assert pt.measure(str(tmp_path))[str(p)] == pt.entry_blocks(1024)


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
        assert "over the 100K layer limit" in str(e)
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

def _partition_root(script):
    """The absolute path the script hands partition-tree.py, $VARS resolved."""
    arg = re.search(r'/partition-tree\.py\s+\S+\s+\S+\s+"?(\S+?)"?\s',
                    script.read_text()).group(1)
    name = re.fullmatch(r"\$\{?(\w+)\}?", arg)
    if not name:
        return arg
    return re.search(rf'^{name.group(1)}=(\S+)', script.read_text(), re.M).group(1)


@pytest.mark.parametrize(
    "script", _partitioning_scripts(),
    ids=lambda p: f"{p.parent.parent.name}-{p.parent.name}")
def test_no_bind_mount_lands_inside_the_partitioned_tree(script):
    # A dataset archive is bind-mounted so it is read without being copied into
    # a layer. The cost of that is that the mount point cannot be unlinked —
    # `rm -f` on it returns EBUSY — so an archive mounted inside the tree the
    # stage partitions is still there when the partitioner walks it, and gets
    # charged as a member of the layer it is meant to stay out of.
    #
    # gitlab did exactly this: a 19G backup tar mounted at
    # /var/opt/gitlab/backups/ failed the partition with "single file ... is
    # 19491590K as a tar member, over the 8388608K layer limit", after a
    # 41-minute restore. Mount outside the tree and symlink in: a symlink is
    # removable, and the partitioner charges it 512 bytes rather than its
    # target.
    root = _partition_root(script)
    targets = re.findall(r"--mount=type=bind[^\s]*?,target=(\S+?)(?:\s|\\|$)",
                         (script.parent / "Dockerfile").read_text())
    assert targets, f"{script.parent.name} bind-mounts nothing; check this regex"
    inside = [t for t in targets
              if t == root or t.startswith(root.rstrip("/") + "/")]
    assert not inside, (
        f"{script.parent.name} bind-mounts {inside} inside {root}, the tree "
        f"{script.name} partitions. A mount point cannot be removed before the "
        f"partition, so it is charged against a bucket. Mount it outside the "
        f"tree and symlink it in, deleting the link before partitioning.")


def _exdev(*_a, **_kw):
    raise OSError(errno.EXDEV, "Invalid cross-device link")


def test_exdev_move_keeps_every_moved_entry_s_owner(tmp_path, monkeypatch):
    # The half of the ownership bug that mkdir_mirroring does NOT cover. The
    # pieces that MOVE were assumed to keep their metadata for free, and they do
    # -- but only when os.rename works. Renaming a directory whose contents live
    # in a lower overlayfs layer raises EXDEV, and shutil.move's fallback is
    # copytree, which copies mode and times and NOT ownership. So the loss was
    # never about which directory: it was about which code path.
    #
    # Measured: gitlab's /var/opt/gitlab/prometheus is gitlab-prometheus-owned in
    # the base image, took the copy path, and reached the shipped image
    # root:root -- so runit's prometheus (-U gitlab-prometheus) crash-looped on
    # its TSDB. git-data, written by the restore into the upper layer, renamed
    # and kept git:git, which is what made this look like a one-directory
    # problem for three builds.
    root, staging = tmp_path / "var", tmp_path / "staging"
    write(root, "prometheus/data/chunk.bin", 100)
    write(root, "prometheus/queries.active", 4)
    calls = []
    real_lstat = os.lstat

    def fake_lstat(path, *a, **kw):
        st = real_lstat(path, *a, **kw)
        # A distinct uid per entry, so a blanket chown -- or one that mirrored
        # the wrong level -- cannot pass.
        uid = {"prometheus": 995, "data": 994, "chunk.bin": 993,
               "queries.active": 992}.get(os.path.basename(str(path)))
        return _Stat(st, uid) if uid else st

    monkeypatch.setattr(pt.os, "rename", _exdev)
    monkeypatch.setattr(pt.os, "lstat", fake_lstat)
    monkeypatch.setattr(
        pt.os, "chown",
        lambda p, u, g, **kw: calls.append((os.path.basename(p), u)))
    monkeypatch.setattr(pt.os, "chmod", lambda p, m, **kw: None)
    monkeypatch.setattr(pt.os, "utime", lambda p, *a, **kw: None)
    pt.main(["partition-tree.py", "500", "4", str(root), str(staging)])
    assert sorted(calls) == [
        ("chunk.bin", 993), ("data", 994), ("prometheus", 995),
        ("queries.active", 992)]


def test_exdev_move_still_moves_the_content(tmp_path, monkeypatch):
    # The copy path must be a real move: same bytes, same modes, source gone.
    # Without this, a chown-only assertion would pass against a copier that
    # silently dropped files.
    root, staging = tmp_path / "var", tmp_path / "staging"
    write(root, "prometheus/data/chunk.bin", 100)
    (root / "prometheus/data").chmod(0o2750)
    monkeypatch.setattr(pt.os, "rename", _exdev)
    pt.main(["partition-tree.py", "500", "4", str(root), str(staging)])
    moved = staging / "bucket-00" / "prometheus" / "data" / "chunk.bin"
    assert moved.is_file() and moved.stat().st_size == 100 * 1024
    assert stat.S_IMODE((staging / "bucket-00" / "prometheus" / "data").stat().st_mode) == 0o2750
    assert not (root / "prometheus").exists()


@pytest.mark.parametrize(
    "script", _partitioning_scripts(),
    ids=lambda p: f"{p.parent.parent.name}-{p.parent.name}")
def test_bucket_count_matches_the_dockerfile_copy_lines(script):
    # Every bucketed image restates "must match the COPY --from lines in the
    # Dockerfile's final stage" in a comment, and nothing enforced it. The two
    # halves live in different files and are edited by hand, which is precisely
    # the shape of invariant that rots.
    #
    # Both directions are real failures, and they fail very differently:
    #
    #   too few COPY lines  — restore-stage.sh mkdirs bucket-00..N-1 and the
    #     partitioner first-fits across all of them, so a filled bucket with no
    #     COPY is simply left behind. The build SUCCEEDS and ships an image
    #     missing an arbitrary slice of the dataset. Silent.
    #   too many COPY lines — the extra COPY names a directory that was never
    #     created and buildkit fails the build. Loud, but a wasted multi-hour
    #     job on the fleet.
    count = int(re.search(r"^BUCKET_COUNT=(\d+)", script.read_text(),
                          re.M).group(1))
    dockerfile = script.parent / "Dockerfile"
    copied = [int(m) for m in re.findall(r"/staging/bucket-(\d+)/",
                                         dockerfile.read_text())]
    assert copied == list(range(count)), (
        f"{dockerfile.relative_to(REPO_ROOT)} COPYs buckets {copied}, but "
        f"{script.name} sets BUCKET_COUNT={count}. The final stage needs "
        f"exactly one contiguous COPY --from line per bucket, 00..{count - 1}.")


def test_both_long_phases_report_progress(tmp_path, monkeypatch, capsys):
    """The measuring walk and the move loop were both silent, and on gitlab's
    30G both are minutes long — a silent build stage is indistinguishable from
    a hung one. Driven with the interval at zero because at its real 30s a test
    tree would never reach the first tick.
    """
    monkeypatch.setattr(pt.Ticker, "INTERVAL", 0)
    root, staging = tmp_path / "root", tmp_path / "staging"
    for name in ("a/one.bin", "b/two.bin"):
        write(root, name, 40)
    staging.mkdir()
    pt.main(["partition-tree.py", "900", "4", str(root), str(staging)])
    out = capsys.readouterr().out
    assert "partition: measured " in out, "the measuring walk reported nothing"
    assert "partition: moved " in out, "the move loop reported nothing"
    # The move line is in bytes against the tree total: a piece is a whole
    # subtree, so a count of pieces would say nothing about what is left.
    assert re.search(r"partition: moved \d+\.\dG of \d+\.\dG after \d+s", out)
