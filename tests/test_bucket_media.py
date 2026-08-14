import importlib.util
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

# Loaded by path for the same reason test_partition.py does it: stage-lib is
# shipped into images via a build context and is deliberately not a package.
_SRC = Path(__file__).resolve().parents[1] / "builder" / "stage-lib" / "bucket-media.py"
_spec = importlib.util.spec_from_file_location("bucket_media", _SRC)
bm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bm)


def write(root: Path, rel: str, kb: int = 1):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * (kb * 1024))
    return p


def members_of(tar: Path):
    with tarfile.open(tar) as tf:
        return sorted(m.name.rstrip("/") for m in tf.getmembers())


def run(tmp_path, limit_kb=64, max_buckets=8):
    out = tmp_path / "out"
    bm.main(["bucket-media.py", str(limit_kb), str(max_buckets),
             str(tmp_path / "root"), str(out)])
    return sorted(out.glob("bucket-*.tar"))


# --- the finding this module exists to prevent -------------------------------

def test_every_ancestor_directory_is_an_explicit_member(tmp_path):
    """THE regression, and it is not hypothetical.

    `ADD --chown` applies to tar MEMBERS. A directory that exists only
    implicitly in a member's path is created by the extractor and is not
    chowned, so a naive `tar cf b.tar a/b/leaf` ships leaf as app:app and both
    'a' and 'a/b' as root:root 0755. Verified against docker 29.6.1.

    That is invisible to any assertion that samples files, which is exactly what
    the plan originally proposed as the replacement for the ownership audit.
    """
    write(tmp_path / "root", "catalog/product/cache/deep/img.jpg", 8)
    tars = run(tmp_path)
    names = set(members_of(tars[0]))
    for anc in ["catalog", "catalog/product", "catalog/product/cache",
                "catalog/product/cache/deep"]:
        assert anc in names, f"{anc} missing — it would extract as root:root"


def test_ancestors_helper():
    assert bm.ancestors("a/b/c") == ["a", "a/b"]
    assert bm.ancestors("top") == []


# --- partition correctness ---------------------------------------------------

def test_every_entry_lands_in_exactly_one_bucket(tmp_path):
    root = tmp_path / "root"
    for i in range(12):
        write(root, f"dir{i:02d}/file.bin", 16)
    tars = run(tmp_path)
    assert len(tars) > 1, "test needs a multi-bucket split to be meaningful"

    seen = []
    for t in tars:
        seen.extend(members_of(t))
    files = [m for m in seen if m.endswith("file.bin")]
    assert len(files) == 12
    assert len(set(files)) == 12, "an entry was duplicated across buckets"


def test_source_tree_is_left_intact(tmp_path):
    """Unlike partition-tree.py, this copies. restore may still bind-mount it."""
    root = tmp_path / "root"
    write(root, "a/img.jpg", 8)
    run(tmp_path)
    assert (root / "a/img.jpg").exists()


def test_shared_parent_is_not_swept_into_one_bucket(tmp_path):
    """--no-recursion is load-bearing: siblings under one parent split apart."""
    root = tmp_path / "root"
    for i in range(6):
        write(root, f"shared/item{i}.bin", 24)
    tars = run(tmp_path, limit_kb=64)
    assert len(tars) > 1
    per_tar = [[m for m in members_of(t) if m.endswith(".bin")] for t in tars]
    assert all(len(x) < 6 for x in per_tar), "one bucket swallowed the whole parent"
    assert sum(len(x) for x in per_tar) == 6


def test_mtimes_are_preserved_not_pinned(tmp_path):
    """Pinning would break the old-path/new-path equivalence check."""
    root = tmp_path / "root"
    p = write(root, "a/img.jpg", 8)
    os.utime(p, (1234567890, 1234567890))
    tars = run(tmp_path)
    with tarfile.open(tars[0]) as tf:
        assert tf.getmember("a/img.jpg").mtime == 1234567890


