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

# A service may have more than one subdomain, because a service may have more
# than one listener: map-osrm serves car, bike and foot from one container and
# selects the profile by port alone. So a site is matched to its service by the
# upstream it names, never by its own name.
SITES_BY_SERVICE = {}
for site, upstream in SITES.items():
    SITES_BY_SERVICE.setdefault(split_ports(upstream)[0], {})[site] = upstream


def bakes_its_address(name: str) -> bool:
    """Whether this service's image reads HTTP_HOST, read off the image itself.

    The proxy overlay exists to correct a baked-in address, so the services it
    must cover are exactly the ones that bake one. Derived rather than listed:
    a hand-kept list is one more thing to forget when an image is added.
    """
    image = BASE_SERVICES[name]["image"]
    repo, _, _ = image.rpartition(":")
    benchmark, _, service = (repo or image).rsplit("/", 1)[-1].partition("-")
    directory = REPO / "images" / benchmark / service
    assert directory.is_dir(), (
        f"{name}'s image {image!r} does not name an images/*/*/ directory")
    return any("HTTP_HOST" in f.read_text(errors="ignore")
               for f in directory.rglob("*") if f.is_file())


ADDRESSED = sorted(n for n in NAMES if bakes_its_address(n))
ADDRESSLESS = sorted(n for n in NAMES if n not in ADDRESSED)


def test_the_fleet_was_discovered():
    # A parse bug that found nothing would make every test below vacuous, and a
    # vacuous suite is indistinguishable from a passing one.
    assert NAMES, f"no services parsed out of {BASE.name}"
    assert SITES, f"no proxied sites parsed out of {CADDYFILE.name}"
    assert ADDRESSED and ADDRESSLESS, (
        "the fleet no longer has both kinds of service, so the split this file "
        "tests against has stopped meaning anything")
    assert set(SITES_BY_SERVICE) <= set(NAMES), (
        f"{CADDYFILE.name} proxies to {sorted(set(SITES_BY_SERVICE) - set(NAMES))}, "
        f"which {BASE.name} does not define — Docker's DNS will not resolve it.")


@pytest.mark.parametrize("name", NAMES)
def test_every_listener_has_a_subdomain(name):
    """One subdomain per published listener, not per service.

    map-osrm is why this counts listeners: its three routing profiles differ by
    port and by nothing else, so a service-shaped check passes on car while bike
    and foot are unreachable through the proxy and nothing says so.
    """
    sites = SITES_BY_SERVICE.get(name, {})
    assert sites, (
        f"{name} is in {BASE.name} but nothing in {CADDYFILE.name} proxies to it, "
        f"so it is unreachable through the proxy.")
    proxied = {split_ports(u)[1] for u in sites.values()}
    listening = {container_port(m) for m in BASE_SERVICES[name]["ports"]}
    if any("${" in port for port in listening):
        # gitlab: its in-container listener follows HTTP_PORT, so the upstream
        # must track PROXY_PORT rather than name a fixed number.
        assert proxied == {"{$PROXY_PORT:80}"}, (
            f"{name}'s listen port follows HTTP_PORT, so the Caddyfile upstream "
            f"must be {{$PROXY_PORT:80}}, not {sorted(proxied)}.")
        return
    assert proxied == listening, (
        f"{CADDYFILE.name} proxies {name} to {sorted(proxied)} and {BASE.name} "
        f"shows it listening on {sorted(listening)} in-container. A port in one "
        f"and not the other is either a 502 against a healthy container or a "
        f"listener nothing can reach.")


@pytest.mark.parametrize("name", ADDRESSED)
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


@pytest.mark.parametrize("name", ADDRESSLESS)
def test_the_overlay_leaves_addressless_services_alone(name):
    """A service that bakes no address has nothing for the overlay to correct.

    The map back ends serve tiles, routes and geocoding results — no page, no
    links, no address anywhere in what they emit. An overlay block for one would
    set HTTP_HOST and HTTP_PORT on a container that reads neither, which reads
    as a contract the image honours and it does not.
    """
    assert name not in OVERLAY_SERVICES, (
        f"{OVERLAY.name} sets an environment for {name}, whose image reads no "
        f"HTTP_HOST. Proxying it is {CADDYFILE.name}'s job and needs no override.")


def test_overlay_declares_no_ports_for_backends():
    """Compose merges `ports:` additively — an overlay cannot remove one.

    So the overlay must not restate them: a second mapping for the same service
    is appended, not substituted, and the two collide at bind time.
    """
    # The proxy is the one service the overlay introduces rather than overrides,
    # and publishing its own port is the whole point of it.
    for name in set(OVERLAY_SERVICES) & set(BASE_SERVICES):
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


@pytest.mark.parametrize("name", sorted(SITES))
def test_index_lists_every_site(name):
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
