"""map-tile's half of the media path: the cluster never enters a build stage.

The fleet-wide media tests in test_bucket_media.py cover what every media image
shares — an ADD per bucket, a declared entry floor, a --chown that matches the
declaration. What is specific here is that this image moves its WHOLE dataset
and that the dataset is a Postgres cluster: the wipe of the base image's own
cluster has to happen before the ADDs land on it, the ownership the archive
carries has to survive them, and the four checks the restore stage used to make
have nowhere left to run but the final stage. Those are the invariants below.

Read with tomllib rather than load_manifest on purpose: these assertions are
about the file's own contents, and they should still report on an image.toml
that the loader refuses.

The numbers are measurements of osm_tile_server.tar itself, taken by walking its
header chain over ranged GETs (no bulk download) and accounting each member the
way demux-media.py does — a 512-byte header plus the payload rounded up to a
block. They are stable because the archive is sha256-pinned.
"""

import re
import tomllib
from pathlib import Path

IMG = Path(__file__).resolve().parents[1] / "images" / "webarena" / "map-tile"
DOCKERFILE = (IMG / "Dockerfile").read_text()
MANIFEST = tomllib.loads((IMG / "image.toml").read_text())
MEDIA = MANIFEST["media"]

# Entries the demux emits: 1,624 in the archive, less the two that make up the
# stripped root and therefore have no member of their own.
MEASURED_ENTRIES = 1622
# The prefix the extraction this replaces dropped with --strip-components=6.
EXTRACT_PREFIX = "projects/ogma3/docker/volumes/osm-data/_data"
EXTRACT_DEST = "/data/database"
# GHCR's comfortable per-layer ceiling; a bucket is a layer.
GHCR_LAYER_CEILING_KB = 10 * 1024 * 1024


def lines():
    return DOCKERFILE.splitlines()


def add_lines():
    return [l for l in lines() if l.startswith("ADD") and ".media/bucket-" in l]


def index_of(pred):
    return next(i for i, l in enumerate(lines()) if pred(l))


# --- the media lands where the extraction used to put it -----------------------

def test_strip_and_dest_reproduce_the_extraction():
    """`tar -C /data/database --strip-components=6` and this pair are the same
    statement about the same archive. strip is the prefix, not a count —
    run_media_prep derives the count from the segments — so a 6-component prefix
    is what proves the count did not move."""
    assert MEDIA["strip"] == EXTRACT_PREFIX
    assert len(MEDIA["strip"].split("/")) == 6
    assert MEDIA["dest"] == EXTRACT_DEST


def test_the_tile_cache_is_not_a_media_target():
    """/data/tiles is the rendered-tile cache and the archive carries no
    osm-tiles volume. It ships as an empty renderer-owned directory; an ADD
    aimed there would be an invented tree."""
    assert not any("/data/tiles" in l for l in add_lines())
    assert re.search(r"install -d -o renderer -g renderer .*/data/tiles", DOCKERFILE)


def test_bucket_indices_are_contiguous_from_zero():
    """demux writes bucket-00..bucket-(max_buckets-1). A gap in the ADDs would
    fail the build on a missing file, or — worse — leave a bucket's worth of the
    cluster out of an image that still builds."""
    got = [int(re.search(r"bucket-(\d+)\.tar", l).group(1)) for l in add_lines()]
    assert got == list(range(MEDIA["max_buckets"]))


def test_one_add_per_declared_bucket():
    """Asserted here as well as fleet-wide, because this image's ADD count is
    the ceiling rather than the 6 buckets the measured data fills: the spares
    are padded tars and must still be ADDed."""
    assert len(add_lines()) == MEDIA["max_buckets"] == 8


# --- the ownership split survives the ADDs -------------------------------------

def test_no_add_carries_a_chown():
    """An empty [media].chown means no flag at all, which is what makes ADD
    reproduce the archive's uid/gid and mode. /data is split between the
    postgres-owned cluster and the renderer-owned volume root, and any --chown
    would flatten it into one owner — leaving Postgres unable to read its own
    cluster."""
    assert MEDIA["chown"] == ""
    for l in add_lines():
        assert "--chown" not in l, "a --chown here overrides the archive's ownership"


def test_ownership_is_asserted_after_the_media_lands():
    """The only check on the absent --chown. Nothing else in the build would
    notice one being introduced, or an extractor resolving uid 101 by name
    against a passwd database where it is not postgres."""
    assert "postgres:postgres" in DOCKERFILE
    assert re.search(r"stat -c %U:%G /data/database/postgres", DOCKERFILE)


# --- the wipe still runs, and still runs first ---------------------------------

