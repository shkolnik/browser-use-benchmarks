"""All seven cache sites read AND write through the shared library.

Task 2 built `builder/stage-lib/derive-cache.sh`; this pins that every derive
script actually sources and uses it, rather than the library existing next to
scripts that still do their own `docker pull`/`docker build -t "$CACHE"`.

Wikipedia used to be a deliberate exception on the WRITE side: its entry is
~88 GB, so it kept building a legacy scratch image rather than gain an
opportunistic oras re-push inside an ordinary fleet run. That exception ended
when the entry was converted deliberately, out of band — nothing is left for
it to protect, and a wikipedia re-derive that still wrote the legacy format
would silently undo the conversion. So the parametrizations below cover all
SEVEN, and there is no longer a per-site carve-out to pin.

The export-filter invariant (#79) is not re-implemented here — it is already
pinned, per-line, by tests/test_derive_export_filtered.py; this file only
checks that the filter is still wired through `dcache_pull`.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SIX = sorted((REPO / "images").glob("*/*/derive-backup.sh"))
WIKIPEDIA = REPO / "images" / "webarena" / "wikipedia" / "split-zim.sh"
SEVEN = SIX + [WIKIPEDIA]

SOURCE_LINE = re.compile(
    r"""\.\s+["']?\$REPO_ROOT["']?/builder/stage-lib/derive-cache\.sh["']?""")


def _code(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_all_seven_scripts_are_present():
    assert len(SIX) == 6, [str(p) for p in SIX]
    assert WIKIPEDIA.is_file()


@pytest.mark.parametrize("script", SEVEN, ids=lambda p: f"{p.parent.name}-{p.name}")
def test_every_site_sources_the_shared_library(script):
    code = _code(script.read_text())
    assert SOURCE_LINE.search(code), (
        f"{script}: does not source builder/stage-lib/derive-cache.sh — the "
        "cache protocol must live in one place, not be re-implemented per site")


@pytest.mark.parametrize("script", SEVEN, ids=lambda p: p.parent.name)
def test_no_site_pulls_the_cache_image_directly(script):
    code = _code(script.read_text())
    assert not re.search(r"docker pull\s+\"\$CACHE\"", code), (
        f"{script}: still calls `docker pull \"$CACHE\"` directly instead of "
        "going through dcache_pull")


@pytest.mark.parametrize("script", SEVEN, ids=lambda p: p.parent.name)
def test_no_site_builds_the_cache_image_to_push(script):
    code = _code(script.read_text())
    assert not re.search(r"docker build\s+-t\s+\"\$CACHE\"", code), (
        f"{script}: still builds a `FROM scratch` image to push instead of "
        "calling dcache_push directly on the files")


@pytest.mark.parametrize("script", SEVEN, ids=lambda p: p.parent.name)
def test_every_site_pulls_through_the_library(script):
    code = _code(script.read_text())
    assert "dcache_pull" in code, f"{script}: does not call dcache_pull"


@pytest.mark.parametrize("script", SEVEN, ids=lambda p: p.parent.name)
def test_every_site_pushes_through_the_library(script):
    code = _code(script.read_text())
    assert "dcache_push" in code, f"{script}: does not call dcache_push"
