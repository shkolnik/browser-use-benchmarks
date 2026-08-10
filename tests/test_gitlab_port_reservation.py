"""gitlab reserves one port for puma, and refuses HTTP_PORT early if it is that.

GitLab is the one image in the fleet whose nginx listener follows HTTP_PORT
(every other image has a fixed internal port and moves only the published side).
Behind it, puma binds a TCP port of its own on the loopback, so whatever puma
holds cannot also be the published port: both would aim at the same bind.

Omnibus's default for puma is 8080 — the second most common HTTP port there is.
Rather than declare that off-limits, the build moves puma to an unremarkable
port and the entrypoint reserves THAT, reading it back from gitlab.rb. Two
halves that must agree, so both are covered here.

What the collision costs when it is not caught, measured on the published image
with the default still in place:

    nginx: (pid 1117) 134s -> 159s -> 185s      stable, won the bind
    puma:  (pid 1428) 13s -> (1506) 3s -> (1552) 12s   new pid every few seconds
    12 x Errno::EADDRINUSE bind(2) for "127.0.0.1" port 8080 in four minutes

and, because the stack never settles, the entrypoint's arming fallback fires at
15 minutes, after which the next puma exit takes the container down under the
#73 contract. It then restarts and does it again. Twenty minutes of restart loop
to report a misconfiguration knowable before anything binds — and for most of
that window the container reports `starting`, so it reads as a slow boot.

The check reads puma's port out of gitlab.rb instead of hardcoding 8080, so it
cannot quietly become a lie if the base image moves it. These tests cover both:
the collision is refused, another port is not, and the refusal follows the
config rather than a constant.
"""
import re
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = (Path(__file__).resolve().parent.parent
              / "images" / "webarena" / "gitlab" / "entrypoint.sh")

# The entrypoint goes on to boot omnibus, which is not present here, so every
# run fails eventually. These tests are about WHICH failure comes first: the
# port check must fire before anything else, which is the whole point of it.
COLLISION = "collides with GitLab's internal puma port"


def run_entrypoint(tmp_path, http_port, gitlab_rb="external_url 'http://old:8023'\n"):
    rb = tmp_path / "gitlab.rb"
    rb.write_text(gitlab_rb)
    return subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env={"PATH": "/usr/bin:/bin", "HTTP_HOST": "gitlab.example.com",
             "HTTP_PORT": str(http_port), "GITLAB_RB": str(rb)},
        capture_output=True, text=True, timeout=60)


def test_the_reserved_port_is_refused(tmp_path):
    r = run_entrypoint(tmp_path, 8080)
    assert r.returncode != 0
    assert COLLISION in r.stderr, r.stderr
    assert "8080" in r.stderr


def test_the_refusal_names_a_way_out(tmp_path):
    # An error an operator cannot act on is only half an error: this one is
    # reached through PROXY_PORT behind the proxy overlay, which is not
    # obviously the same knob as HTTP_PORT unless the message says so.
    r = run_entrypoint(tmp_path, 8080)
    assert "PROXY_PORT" in r.stderr, r.stderr


def test_another_port_is_not_refused(tmp_path):
    # Fails later for want of an omnibus install; it must not fail HERE.
    r = run_entrypoint(tmp_path, 8023)
    assert COLLISION not in r.stderr, r.stderr


RESTORE = (Path(__file__).resolve().parent.parent
           / "images" / "webarena" / "gitlab" / "restore-stage.sh")

# Ports a deployment plausibly wants to publish. Reserving one of these to hide
# an internal detail is the wrong trade — that is why puma moves rather than
# 8080 being declared off-limits.
COMMONLY_PUBLISHED = {"80", "443", "8000", "8008", "8080", "8081", "8443",
                      "3000", "5000", "9000"}


def test_the_build_moves_puma_off_the_common_ports():
    m = re.search(r"^PUMA_PORT=(\d+)", RESTORE.read_text(), re.M)
    assert m, "restore-stage.sh no longer sets PUMA_PORT"
    assert m.group(1) not in COMMONLY_PUBLISHED, (
        f"puma was moved to {m.group(1)}, which is a port people publish. The "
        f"point of moving it is that HTTP_PORT can be anything ordinary.")


def test_the_build_verifies_the_port_actually_took():
    # Setting puma['port'] and reconfiguring is not proof it moved. If the
    # rendered config still said 8080, the entrypoint would reserve a port puma
    # is not on and admit the one it is — the collision back, now invisible.
    body = RESTORE.read_text()
    assert "puma.rb" in body and "${PUMA_PORT}" in body, (
        "restore-stage.sh must assert the RENDERED puma config binds PUMA_PORT")


def test_the_reserved_port_follows_gitlab_rb_not_a_constant(tmp_path):
    # If the base image ever moves puma, a hardcoded 8080 would refuse the wrong
    # port and wave the colliding one through — the check would still pass its
    # own tests while protecting nothing.
    moved = "external_url 'http://old:8023'\nputs = 1\npuma['port'] = 9292\n"
    assert COLLISION in run_entrypoint(tmp_path, 9292, moved).stderr
    assert COLLISION not in run_entrypoint(tmp_path, 8080, moved).stderr