def test_the_wipe_precedes_the_first_add():
    """The base ships its own initdb'd cluster at /data/database/postgres.
    ADDing onto it merges two unrelated clusters — only the colliding names are
    replaced, and base-only WAL segments with a different system identifier
    survive — so the wipe is not optional, and after the ADDs it would delete
    the data instead."""
    wipe = index_of(lambda l: l.startswith("RUN rm -rf /data/database"))
    first_add = index_of(lambda l: l in add_lines())
    assert wipe < first_add
    assert "install -d -o renderer -g renderer /data/database" in DOCKERFILE


# --- the four validations moved rather than vanished ---------------------------

def test_every_validation_runs_after_the_last_add():
    """In the final stage, past the last ADD. Anywhere earlier and each one
    inspects a directory the buckets have not been extracted into yet — checks
    that pass by looking at nothing."""
    src = lines()
    last_add = max(i for i, l in enumerate(src) if l in add_lines())
    tail = "\n".join(src[last_add + 1:])
    for needle, what in [
        ("PG_VERSION", "the PG-major assert"),
        ("planet-import-complete", "the import-marker check"),
        ("stat -c %U:%G", "the cluster-ownership assert"),
        ("du -sk /data/database", "the size floor"),
    ]:
        assert needle in tail, f"{what} is not after the ADDs"


def test_the_pg_major_assert_compares_against_the_image():
    """A cluster only starts under its own major. The comparison is with what
    the base image ships, not with a literal 15, so a base bump fails the build
    rather than shipping a database that refuses to start."""
    assert "/usr/lib/postgresql/" in DOCKERFILE
    assert re.search(r'\[ "\$DATA_PG" = "\$IMAGE_PG" \]', DOCKERFILE)


def test_the_size_floor_is_still_ten_gibibytes():
    """A truncated import would otherwise build clean and serve blank tiles."""
    assert "10485760" in DOCKERFILE


# --- the host-side floor -------------------------------------------------------

def test_min_entries_is_a_floor_under_the_measurement():
    """Declared, so a truncated archive is refused before any of it is a layer,
    and under what was measured, so the WAL segment count a re-pinned capture
    would legitimately move is not a build failure."""
    assert 0 < MEDIA["min_entries"] <= MEASURED_ENTRIES
    # Far enough under to absorb that movement, far enough over that an empty or
    # wrong archive cannot clear it: the cluster is ~1.6k entries, not a handful.
    assert MEDIA["min_entries"] >= MEASURED_ENTRIES * 0.8


def test_the_buckets_fit_the_registry_and_the_data():
    """limit_kb is a per-layer ceiling. 8 GiB holds the measured 38.445 GiB in 6
    buckets (largest 7.997 GiB) and stays inside GHCR's ~10G comfort zone."""
    assert MEDIA["limit_kb"] == 8 * 1024 * 1024
    assert MEDIA["limit_kb"] < GHCR_LAYER_CEILING_KB
    measured_kb = 38.445 * 1024 * 1024
    assert MEDIA["limit_kb"] * MEDIA["max_buckets"] > measured_kb, (
        "the ceiling cannot hold the archive — the demux would refuse it")


def test_the_archive_is_the_pinned_dataset():
    """No [prepare] here: the cluster ships as captured, so the media archive is
    the upstream object and its sha256 is verified at download."""
    assert "prepare" not in MANIFEST
    assert MEDIA["archive"] in [d["filename"] for d in MANIFEST["datasets"]]


# --- nothing stages the tree anymore -------------------------------------------

def test_there_is_no_restore_stage():
    """The whole dataset is media, so extracting it into a stage would write
    38.4 GiB for a COPY to lift straight back out."""
    froms = [l for l in lines() if l.startswith("FROM")]
    assert len(froms) == 1, "a second stage is a second copy of the cluster"
    assert " AS restore" not in DOCKERFILE
    body = "\n".join(l for l in lines() if not l.lstrip().startswith("#"))
    assert "--from=restore" not in body
    assert "--from=stagelib" not in body, "nothing partitions a tree here anymore"
    assert "osm_tile_server.tar" not in body, (
        "the archive is demuxed on the host; a COPY of it would put 38.4 GiB "
        "back into a build stage")


def test_the_restore_script_is_gone():
    """It extracted, validated and partitioned. The first and last are the media
    path's now, the middle one moved into the final stage, and a script left
    behind would be read as something the build still runs."""
    assert not (IMG / "restore-stage.sh").exists()


def test_restore_needs_media_is_false():
    """Stated explicitly: there is no stage that could read it."""
    assert MEDIA["restore_needs_media"] is False