# --- the audit must actually fail ---------------------------------------------

def test_audit_rejects_a_missing_entry(tmp_path):
    """A check that has never failed has not been tested."""
    root = tmp_path / "root"
    write(root, "a/img.jpg", 8)
    with pytest.raises(SystemExit, match="in no bucket"):
        bm.audit(str(root), ["a"])


def test_audit_rejects_an_implicit_ancestor(tmp_path):
    root = tmp_path / "root"
    write(root, "a/img.jpg", 8)
    with pytest.raises(SystemExit, match="not an explicit member"):
        bm.audit(str(root), ["a/img.jpg"])


def test_audit_rejects_setgid(tmp_path):
    root = tmp_path / "root"
    d = root / "a"
    write(root, "a/img.jpg", 8)
    os.chmod(d, 0o2775)
    with pytest.raises(SystemExit, match="setuid/setgid"):
        bm.audit(str(root), ["a", "a/img.jpg"])


def test_audit_rejects_world_writable(tmp_path):
    root = tmp_path / "root"
    p = write(root, "a/img.jpg", 8)
    os.chmod(p, 0o666)
    with pytest.raises(SystemExit, match="world-writable"):
        bm.audit(str(root), ["a", "a/img.jpg"])


def test_audit_rejects_escaping_symlink(tmp_path):
    root = tmp_path / "root"
    write(root, "a/img.jpg", 8)
    (root / "a" / "escape").symlink_to(tmp_path)
    with pytest.raises(SystemExit, match="escapes the tree"):
        bm.audit(str(root), ["a", "a/img.jpg", "a/escape"])


def test_audit_accepts_an_internal_symlink(tmp_path):
    root = tmp_path / "root"
    write(root, "a/img.jpg", 8)
    (root / "a" / "alias").symlink_to(root / "a" / "img.jpg")
    bm.audit(str(root), ["a", "a/img.jpg", "a/alias"])


# --- end to end, against a real extractor -------------------------------------

@pytest.mark.skipif(not os.environ.get("DOCKER_E2E"),
                    reason="set DOCKER_E2E=1 to run the docker round-trip")
def test_add_chown_covers_directories_end_to_end(tmp_path):
    """The property the unit tests approximate, asserted against real buildkit."""
    root = tmp_path / "root"
    write(root, "catalog/product/cache/img.jpg", 8)
    tars = run(tmp_path)
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "bucket.tar").write_bytes(tars[0].read_bytes())
    (ctx / "Dockerfile").write_text(
        "FROM debian:trixie-slim\n"
        "RUN groupadd -g 3000 app && useradd -u 3000 -g 3000 -M app\n"
        "ADD --chown=app:app bucket.tar /dest/\n"
        "RUN find /dest ! -user app -print | tee /tmp/bad; [ ! -s /tmp/bad ]\n")
    subprocess.run(["docker", "build", "--no-cache", "-q", "."],
                   cwd=ctx, check=True)


def test_unused_buckets_are_valid_nonempty_tars(tmp_path):
    """An empty tar is not recognised as an archive — ADD copies it verbatim.

    Found end-to-end: with max_buckets above what the tree needed, five literal
    bucket-NN.tar files appeared inside the media destination. max_buckets is a
    ceiling with headroom by design, and the Dockerfile's ADD lines are static,
    so unused buckets must still extract to nothing rather than to a file.
    """
    root = tmp_path / "root"
    write(root, "a/img.jpg", 8)
    tars = run(tmp_path, limit_kb=1024, max_buckets=4)
    assert len(tars) == 4, "every index up to the ceiling needs a tar"
    for t in tars:
        members = members_of(t)
        assert members, f"{t.name} is empty — docker would ADD it as a file"
    # the padded ones carry only directories, so nothing is duplicated
    files = [m for t in tars for m in members_of(t) if m.endswith(".jpg")]
    assert files == ["a/img.jpg"]
