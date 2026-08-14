"""map-nominatim's project data on the media path.

The archive is unlike every earlier adoption in one way that decides three of
its fields: once the `osm_dump/` prefix is stripped it is TWO FILES AND NO
DIRECTORY. shopping, reddit and classifieds all strip (or decline to strip)
their way to a top level of directories, which is what demux-media.py pads an
unused bucket with. Here that list is empty, so an unused bucket would be a tar
with no members — which docker does not recognise as an archive, so `ADD` would
copy it verbatim and drop a literal bucket-NN.tar into /nominatim/data.

The image avoids that by sizing the ceiling to the archive rather than padding
around it: one bucket, so there is no unused bucket. These tests hold that
reasoning in place, and hold the named-file check that moved out of the restore
stage when the tree stopped entering it.
"""

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

from builder.manifest import load_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE = REPO_ROOT / "images" / "webarena" / "map-nominatim"

_spec = importlib.util.spec_from_file_location(
    "demux_media", REPO_ROOT / "builder" / "stage-lib" / "demux-media.py")
dm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dm)

# The two members, at the sizes image.toml pins for them.
#
# Only the bucket arithmetic uses these numbers, and it uses them as numbers.
# The tests that run the demux for real build the same archive SHAPE at a few
# kilobytes a member: the demux copies every payload byte into the bucket tar,
# so an archive at the real sizes would write 1.75 GiB per test to prove
# something about names and prefixes that a kilobyte proves just as well.
PBF = "us-northeast-latest.osm.pbf"
IMPORTANCE = "wikimedia-importance.sql.gz"
REAL_SIZES = {PBF: 1_485_107_682, IMPORTANCE: 393_574_858}
TOKEN_SIZES = {PBF: 4096, IMPORTANCE: 1024}


def _media():
    m = load_manifest(IMAGE).media
    assert m is not None, "map-nominatim no longer declares [media]"
    return m


def _archive(path, sizes, root="osm_dump"):
    """The archive's shape: a single root directory holding two flat files."""
    with tarfile.open(path, "w") as tf:
        d = tarfile.TarInfo(root)
        d.type, d.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(d)
        for name, size in sizes.items():
            ti = tarfile.TarInfo(f"{root}/{name}")
            ti.size, ti.mode = size, 0o644
            tf.addfile(ti, io.BytesIO(b"x" * size))
    return path


def test_the_whole_archive_lands_in_one_bucket():
    """limit_kb against what the two files cost, in the demux's own accounting.

    entry_blocks, not the byte counts: the ceiling is measured in what the
    bucket tar will hold, so it has to clear the payloads rounded up to 512-byte
    blocks plus a header each. Getting this wrong does not produce a smaller
    layer — demux only moves forward, so it would step past max_buckets and fail
    the build with `tree outgrew 1 buckets`.
    """
    m = _media()
    cost = sum(dm.entry_blocks(s) for s in REAL_SIZES.values())
    assert m.max_buckets == 1
    assert cost <= m.limit_kb * 1024, (
        f"the archive costs {cost} bytes as tar members, over the "
        f"{m.limit_kb * 1024} limit_kb allows in its single bucket")


def test_the_members_land_at_the_paths_the_env_vars_name(tmp_path):
    """strip and dest together, read the way the image reads them.

    ENV PBF_PATH is dest + the member's own name, and the member's own name is
    what survives the strip. Upstream's layout puts these under a further
    osm_dump/ and its PBF_PATH does not resolve; the test is that ours does.
    """
    m = _media()
    out = tmp_path / "out"
    out.mkdir()
    src = _archive(tmp_path / "osm_dump.tar", TOKEN_SIZES, root=m.strip)
    dm.demux([str(src)], str(out), m.strip, m.limit_kb * 1024, m.max_buckets)
    with tarfile.open(out / "bucket-00.tar") as tf:
        names = sorted(x.name for x in tf)
    assert names == sorted(TOKEN_SIZES)
    df = (IMAGE / "Dockerfile").read_text()
    for name in REAL_SIZES:
        assert f"={m.dest}/{name}\n" in df, (
            f"no ENV points at {m.dest}/{name}, where the ADD puts it")


def test_no_bucket_is_left_unpadded(tmp_path):
    """Why max_buckets is 1, stated as the failure it avoids.

    Padding comes from the archive's top-level DIRECTORIES. Stripping osm_dump/
    leaves none, so a spare bucket has nothing to pad with — and a zero-member
    tar is a file ADD treats as a file. The demux refuses rather than emitting
    one, so raising this ceiling fails on the host instead of in a built image.
    """
    m = _media()
    out = tmp_path / "out"
    out.mkdir()
    src = _archive(tmp_path / "osm_dump.tar", TOKEN_SIZES, root=m.strip)
    with pytest.raises(SystemExit) as e:
        dm.demux([str(src)], str(out), m.strip, m.limit_kb * 1024, 2)
    assert "no top-level directory to pad" in str(e.value)
    assert m.max_buckets == 1, (
        "this archive has no top-level directory to pad an unused bucket with")


def test_min_entries_is_the_count_after_the_strip(tmp_path):
    """The floor is two, and two is all it can be.

    The stripped root has no member of its own — demux drops it, because the
    destination directory already exists in the image — so the archive presents
    exactly the two files. A floor above that would fail the build on a correct
    archive; this pins it to what the demux actually counts.
    """
    m = _media()
    out = tmp_path / "out"
    out.mkdir()
    src = _archive(tmp_path / "osm_dump.tar", TOKEN_SIZES, root=m.strip)
    n, *_ = dm.demux([str(src)], str(out), m.strip, m.limit_kb * 1024, m.max_buckets)
    assert m.min_entries == n == 2


def test_the_named_file_check_moved_into_the_final_stage():
    """restore-stage.sh checked both files by name. The tree no longer reaches
    it, and min_entries cannot replace that half of the check — two entries is
    two entries whatever they are called. The assertion has to exist somewhere
    after the ADD, and it has to name the settings rather than a second copy of
    the filenames, since making PBF_PATH resolve is why the tree ships at all.
    """
    restore = (IMAGE / "restore-stage.sh").read_text()
    assert "osm_dump" not in restore.replace("osm_dump.tar", ""), (
        "restore-stage.sh still handles the project data")
    df = (IMAGE / "Dockerfile").read_text()
    body = df[df.index("ADD "):]
    for var in ("$PBF_PATH", "$IMPORT_WIKIPEDIA"):
        assert var in body, f"nothing after the ADD asserts {var} resolves"


def test_the_cluster_is_not_on_the_media_path():
    """nominatim_volumes.tar is the counter-example the media path must keep
    refusing: the restore stage starts the cluster it extracted, replays the
    snapshot's WAL and stops it cleanly, so what ships is not what was
    extracted. Routing it through [media] would ship the crashed snapshot and
    move the recovery onto every boot of every pulled image.
    """
    m = _media()
    assert m.archive == "osm_dump.tar"
    restore = (IMAGE / "restore-stage.sh").read_text()
    assert "service postgresql start" in restore
    df = (IMAGE / "Dockerfile").read_text()
    assert "COPY --from=datasets nominatim_volumes.tar" in df
