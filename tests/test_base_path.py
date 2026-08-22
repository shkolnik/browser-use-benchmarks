"""service.base_path must agree with the healthcheck it sits beside.

Both describe where the app answers, and they are edited in different places at
different times. Read the real manifests rather than fixtures: the invariant is
about what this repo ships, and a fixture cannot go stale the way the thing being
guarded does.
"""

from pathlib import Path
from urllib.parse import urlparse

import pytest

from builder.manifest import load_manifest

REPO = Path(__file__).resolve().parent.parent


def _cases():
    out = []
    for manifest in sorted(REPO.glob("images/*/*/image.toml")):
        m = load_manifest(manifest.parent)
        if m.base_path:
            out.append(pytest.param(m, id=str(manifest.parent.relative_to(REPO / "images"))))
    return out


CASES = _cases()


def test_there_are_cases():
    # A collection bug that found nothing would make the test below vacuous, and
    # a vacuous suite is indistinguishable from a passing one.
    assert CASES, "no image declares a [service].base_path"


@pytest.mark.parametrize("m", CASES)
def test_healthcheck_lives_under_base_path(m):
    path = urlparse(m.healthcheck).path
    assert path.startswith(m.base_path + "/") or path == m.base_path, (
        f"healthcheck path {path!r} is not under base_path {m.base_path!r} — "
        "one of the two has drifted"
    )
