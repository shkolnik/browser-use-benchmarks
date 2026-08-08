"""Every `docker export` of a cache image must extract through a filter.

`docker export` does not hand back only the files a `FROM scratch` image
COPYed in. Docker injects `/dev`, `/etc`, `/proc`, `/sys` and `/.dockerenv`
into every container it creates, and they ride along in the export stream.
Measured on a two-file scratch image (2026-08-08): 14 members, 12 of them
injected. So the bare idiom

    docker export "$cid" | tar -x -C "$DATASETS_DIR"

drops an `etc/` holding hosts/resolv.conf, plus dev/, proc/, sys/ and
.dockerenv, straight into the runner's shared datasets directory — on every
cache hit, for every image. It is dirt rather than corruption, but it makes
the datasets dir unattributable and a name like `etc` could shadow a real
output.

USE EXACTLY ONE PATTERN per extract. GNU tar exits 2 with "Not found in
archive" if ANY given pattern matches nothing, so listing several shapes
turns a successful extract into a hard failure the moment one of them is
absent (verified with tar 1.35, 2026-08-08). One wildcard covering all of a
script's outputs is both sufficient and safe.
"""
import re
from pathlib import Path

import pytest

# The idiom used to live in each of the seven derive scripts under images/;
# Task 3 of the derived-cache plan centralized the legacy extract into
# builder/stage-lib/derive-cache.sh's dcache_pull, so the ONE place doing a
# raw `docker export | tar` today is the library, not the per-site scripts.
# Scan both trees so this guard keeps covering wherever the idiom actually
# lives instead of going quietly vacuous.
REPO = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((REPO / "images").glob("*/*/*.sh")) + \
    sorted((REPO / "builder" / "stage-lib").glob("*.sh"))

EXPORT = re.compile(r"docker export\b[^\n|]*\|[^\n]*\btar\b[^\n]*")


def _code(text: str) -> str:
    # builder/stage-lib/derive-cache.sh's header prose mentions the
    # `docker export | tar` idiom on one line while explaining it (see #79) —
    # stripping comments first keeps this scanning CODE, not documentation
    # that happens to describe code.
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _export_lines(text: str):
    return [m.group(0) for m in EXPORT.finditer(text)]


def _cases():
    out = []
    for s in SCRIPTS:
        for line in _export_lines(_code(s.read_text())):
            out.append(pytest.param(s, line, id=f"{s.parent.parent.name}-{s.parent.name}"))
    return out


CASES = _cases()


def test_there_are_cases():
    # One occurrence lives today, in the shared library's legacy fallback path
    # (dcache_pull). A collection bug that found none would make every test
    # below vacuous, and a vacuous suite looks like a green one.
    assert len(CASES) >= 1, f"expected the fleet's export idiom, found {len(CASES)}"


@pytest.mark.parametrize("script,line", CASES)
def test_export_extract_is_filtered(script, line):
    assert "--wildcards" in line, (
        f"{script.relative_to(REPO)} extracts a `docker export` stream without a "
        f"filter:\n    {line.strip()}\n"
        f"That writes Docker's injected .dockerenv, dev/, etc/, proc/ and sys/ "
        f"into the datasets directory. Add `--wildcards '<one-pattern>'` matching "
        f"exactly this script's outputs.")


@pytest.mark.parametrize("script,line", CASES)
def test_export_extract_uses_exactly_one_pattern(script, line):
    """More than one pattern is a latent hard failure, not extra safety."""
    after = line.split("--wildcards", 1)[1] if "--wildcards" in line else ""
    patterns = re.findall(r"""(?:"([^"]+)"|'([^']+)')""", after)
    flat = [a or b for a, b in patterns]
    assert len(flat) == 1, (
        f"{script.relative_to(REPO)} passes {len(flat)} patterns to tar "
        f"({flat}):\n    {line.strip()}\n"
        f"tar exits 2 on any pattern that matches nothing, so the extract fails "
        f"as soon as one output is absent. Use a single covering wildcard.")
