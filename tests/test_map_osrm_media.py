"""map-osrm's half of the media path: the routing data never enters a stage.

The fleet-wide media tests in test_bucket_media.py cover what every media image
shares — an ADD per bucket, a declared entry floor. What is specific here is
that this image moves its WHOLE dataset, so the structural check that used to
run in the restore stage has nowhere to run but the final one, and there is no
restore stage left. Those are the invariants below.

Read with tomllib rather than load_manifest on purpose: these assertions are
about the file's own contents, and they should still report on an image.toml
that the loader refuses.
"""

import re
import tomllib
from pathlib import Path

IMG = Path(__file__).resolve().parents[1] / "images" / "webarena" / "map-osrm"
DOCKERFILE = (IMG / "Dockerfile").read_text()
MANIFEST = tomllib.loads((IMG / "image.toml").read_text())
MEDIA = MANIFEST["media"]

# What osrm-routed names as "Required files are missing"; the Dockerfile's list
# is the contract, this is the copy the tests reason about.
REQUIRED_EXTS = [
    "mldgr", "ramIndex", "fileIndex", "edges", "geometry", "names", "properties",
    "timestamp", "datasource_names", "icd", "maneuver_overrides",
    "turn_weight_penalties", "turn_duration_penalties",
]
PROFILES = ["car", "bike", "foot"]


def lines():
    return DOCKERFILE.splitlines()


def add_lines():
    return [l for l in lines() if l.startswith("ADD") and ".media/bucket-" in l]


# --- the media lands where the entrypoint reads --------------------------------

def test_bucket_indices_are_contiguous_from_zero():
    """demux writes bucket-00..bucket-(max_buckets-1). A gap in the ADDs would
    fail the build on a missing file, or — worse — leave a bucket's worth of
    routing data out of an image that still builds."""
    got = [int(re.search(r"bucket-(\d+)\.tar", l).group(1)) for l in add_lines()]
    assert got == list(range(MEDIA["max_buckets"]))


def test_the_adds_extract_where_the_entrypoint_looks():
    """dest plus the archive's own top level is what the entrypoint opens."""
    assert MEDIA["dest"] == "/data"
    assert MEDIA["strip"] == "", "the archive's top level is already car/ bike/ foot/"
    entry = (IMG / "entrypoint.sh").read_text()
    for p in PROFILES:
        assert f'data="{MEDIA["dest"]}/$profile' in entry or f"{MEDIA['dest']}/{p}" in entry


def test_chown_is_declared_and_carried_by_every_add():
    """Not the archive's own ownership: its profile directories carry the uid of
    the account that captured the tar. An ADD that lost the flag would ship
    them that way and still build clean."""
    assert MEDIA["chown"] == "root:root"
    for l in add_lines():
        assert f"--chown={MEDIA['chown']}" in l


# --- the validation moved rather than vanished ---------------------------------

def test_the_full_file_set_is_still_checked():
    body = DOCKERFILE
    for ext in REQUIRED_EXTS:
        assert re.search(rf"\b{ext}\b", body), (
            f"{ext} dropped from the check — osrm-routed names it as required")
    for p in PROFILES:
        assert p in body


def test_the_mldgr_size_floor_is_still_checked():
    assert "100000000" in DOCKERFILE, "the .mldgr truncation floor is gone"


def test_the_validation_runs_after_the_media_lands():
    """In the final stage, past the last ADD. Anywhere earlier and it inspects a
    directory the buckets have not been extracted into yet — a check that passes
    by looking at nothing."""
    src = lines()
    last_add = max(i for i, l in enumerate(src) if l in add_lines())
    check = min(i for i, l in enumerate(src) if l.startswith("RUN") )
    assert check > last_add
    assert "mldgr" in "\n".join(src[check:])


def test_ownership_is_asserted_in_the_image():
    """`ADD --chown` exits clean on a name the image cannot resolve, so the flag
    taking effect is not otherwise observable from inside the build."""
    assert re.search(r"!\s*-user root", DOCKERFILE)


def test_min_entries_covers_the_files_the_check_demands():
    """The floor and the in-image check are two halves of one statement. A floor
    below what the check requires would let the host pass an archive the build
    is about to reject, twelve minutes of download later."""
    assert MEDIA["min_entries"] >= len(REQUIRED_EXTS) * len(PROFILES)


# --- nothing stages the tree anymore -------------------------------------------

def test_there_is_no_restore_stage():
    """The whole dataset is media, so extracting it into a stage would write
    19.8 GiB for a COPY to lift straight back out."""
    froms = [l for l in lines() if l.startswith("FROM")]
    assert len(froms) == 1, "a second stage is a second copy of the routing data"
    body = "\n".join(l for l in lines() if not l.lstrip().startswith("#"))
    assert "--from=restore" not in body
    assert "osrm_routing.tar" not in body, (
        "the archive is demuxed on the host; a COPY of it would put 19.8 GiB "
        "back into a build stage")


def test_restore_needs_media_is_false():
    """Stated explicitly: there is no stage that could read it."""
    assert MEDIA["restore_needs_media"] is False
