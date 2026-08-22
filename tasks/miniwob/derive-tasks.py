#!/usr/bin/env python3
"""Derive tasks/miniwob/core/tasks.jsonl from the vendored upstream sources.

MiniWoB++ ships no task manifest. What a task IS lives in three places: the
gymnasium registry names it, an env class docstring describes it, and the HTML
page it maps to carries its episode deadline. This joins the three.

Two upstream refs are in play and they are not interchangeable. The registry and
the env docstrings come from the vendored copies under upstream/, taken at the
commit tasks/miniwob/core/suite.toml pins. The HTML comes from the v1.0 tag
archive, because that is what images/miniwob/server serves -- and only the HTML
carries `core.EPISODE_MAX_TIME`, which is the difference between a task an agent
can finish and one it cannot.

Run with --check in CI: it regenerates and diffs, so a vendored source that moves
without the derived file moving is a failure rather than a silent divergence.
"""

import argparse
import ast
import hashlib
import json
import re
import sys
import tarfile
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
UPSTREAM = HERE / "upstream" / "miniwob"
OUT = HERE / "core" / "tasks.jsonl"

# The HTML archive is a build input of images/miniwob/server, so its pin lives in
# that image's manifest and nowhere else. A second copy here would be a
# silent-wrong-data hole: update one and this script would verify the old bytes.
IMAGE_MANIFEST = REPO / "images" / "miniwob" / "server" / "image.toml"
ARCHIVE = "miniwob-plusplus.tar.gz"

# selenium_instance.py builds a flight task's URL as
# subdomain.replace(".", "/") + "/wrapper.html", and everything else as
# <subdomain>.html. The viewports differ with it.
VIEWPORT = {"window_w": 500, "window_h": 320, "task_w": 160, "task_h": 210}
FLIGHT_VIEWPORT = {"window_w": 600, "window_h": 800, "task_w": 375, "task_h": 667}

# core.js sets this once and each page may override it. A page that does not
# override it gets ten seconds, which is the trap: nothing reports the deadline,
# and on expiry the page scores -1 with no error.
CORE_DEFAULT_EPISODE_MAX_TIME_MS = 10000


def read_pin() -> str:
    manifest = tomllib.loads(IMAGE_MANIFEST.read_text())
    for dataset in manifest.get("datasets", []):
        if dataset.get("filename") == ARCHIVE:
            return dataset["sha256"]
    raise SystemExit(f"{IMAGE_MANIFEST} declares no dataset named {ARCHIVE}")


def open_archive(datasets_dir: Path) -> tarfile.TarFile:
    path = datasets_dir / ARCHIVE
    if not path.exists():
        raise SystemExit(
            f"{path} is missing. Fetch it with:\n"
            f"  bin/build download miniwob/server --datasets-dir {datasets_dir}"
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pinned = read_pin()
    if digest != pinned:
        raise SystemExit(
            f"{path} does not match the pin in {IMAGE_MANIFEST}\n"
            f"  pinned {pinned}\n  actual {digest}"
        )
    return tarfile.open(path)


def registrations() -> list[dict]:
    """Every register() call in the vendored registry, in file order.

    The reason a task is flagged nondeterministic is a trailing comment, which
    the AST discards, so the comments are recovered by line. They are the only
    record of WHY an env cannot be replayed from a seed.
    """
    source = (UPSTREAM / "registration.py").read_text()
    lines = source.splitlines()
    reasons = {}
    for i, line in enumerate(lines, start=1):
        match = re.search(r"nondeterministic=True,\s*#\s*(.+)$", line)
        if match:
            reasons[i] = match.group(1).strip()

    # Two section markers divide the file; everything before the first is the
    # main set. The finer taxonomy in upstream's docs is not in this file, so it
    # is not claimed here.
    sections = {}
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped == "# FlightWoB tasks":
            sections[i] = "flightwob"
        elif stripped == "# MiniWoB test set":
            sections[i] = "test"

    def section_at(lineno: int) -> str:
        current = "main"
        for start, name in sorted(sections.items()):
            if lineno >= start:
                current = name
        return current

    out = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "register"):
            continue
        kwargs = {k.arg: k.value for k in node.keywords}
        nondeterministic = False
        reason = None
        if "nondeterministic" in kwargs:
            nondeterministic = bool(ast.literal_eval(kwargs["nondeterministic"]))
            reason = reasons.get(kwargs["nondeterministic"].lineno)
        out.append(
            {
                "env_id": ast.literal_eval(kwargs["id"]),
                "entry_point": ast.literal_eval(kwargs["entry_point"]),
                "section": section_at(node.lineno),
                "nondeterministic": nondeterministic,
                "nondeterministic_reason": reason,
            }
        )
    return out


