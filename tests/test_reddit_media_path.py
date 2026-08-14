"""Invariants that came with routing reddit's uploaded images around the build.

Both are fleet-wide in shape and are applied to every image that declares
[media]; they live in a reddit-named file because reddit is the conversion that
made them necessary. Its archive is the first that does not have a single
top-level directory to strip, and the first whose Dockerfile had to stop COPYing
the archive into a build stage.
"""

import re
from pathlib import Path

import pytest

from builder.manifest import load_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _media_images():
    images = [(t.parent, load_manifest(t.parent))
              for t in sorted((REPO_ROOT / "images").glob("*/*/image.toml"))]
    return [(d, m) for d, m in images if m.media]


def _instructions(dockerfile: Path) -> str:
    # Comments only. Every image that converted explains in prose why the
    # archive is absent, and naming it there is the point of the note — it is
    # the instructions that must not mention it.
    return "\n".join(l for l in dockerfile.read_text().splitlines()
                     if not l.lstrip().startswith("#"))


def test_some_image_uses_the_media_path():
    assert _media_images(), "no image declares [media]"


@pytest.mark.parametrize("image,m", _media_images(),
                         ids=[d.name for d, _ in _media_images()])
def test_no_build_stage_copies_an_unread_media_archive(image, m):
    # restore_needs_media = false asserts that no build stage opens the tree, and
    # the host-side demux is what makes that pay: the archive is bucketed on the
    # CI host and never materialised. A `COPY --from=datasets <archive>` left
    # behind would silently reinstate the cost the declaration claims is gone —
    # tens of gigabytes into a stage, plus the layer holding them — and no
    # assertion in the build would notice, because the build still works.
    if m.media.restore_needs_media:
        return
    assert m.media.archive not in _instructions(image / "Dockerfile"), (
        f"{image.name}/Dockerfile still COPYs or mounts {m.media.archive}, but its "
        f"[media] declares restore_needs_media = false — the archive is "
        f"bucketed on the host and must not enter a build stage.")


def test_reddit_strip_and_dest_match_what_derive_backup_tars():
    # The archive is written `cd <dir> && tar cf ... media submission_images`,
    # so it has TWO top-level directories: there is no common prefix to strip,
    # and the only correct dest is the directory tar ran in. A strip added here
    # would drop `media/` and `submission_images/` from every path and scatter
    # 38.5G of images directly into public/ — an ADD that succeeds and an image
    # that serves 404s for every upload.
    image = REPO_ROOT / "images" / "webarena" / "reddit"
    m = load_manifest(image)
    derive = (image / "derive-backup.sh").read_text()
    cd_dir, tarred = re.search(
        r"cd (\S+) && tar cf \S+ \$\(ls -d ([^)]*?) 2>/dev/null\)",
        derive).groups()
    assert len(tarred.split()) > 1, (
        "derive-backup.sh now archives a single subtree; strip/dest below were "
        "chosen because it archives more than one")
    assert m.media.strip == "", "the archive has no single prefix to strip"
    assert m.media.dest.split("/")[-1] == cd_dir.split("/")[-1], (
        f"[media].dest is {m.media.dest}, but derive-backup.sh tars from "
        f"{cd_dir} — the members' paths are relative to that directory.")
