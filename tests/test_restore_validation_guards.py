"""The two defects that failed shopping AND shopping-admin on run 31284811602.

Both images died with a bare `exit code: 7` and no diagnosis at all. Two
separate bugs stacked: nginx was curled before it was listening, and the way
the check was written meant the error branch could never run to say so.

These are source-shape assertions rather than behavioural ones because the
behaviour needs a booted Magento stack inside a ~45G image. The shape is
still the thing that broke, and it broke identically in two files, so it is
worth pinning where a reviewer will see it.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STAGES = [
    REPO / "images" / "webarena" / "shopping" / "restore-stage.sh",
    REPO / "images" / "webarena" / "shopping-admin" / "restore-stage.sh",
]

# `name=$(curl ...)`, the capture form. A bare `curl` as its own statement is
# a different thing and is allowed to fail the script directly.
CAPTURE = re.compile(r"^\s*\w+=\$\(\s*curl\b[^\n]*\)", re.M)


@pytest.mark.parametrize("stage", STAGES, ids=lambda p: p.parent.name)
def test_every_curl_capture_survives_a_connection_failure(stage):
    """Under `set -e` the ASSIGNMENT is what fails, before any check runs.

    `code=$(curl ...)` with no `|| true` kills the script with curl's exit 7
    when nothing is listening, so the `[ "$code" = 200 ] || { echo ...; }` on
    the very next line never executes. The operator gets an exit code and no
    message. With the guard, curl writes 000 and the existing check reports it.
    """
    assert stage.is_file(), stage
    unguarded = [m.group(0).strip() for m in CAPTURE.finditer(stage.read_text())
                 if "|| true" not in m.group(0)]
    assert not unguarded, (
        "these captures die under set -e before their own error branch can "
        f"report anything:\n  " + "\n  ".join(unguarded))


@pytest.mark.parametrize("stage", STAGES, ids=lambda p: p.parent.name)
def test_nginx_is_waited_for_before_it_is_curled(stage):
    """svc_start returns at fork, not at bind.

    mariadb and elasticsearch both get readiness loops in these scripts; nginx
    got none, so the first validation curl raced the daemon. Ordering is the
    assertion: a wait that runs after the first curl would be no wait at all.
    """
    text = stage.read_text()
    start = text.index("svc_start_nginx")
    assert "svc_wait_http" in text[start:], (
        f"{stage.parent.name}: nginx is started and never waited for")
    wait_at = text.index("svc_wait_http", start)
    first_curl = CAPTURE.search(text, start)
    assert first_curl, f"{stage.parent.name}: no validation curl found at all"
    assert wait_at < first_curl.start(), (
        f"{stage.parent.name}: the readiness wait comes AFTER the first "
        "validation curl, which is the race it was meant to close")
