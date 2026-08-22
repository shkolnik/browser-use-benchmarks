#!/usr/bin/env python3
"""Place VisualWebArena's per-task input images under datasets/.

VWA references an image by a path relative to the upstream repository root, and
those images are only in the git tree -- there is no released archive -- so the
pinnable object is the commit tarball. This fetches it, verifies it against the
pin in tasks/vwa/datasets.toml, and unpacks the two trees the tasks address into
a flat directory whose layout matches the paths the task files use.

Not `bin/build`: find_images globs images/*/*/image.toml, so the builder
structurally cannot see a tasks-side dataset. The fetching itself is
builder.download.ensure_dataset, which already rotates mirrors, resumes, and
quarantines a file that fails its checksum rather than passing it on.
"""

import argparse
import sys
import tarfile
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from builder.download import ensure_dataset  # noqa: E402
from builder.manifest import Dataset  # noqa: E402

PIN = HERE / "datasets.toml"

# Where the images land, relative to the datasets dir. Task files address an
# image by its path inside the upstream repo, so the prefix is stripped once
# here and a caller joins the rest verbatim.
DEST = "vwa-input-images"
TREES = (
    "environment_docker/webarena-homepage/static/input_images/",
    "coco_images/",
)


def pinned() -> tuple[Dataset, dict]:
    data = tomllib.loads(PIN.read_text())
    entries = data.get("datasets", [])
    if len(entries) != 1:
        raise SystemExit(f"error: {PIN} must declare exactly one dataset, found {len(entries)}")
    d = entries[0]
    return Dataset(filename=d["filename"], sha256=d["sha256"], urls=list(d["urls"])), d


def wanted(name: str) -> str | None:
    """The destination path for an archive member, or None to skip it.

    Members are prefixed with the archive's own top-level directory, which
    carries the commit sha and would otherwise leak into every stored path.
    """
    parts = name.split("/", 1)
    if len(parts) != 2:
        return None
    rel = parts[1]
    for tree in TREES:
        if rel.startswith(tree):
            return rel
    return None


def unpack(archive: Path, dest: Path) -> int:
    extracted = 0
    with tarfile.open(archive) as tar:
        for member in tar:
            if not member.isfile():
                continue
            rel = wanted(member.name)
            if rel is None:
                continue
            target = dest / rel
            # A member whose path escapes the destination would be written
            # outside the datasets dir. Nothing in this archive does, and that
            # is exactly why an unchecked extract here would never be noticed.
            resolved = target.resolve()
            if not resolved.is_relative_to(dest.resolve()):
                raise SystemExit(f"error: {member.name} escapes {dest}")
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = tar.extractfile(member)
            if handle is None:
                continue
            target.write_bytes(handle.read())
            extracted += 1
    return extracted


def count_on_disk(dest: Path) -> tuple[int, int]:
    files = [p for p in dest.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets-dir", type=Path, default=REPO / "datasets")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is already unpacked without fetching anything",
    )
    args = parser.parse_args()

    dataset, entry = pinned()
    dest = args.datasets_dir / DEST

    if args.check:
        if not dest.is_dir():
            sys.exit(f"{dest} does not exist — run this script without --check")
        files, size = count_on_disk(dest)
        print(f"{dest}: {files} files, {size} bytes")
        expected = entry.get("unpacked_files")
        if expected and files != expected:
            sys.exit(f"expected {expected} files, found {files}")
        sys.exit(0)

    archive = ensure_dataset(dataset, args.datasets_dir)
    dest.mkdir(parents=True, exist_ok=True)
    extracted = unpack(archive, dest)
    files, size = count_on_disk(dest)
    print(f"unpacked {extracted} files into {dest} ({files} files, {size} bytes total)")
