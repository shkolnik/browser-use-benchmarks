"""Invariants tying vwa/classifieds' [media] section to the files around it.

The generic media invariants — ADD-line count vs max_buckets, and that unused
buckets are still valid tars — live in test_bucket_media.py and cover every
image that declares a [media] section. What is checked here is specific to this
image: where the archive is rooted, and the claim that no build stage reads it.
"""

import re
from pathlib import Path

from builder.manifest import load_manifest

IMAGE = Path(__file__).resolve().parents[1] / "images" / "vwa" / "classifieds"
MEDIA = load_manifest(IMAGE).media


def test_media_is_rooted_where_the_derive_script_tars_it():
    """dest/strip describe the archive, and only derive-backup.sh knows its shape.

    The archive is written by one `tar cf … -C <dir> <subtree>` in the prepare
    script, so <dir> is the directory its members are relative to — which is
    exactly what dest means — and the members keep <subtree> as their prefix,
    which is why nothing is stripped. Change either half of that tar command and
    the members land somewhere else in the image with nothing failing at build
    time, so the two are read out of the script rather than restated.
    """
    tar = re.search(r"tar cf \S+ \\\n\s*-C (\S+) (\S+)\b",
                    (IMAGE / "derive-backup.sh").read_text())
    assert tar, "derive-backup.sh no longer writes the archive with a single `tar cf -C`"
    parent, subtree = tar.group(1), tar.group(2)
    assert MEDIA.dest == parent
    assert MEDIA.strip == "", (
        f"strip must stay empty so the archive's top level is the single "
        f"'{subtree}' directory. demux-media.py keeps every top-level directory "
        f"as padding for unused buckets and dedupes that list by scanning it; "
        f"stripping '{subtree}' would promote 84,148 per-item directories to "
        f"the top level, making the padding enormous and the dedupe quadratic.")
    dockerfile = (IMAGE / "Dockerfile").read_text()
    assert f"{parent}/{subtree}" in dockerfile, (
        "the final stage must still name the directory the members extract into "
        "— it is what the ownership guard checks")


def test_no_build_stage_reads_the_media_archive():
    """restore_needs_media = false is an assertion about the whole build.

    It says the tree is never materialised, so the archive must not reach a
    build stage by any other route either. A stray COPY --from=datasets would
    put the 73G back inside the build while the media path also shipped it —
    doubling the work rather than replacing it — and only a disk-space failure
    on the runner would say so.
    """
    assert MEDIA.restore_needs_media is False
    for name in ("Dockerfile", "restore-stage.sh", "audit.sh", "entrypoint.sh"):
        # Comments stripped: the Dockerfile says in prose that it deliberately
        # does not copy the archive, and that sentence is the thing being
        # checked, not a violation of it.
        code = [l for l in (IMAGE / name).read_text().splitlines()
                if not l.lstrip().startswith("#")]
        offenders = [l for l in code if MEDIA.archive in l]
        assert not offenders, (
            f"{name} still references {MEDIA.archive} ({offenders}), but "
            f"[media] declares that no build stage reads it")


def test_the_photos_arrive_owned_by_the_user_that_serves_them():
    """php -S runs as www-data and Osclass writes new item photos at runtime.

    A tree owned by anyone else is a runtime failure the healthcheck cannot see:
    the home page renders from the database and never writes a photo.
    """
    user = re.search(r"svc_start php --user (\S+)",
                     (IMAGE / "entrypoint.sh").read_text()).group(1)
    assert MEDIA.chown == f"{user}:{user}"
    guard = re.search(r"find (\S+) -type d ! -user (\S+) ",
                      (IMAGE / "Dockerfile").read_text())
    assert guard, "the final stage lost its root-owned-directory guard"
    assert guard.group(2) == user
