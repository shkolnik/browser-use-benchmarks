import importlib.util
from pathlib import Path

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
