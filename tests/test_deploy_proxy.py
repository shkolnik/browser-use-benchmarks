"""The proxy's upstreams must match where each service actually listens.

deploy/Caddyfile names an in-container port per service; deploy/compose.yml
names the same port as the container side of its `ports:` mapping. Nothing
connects them, and a drift is invisible in the worst way: Caddy starts fine,
the container is healthy, and the subdomain returns 502.

The overlay has the matching hazard. Behind the proxy, HTTP_HOST/HTTP_PORT must
describe the subdomain on the proxy's port, because Magento, Osclass and GitLab
bake that address into every URL they serve. A service left at its direct-mode
address answers through the proxy while emitting links to the wrong place —
a 200 the whole way, and broken to a user.

Reads the real deploy files rather than fixtures, for the reason given in
test_service_ports.py: a fixture cannot go stale the way the thing being
guarded does.
"""
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
BASE = DEPLOY / "compose.yml"
OVERLAY = DEPLOY / "compose.proxy.yml"
CADDYFILE = DEPLOY / "Caddyfile"


def split_ports(mapping: str) -> list[str]:
    """Split a compose ports string on ':' — ignoring ':' inside ${...}.

    "${BIND_ADDR:-0.0.0.0}:${GITLAB_PORT:-8023}:${GITLAB_PORT:-8023}" has six
    colons and three fields; a plain split gets this wrong in exactly the case
    that matters, since the default-value syntax is itself colon-bearing.
    """
    fields, buf, depth = [], "", 0
    for ch in mapping:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == ":" and depth == 0:
            fields.append(buf)
            buf = ""
        else:
            buf += ch
    fields.append(buf)
    return fields


def container_port(mapping: str) -> str:
    """The container side of a ports mapping — always the last field."""
    return split_ports(str(mapping))[-1]


BASE_SERVICES = yaml.safe_load(BASE.read_text())["services"]
OVERLAY_SERVICES = yaml.safe_load(OVERLAY.read_text())["services"]
CADDY = CADDYFILE.read_text()

# http://shopping.{$BENCH_HOST} { reverse_proxy shopping:7770 }
SITES = dict(
    re.findall(
        r"^http://([\w.-]+)\.\{\$BENCH_HOST\}\s*\{\s*\n\s*reverse_proxy\s+(\S+)",
        CADDY,
        re.M,
    )
)

NAMES = sorted(BASE_SERVICES)


def test_the_fleet_was_discovered():
    # A parse bug that found nothing would make every test below vacuous, and a
    # vacuous suite is indistinguishable from a passing one.
    assert len(NAMES) == 8, f"expected 8 services in {BASE.name}, got {NAMES}"
    assert len(SITES) == 8, f"expected 8 proxied sites in Caddyfile, got {SITES}"


@pytest.mark.parametrize("name", NAMES)
def test_every_service_has_a_subdomain(name):
    assert name in SITES, (
        f"{name} is in {BASE.name} but has no http://{name}.{{$BENCH_HOST}} site "
        f"in {CADDYFILE.name}, so it is unreachable through the proxy.")


@pytest.mark.parametrize("name", NAMES)
def test_upstream_targets_the_container_listen_port(name):
    # Brace-aware, for the same reason split_ports is: gitlab's upstream is
    # `gitlab:{$PROXY_PORT:80}`, whose last colon is inside the default value.
    upstream_host, upstream_port = split_ports(SITES[name])
    assert upstream_host == name, (
        f"{CADDYFILE.name} proxies {name} to host '{upstream_host}', which is "
        f"not the compose service name — Docker's DNS will not resolve it.")
    expected = container_port(BASE_SERVICES[name]["ports"][0])
    if "${" in expected:
        # gitlab: its in-container listener follows HTTP_PORT, so the upstream
        # must track PROXY_PORT rather than name a fixed number.
        assert upstream_port == "{$PROXY_PORT:80}", (
            f"{name}'s listen port follows HTTP_PORT, so the Caddyfile upstream "
            f"must be {{$PROXY_PORT:80}}, not '{upstream_port}'.")
    else:
        assert upstream_port == expected, (
            f"{CADDYFILE.name} proxies {name} to port {upstream_port} but "
            f"{BASE.name} shows it listening on {expected} in-container. "
            f"The subdomain would return 502 against a healthy container.")


