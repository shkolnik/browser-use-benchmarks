"""All seven cache sites read through the shared library; the six also write
through it.

Task 2 built `builder/stage-lib/derive-cache.sh`; this pins that every derive
script actually sources and uses it, rather than the library existing next to
scripts that still do their own `docker pull`/`docker build -t "$CACHE"`.
Wikipedia is the one deliberate exception on the WRITE side (see the plan's
Global Constraints — its cache is ~88 GB and this task gives it the dual-
format read only), so its push path keeps building a legacy scratch image.

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


@pytest.mark.parametrize("script", SIX, ids=lambda p: p.parent.name)
def test_the_six_no_longer_pull_the_cache_image_directly(script):
    code = _code(script.read_text())
    assert not re.search(r"docker pull\s+\"\$CACHE\"", code), (
        f"{script}: still calls `docker pull \"$CACHE\"` directly instead of "
        "going through dcache_pull")


@pytest.mark.parametrize("script", SIX, ids=lambda p: p.parent.name)
def test_the_six_no_longer_build_the_cache_image_to_push(script):
    code = _code(script.read_text())
    assert not re.search(r"docker build\s+-t\s+\"\$CACHE\"", code), (
        f"{script}: still builds a `FROM scratch` image to push instead of "
        "calling dcache_push directly on the files")


@pytest.mark.parametrize("script", SIX, ids=lambda p: p.parent.name)
def test_the_six_pull_through_the_library(script):
    code = _code(script.read_text())
    assert "dcache_pull" in code, f"{script}: does not call dcache_pull"


@pytest.mark.parametrize("script", SIX, ids=lambda p: p.parent.name)
def test_the_six_push_through_the_library(script):
    code = _code(script.read_text())
    assert "dcache_push" in code, f"{script}: does not call dcache_push"


def test_wikipedia_pulls_through_the_library():
    code = _code(WIKIPEDIA.read_text())
    assert "dcache_pull" in code, "wikipedia's read side must use dcache_pull too"


def test_wikipedia_still_builds_and_pushes_the_legacy_image():
    """Task 3 gives wikipedia the dual-format READ only (Global Constraints):
    its ~88 GB cache must not gain an opportunistic re-push on the next
    ordinary rebuild. Its write path stays exactly as it was."""
    code = _code(WIKIPEDIA.read_text())
    assert re.search(r'docker build\s+-t\s+"\$CACHE"', code), (
        "wikipedia's push path must still build the legacy scratch image — "
        "converting it was explicitly out of scope for this task")
    assert "dcache_push" not in code, (
        "wikipedia must not opportunistically push its ~88 GB cache as oras; "
        "that conversion happens only when it is next re-derived")
