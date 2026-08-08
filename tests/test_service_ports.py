"""Every service's healthcheck must probe the port compose actually publishes.

These two numbers live in different files — `[service].healthcheck` in
images/<bench>/<service>/image.toml, and the `ports:` mapping plus HTTP_PORT in
images/<bench>/compose.yml — and nothing connected them. Moving wikipedia off
8888 (taken on the build runner by the OpenTelemetry collector, which failed
run 31265916367's smoke with "address already in use") meant editing six files
by hand, and a miss in any one of them produces a smoke gate that polls a port
nothing is bound to. That reads as an unhealthy service rather than as the
config error it is.

This runs over the REAL manifests and compose files rather than fixtures: the
invariant is about what this repo ships, and a fixture could not go stale in
the way the thing being guarded does.
"""
from pathlib import Path

import pytest
import yaml

from builder.discover import find_images
from builder.manifest import load_manifest

REPO = Path(__file__).resolve().parent.parent


def _published_port(mapping) -> str:
    """The host port from a compose `ports:` entry, short or long form."""
    if isinstance(mapping, dict):
        return str(mapping.get("published", ""))
    # "9888:80", "127.0.0.1:9888:80" — the host port is the field before the
    # container port, which is always last.
    return str(mapping).split(":")[-2]


def _cases():
    out = []
    for ref in find_images(REPO, "all"):
        m = load_manifest(ref.path)
        if not m.healthcheck:
            continue
        compose = REPO / "images" / ref.benchmark / "compose.yml"
        if not compose.is_file():
            continue
        out.append(pytest.param(ref, m, compose, id=ref.name))
    return out


CASES = _cases()


def test_there_are_cases():
    # A collection bug that found nothing would make every test below vacuous,
    # and a vacuous suite is indistinguishable from a passing one.
    assert CASES, "no image with a [service].healthcheck was discovered"


@pytest.mark.parametrize("ref,m,compose", CASES)
def test_healthcheck_port_is_the_published_port(ref, m, compose):
    spec = yaml.safe_load(compose.read_text())["services"][ref.service]
    published = {_published_port(p) for p in spec.get("ports", [])}
    url = m.healthcheck
    # http://127.0.0.1:9888/path -> 9888
    hostport = url.split("//", 1)[1].split("/", 1)[0]
    probed = hostport.split(":")[1] if ":" in hostport else (
        "443" if url.startswith("https") else "80")
    assert probed in published, (
        f"{ref.name}: [service].healthcheck probes port {probed} but "
        f"{compose.relative_to(REPO)} publishes {sorted(published)} for "
        f"service '{ref.service}'. The smoke gate would poll a port nothing "
        f"is bound to and report the service unhealthy.")


@pytest.mark.parametrize("ref,m,compose", CASES)
def test_http_port_env_matches_the_published_port(ref, m, compose):
    """HTTP_PORT describes the PUBLISHED side of the mapping (service-contract).

    An image that renders absolute URLs from HTTP_PORT emits links to a port
    nothing listens on when this drifts, which no healthcheck would catch —
    the page still returns 200.
    """
    spec = yaml.safe_load(compose.read_text())["services"][ref.service]
    env = spec.get("environment") or {}
    if isinstance(env, list):
        env = dict(e.split("=", 1) for e in env if "=" in e)
    if "HTTP_PORT" not in env:
        pytest.skip(f"{ref.name} declares no HTTP_PORT")
    published = {_published_port(p) for p in spec.get("ports", [])}
    assert str(env["HTTP_PORT"]) in published, (
        f"{ref.name}: HTTP_PORT={env['HTTP_PORT']} but "
        f"{compose.relative_to(REPO)} publishes {sorted(published)}. "
        f"HTTP_PORT is the published port, not the in-container one.")
