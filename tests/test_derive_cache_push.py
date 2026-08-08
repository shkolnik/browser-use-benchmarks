"""A derive script that cannot publish its cache must fail, not warn.

builder/docker.py stamps a successful prepare step and skips the script on
every later run with matching inputs, so the run that derives is the only run
that ever pushes. A warning there leaves the cache empty for good while later
builds believe it is populated — PR #16's finding on wikipedia, applied to the
other six.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SIX = sorted((REPO / "images").glob("*/*/derive-backup.sh"))


def _code(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_all_six_scripts_are_present():
    assert len(SIX) == 6, [str(p) for p in SIX]


@pytest.mark.parametrize("script", SIX, ids=lambda p: p.parent.name)
def test_a_push_failure_is_never_swallowed(script):
    code = _code(script.read_text())
    assert not re.search(r"push[^\n]*\|\|\s*echo", code), (
        f"{script}: a failed cache push is reported as a warning and the run "
        "still succeeds, so prepare_reuse_check stamps it and nothing retries")


@pytest.mark.parametrize("script", SIX, ids=lambda p: p.parent.name)
def test_the_push_is_retried_then_fatal(script):
    code = _code(script.read_text())
    assert "for attempt in 1 2 3" in code, f"{script}: no retry loop"
    assert re.search(r"exit 1", code), f"{script}: no hard failure path"
