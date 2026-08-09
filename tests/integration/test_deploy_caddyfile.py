"""Adapt deploy/Caddyfile with the real Caddy, so a broken config fails here.

The unit tests in tests/test_deploy_proxy.py check what the config SAYS —
that every service has a subdomain, that upstreams name the right ports. None
of them can tell whether Caddy will accept the file at all, and that is a live
failure mode: the index body is backtick-quoted, backtick is also Caddy's
multi-line delimiter, and writing a shell command in that body the Markdown way
closed the string early. Caddy refused the whole config, the container
crashlooped, and every subdomain went down — for a typo in help text.

`caddy validate` adapts and provisions the config without binding a port, which
is exactly the check the unit tests structurally cannot do.
"""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CADDYFILE = REPO / "deploy" / "Caddyfile"
OVERLAY = REPO / "deploy" / "compose.proxy.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or subprocess.run(
        ["docker", "info"], capture_output=True).returncode != 0,
    reason="docker unavailable")


def caddy_image() -> str:
    """The pinned image from the overlay, so this cannot validate a different
    Caddy than the one that will serve."""
    return yaml.safe_load(OVERLAY.read_text())["services"]["proxy"]["image"]


def validate(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm",
         "-e", "BENCH_HOST=example.com", "-e", "PROXY_PORT=80",
         "-v", f"{path}:/etc/caddy/Caddyfile:ro",
         caddy_image(),
         "caddy", "validate", "--adapter", "caddyfile",
         "--config", "/etc/caddy/Caddyfile"],
        capture_output=True, text=True)


def test_caddyfile_adapts():
    r = validate(CADDYFILE)
    assert r.returncode == 0, (
        f"deploy/Caddyfile is not a valid Caddy config, so the proxy would "
        f"crashloop and every subdomain would be down:\n{r.stderr}")


def test_validation_would_catch_a_broken_config(tmp_path):
    """Prove the check bites, not just that it runs.

    A validator that returned 0 unconditionally would make the test above
    vacuous, and vacuous is indistinguishable from passing.
    """
    broken = tmp_path / "Caddyfile"
    broken.write_text(CADDYFILE.read_text().replace(
        "    docker compose up -d --wait <name>",
        "`docker compose up -d --wait <name>`"))
    r = validate(broken)
    assert r.returncode != 0, (
        "a stray backtick in the index body should fail validation; if this "
        "passes, the check above proves nothing")
