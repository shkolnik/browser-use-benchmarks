"""A HEALTHCHECK must not assume what HTTP_HOST is set to.

Found on a real deployment, not in CI, and CI could not have found it: the
smoke gate sets `HTTP_HOST: 127.0.0.1`, and shopping-admin's HEALTHCHECK curled
`http://127.0.0.1:7780/admin`. The probe and the only environment it ever ran in
agreed by construction. Point the same image at any other hostname — which is
the entire purpose of HTTP_HOST — and Magento's adminhtml, pinned to the
configured base_url, falls through to the frontend area and 404s. The container
reports unhealthy forever while serving every real client correctly.

Two invariants, both static over the real Dockerfiles:

  * a probe CONNECTS over loopback, because nginx's listen port is fixed inside
    the image and does not follow HTTP_PORT (dialing ${HTTP_HOST} instead would
    leave the container's readiness depending on DNS and on egress back into
    itself);
  * a probe against a host-pinned app PRESENTS ${HTTP_HOST}, because that is the
    name the app was configured to answer to.

Connect to the address that is fixed; send the name that is not.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMAGES = REPO / "images"

# Apps that serve any Host answer a loopback probe whichever name it sends, so
# the header is optional for them. Two are pinned to their configured address,
# both measured in-container:
#
#   webarena/shopping-admin  Magento adminhtml, pinned to base_url.
#                            Host=127.0.0.1 -> 404, Host=$HTTP_HOST -> 200.
#   vwa/classifieds          Osclass, pinned to WEB_PATH. It 302s EVERY request
#                            whose Host does not match, so the probe saw a
#                            0-byte redirect, not a page.
#                            no Host -> 302, Host=$HTTP_HOST -> 200 (31,721 B).
#
# The storefront (webarena/shopping) is NOT pinned: redirect_to_base=0 makes it
# serve any Host, which is why it stayed healthy on the same deployment that
# broke the admin and classifieds.
HOST_PINNED = {"webarena/shopping-admin", "vwa/classifieds"}


def _healthcheck_curls():
    """(image, command) for every Dockerfile HEALTHCHECK, joined across \\ lines."""
    found = {}
    for dockerfile in sorted(IMAGES.glob("*/*/Dockerfile")):
        text = re.sub(r"\\\n\s*", " ", dockerfile.read_text())
        for line in text.splitlines():
            if line.startswith("HEALTHCHECK") and "curl" in line:
                found[str(dockerfile.parent.relative_to(IMAGES))] = line
    return found


def test_discovery_found_the_healthchecks():
    # Every assertion below is over a dict comprehension; an empty one passes
    # them all while checking nothing.
    found = _healthcheck_curls()
    assert len(found) >= 6, f"HEALTHCHECK discovery found only {sorted(found)}"


def test_every_probe_connects_over_loopback():
    dialed_out = {
        img: cmd for img, cmd in _healthcheck_curls().items()
        if not re.search(r"https?://(127\.0\.0\.1|localhost)[:/]", cmd)}
    assert not dialed_out, (
        f"HEALTHCHECK(s) not connecting over loopback: {sorted(dialed_out)}. The "
        f"listen port inside the image is fixed and does not follow HTTP_PORT, so "
        f"a probe dialing ${{HTTP_HOST}}:${{HTTP_PORT}} reaches a port nothing is "
        f"bound to — and makes readiness depend on DNS resolving the published "
        f"name from inside the container.")


def test_host_pinned_apps_are_probed_under_their_configured_name():
    missing = sorted(
        img for img, cmd in _healthcheck_curls().items()
        if img in HOST_PINNED and "${HTTP_HOST}" not in cmd)
    assert not missing, (
        f"{missing} probe(s) a host-pinned app without presenting ${{HTTP_HOST}}. "
        f"Add -H \"Host: ${{HTTP_HOST}}\": a literal hostname here only ever passes "
        f"under the one HTTP_HOST that happens to match it, so the container goes "
        f"permanently unhealthy on every other deployment while serving correctly.")