@pytest.mark.parametrize("name", NAMES)
def test_overlay_moves_the_public_address_to_the_subdomain(name):
    env = OVERLAY_SERVICES[name]["environment"]
    assert env["HTTP_HOST"].startswith(f"{name}.${{BENCH_HOST"), (
        f"{OVERLAY.name} leaves {name}'s HTTP_HOST as '{env['HTTP_HOST']}'. "
        f"Behind the proxy the address clients use is {name}.$BENCH_HOST, and "
        f"this value is what the image bakes into every URL it serves.")
    assert env["HTTP_PORT"] == "${PROXY_PORT:-80}", (
        f"{OVERLAY.name} leaves {name}'s HTTP_PORT as '{env['HTTP_PORT']}'. "
        f"Behind the proxy the port clients use is the proxy's, not the "
        f"container's own published port.")


def test_overlay_declares_no_ports_for_backends():
    """Compose merges `ports:` additively — an overlay cannot remove one.

    So the overlay must not restate them: a second mapping for the same service
    is appended, not substituted, and the two collide at bind time.
    """
    for name in NAMES:
        assert "ports" not in OVERLAY_SERVICES[name], (
            f"{OVERLAY.name} restates ports for {name}. Compose appends rather "
            f"than replaces, so this collides with {BASE.name}'s mapping.")


def test_unmatched_hosts_get_an_error_not_a_blank_200():
    """Caddy answers an unmatched host with an empty 200 by default.

    Measured, not assumed: before the catch-all existed, `Host: nope.<domain>`
    returned `HTTP/1.1 200 OK` with `Content-Length: 0`. That is the worst
    available answer — a typo'd subdomain, or a stray DNS record pointing here,
    looks like a service that is up and serving an empty page rather than a
    name that is not published.
    """
    assert re.search(r"^http://:80\s*\{", CADDY, re.M), (
        f"{CADDYFILE.name} has no `http://:80` catch-all, so an unmatched host "
        f"falls through to Caddy's default empty 200.")
    # Split on a closing brace in column 0, not any "}": the respond body
    # contains {$BENCH_HOST}, whose brace would end the slice early.
    catchall = CADDY.split("http://:80", 1)[1].split("\n}", 1)[0]
    assert re.search(r"\brespond\b.*\b404\b", catchall), (
        f"the `http://:80` catch-all in {CADDYFILE.name} must respond 404; an "
        f"unpublished hostname is not a success.")


@pytest.mark.parametrize("name", NAMES)
def test_index_lists_every_service(name):
    """The bare domain serves an index; a service missing from it is invisible.

    This is a completeness check only. It does NOT guard the config's syntax:
    a stray backtick placed after the list still truncates the body somewhere
    this assertion cannot see, and that was measured, not assumed — the case
    passed while the real Caddy refused the file. Parsing is checked for real
    in tests/integration/test_deploy_caddyfile.py.
    """
    body = re.search(r"respond `(.*?)`", CADDY, re.S)
    assert body, f"{CADDYFILE.name} has no backtick-quoted respond body"
    assert f"http://{name}." in body.group(1), (
        f"the index in {CADDYFILE.name} does not list {name}. Either the entry "
        f"is missing, or a stray backtick truncated the body before it — which "
        f"also stops Caddy starting at all.")


def test_proxy_is_only_in_the_overlay():
    assert "proxy" not in BASE_SERVICES, (
        f"the proxy belongs in {OVERLAY.name}; {BASE.name} is the direct-port "
        f"deployment and must stand alone.")
    assert "proxy" in OVERLAY_SERVICES