def env_classes() -> dict[str, dict]:
    """subdomain and docstring facts, keyed by 'module:ClassName'."""
    out = {}
    for stem in ("miniwob_envs", "flightwob_envs"):
        path = UPSTREAM / "envs" / f"{stem}.py"
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            subdomain = None
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and getattr(stmt.targets[0], "id", None) == "subdomain"
                ):
                    subdomain = ast.literal_eval(stmt.value)
            key = f"miniwob.envs.{stem}:{node.name}"
            out[key] = {"subdomain": subdomain, **parse_docstring(ast.get_docstring(node) or "")}
    return out


def parse_docstring(doc: str) -> dict:
    """Pull the sections the env docstrings are written in.

    They follow a fixed shape -- '## Description', '## Utterance fields',
    '## Additional notes' -- because upstream generates them.
    """
    sections: dict[str, list[str]] = {}
    current = None
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current = stripped[3:].strip()
            sections[current] = []
        elif current and stripped:
            sections[current].append(stripped)

    description = " ".join(sections.get("Description", [])).strip()

    fields = []
    for line in sections.get("Utterance fields", []):
        if line == "(none)":
            continue
        if line.startswith("* "):
            fields.append(line[2:].strip())

    notes = [line[2:].strip() for line in sections.get("Additional notes", []) if line.startswith("* ")]
    return {
        "description": description,
        "utterance_fields": fields,
        "additional_notes": notes,
        "partial_reward": any("Partial reward:" in note for note in notes),
    }


def url_path(subdomain: str) -> str:
    if subdomain.startswith("flight."):
        return "/" + subdomain.replace(".", "/") + "/wrapper.html"
    return f"/miniwob/{subdomain}.html"


def episode_deadlines(archive: tarfile.TarFile) -> dict[str, int]:
    """The `core.EPISODE_MAX_TIME` each served page sets, keyed by url path."""
    pattern = re.compile(rb"core\.EPISODE_MAX_TIME\s*=\s*(\d+)")
    found = {}
    for member in archive.getmembers():
        if not member.isfile() or not member.name.endswith(".html"):
            continue
        parts = member.name.split("/")
        if len(parts) < 4 or parts[1:3] != ["miniwob", "html"]:
            continue
        rel = "/" + "/".join(parts[3:])
        handle = archive.extractfile(member)
        if handle is None:
            continue
        match = pattern.search(handle.read())
        if match:
            found[rel] = int(match.group(1))
    return found


def build(datasets_dir: Path) -> list[dict]:
    classes = env_classes()
    archive = open_archive(datasets_dir)
    try:
        deadlines = episode_deadlines(archive)
        served = {
            "/" + "/".join(m.name.split("/")[3:])
            for m in archive.getmembers()
            if m.isfile() and m.name.endswith(".html") and m.name.split("/")[1:3] == ["miniwob", "html"]
        }
    finally:
        archive.close()

    records = []
    for entry in registrations():
        info = classes.get(entry["entry_point"])
        if info is None or not info["subdomain"]:
            raise SystemExit(f"no env class with a subdomain for {entry['entry_point']}")
        path = url_path(info["subdomain"])
        deadline = deadlines.get(path)
        flight = info["subdomain"].startswith("flight.")
        records.append(
            {
                "env_id": entry["env_id"],
                "subdomain": info["subdomain"],
                "url_path": path,
                "served_by_image": path in served,
                "section": entry["section"],
                "description": info["description"],
                "utterance_fields": info["utterance_fields"],
                "partial_reward": info["partial_reward"],
                "additional_notes": info["additional_notes"],
                "nondeterministic": entry["nondeterministic"],
                "nondeterministic_reason": entry["nondeterministic_reason"],
                "episode_max_time_ms": deadline or CORE_DEFAULT_EPISODE_MAX_TIME_MS,
                "episode_max_time_source": "page" if deadline else "core-default",
                "viewport": FLIGHT_VIEWPORT if flight else VIEWPORT,
                "entry_point": entry["entry_point"],
            }
        )
    return records


def render(records: list[dict]) -> str:
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets-dir", type=Path, default=REPO / "datasets")
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate and diff against the committed file; exit 1 on drift",
    )
    args = parser.parse_args()

    rendered = render(build(args.datasets_dir))
    if args.check:
        if not OUT.exists():
            sys.exit(f"{OUT} does not exist")
        if OUT.read_text() != rendered:
            sys.exit(f"{OUT} is stale: regenerate it with {Path(__file__).name}")
        print(f"{OUT} matches the vendored sources")
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(rendered)
        print(f"wrote {OUT} ({len(rendered.splitlines())} records, {len(rendered)} bytes)")
