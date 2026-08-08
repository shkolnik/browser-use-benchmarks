"""A derive script that cannot publish its cache must fail, not warn.

builder/docker.py stamps a successful prepare step and skips the script on
every later run with matching inputs, so the run that derives is the only run
that ever pushes. A warning there leaves the cache empty for good while later
builds believe it is populated — PR #16's finding on wikipedia, applied to the
other six.

Task 3 of the derived-cache plan converted the six to push through the shared
`dcache_push` (builder/stage-lib/derive-cache.sh), which carries this exact
retry-then-fail contract itself (pinned separately by
tests/test_derive_cache_lib.py::test_a_push_retries_three_times_then_exits_nonzero).
So a script satisfies the behaviour here either by still doing its own retry
loop (wikipedia's legacy push path, out of scope for Task 3 — see the plan's
Global Constraints) or by delegating to dcache_push — never by swallowing a
failed push either way.
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
    has_own_retry_loop = "for attempt in 1 2 3" in code and re.search(r"exit 1", code)
    delegates_to_library = "dcache_push" in code
    assert has_own_retry_loop or delegates_to_library, (
        f"{script}: no retry loop and no dcache_push delegate — a failed push "
        "would not be retried and would not fail the build")
