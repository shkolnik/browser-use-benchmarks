"""gitlab's arming script: when boot is over, and what happens when it never is.

`arm-services.sh` is the half of the #73 contract that decides WHEN a dead
service starts killing the container (runit-finish.sh, tested next door, decides
what happens once it does). Two things have to be right, and they pull in
opposite directions:

  arm too early   boot's own exits are read as crashes — sidekiq exits 1 while
                  settling, and reconfigure restarts puma, nginx and gitlab-kas,
                  each exiting 0 because they handle SIGTERM
  arm too late    a stack that never comes up is never anybody's problem

The second is what #85 was. The script used to arm anyway after 15 minutes and
log a warning, so a service that never STARTED — as opposed to one that started
and died — produced no exit for the hooks to catch, and the container sat there.
The worked example is #84's port collision: twenty minutes of restart loop,
reporting `starting` for most of it, describing a stack that could never come
up. Now the timeout is itself a failure, and it names what is down.

Fake `gitlab-ctl` and `curl` on PATH, and the loop's two constants turned down
from 90 x 10s: this exercises the shipped script in milliseconds. A booted-gitlab
test costs ~2.5 minutes and could not produce "nginx never starts" on demand.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parent.parent
          / "images" / "webarena" / "gitlab" / "arm-services.sh")

UP = "run: puma: (pid 100) 42s; run: log: (pid 101) 42s\n"
CHURNING = "run: puma: (pid @N@) 2s; run: log: (pid 101) 42s\n"
DOWN = "down: puma: 1s, normally up; run: log: (pid 101) 42s\n"


@pytest.fixture
def armenv(tmp_path):
    """Run the real script against a fake gitlab, with /run redirected."""
    run = tmp_path / "run"
    run.mkdir()
    bin_ = tmp_path / "bin"
    bin_.mkdir()

    (bin_ / "gitlab-ctl").write_text(
        "#!/bin/sh\n"
        f"n=$(cat {tmp_path}/n 2>/dev/null || echo 0); n=$((n+1)); echo $n > {tmp_path}/n\n"
        f"sed \"s/@N@/$n/g\" {tmp_path}/status\n")
    # Serving or not is a file, so a test can say "nothing ever answered".
    (bin_ / "curl").write_text(
        f"#!/bin/sh\n[ -e {tmp_path}/serving ] || exit 22\n")
    for f in ("gitlab-ctl", "curl"):
        (bin_ / f).chmod(0o755)

    def call(status=UP, serving=True, tries=3, already_failed=None):
        (tmp_path / "status").write_text(status)
        if serving:
            (tmp_path / "serving").touch()
        if already_failed:
            (run / "gitlab-service-failed").write_text(already_failed)

        victim = subprocess.Popen(["sleep", "30"])
        try:
            body = SCRIPT.read_text().replace("/run/", f"{run}/")
            script = tmp_path / "arm-services.sh"
            script.write_text(body)
            script.chmod(0o755)
            p = subprocess.run(
                ["bash", str(script), "8023"], capture_output=True, text=True,
                timeout=60,
                env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}",
                     "ARM_TRIES": str(tries), "ARM_SLEEP": "0",
                     "SUPERVISOR_PID": str(victim.pid)})
            time.sleep(0.2)
            signalled = victim.poll() is not None
        finally:
            if victim.poll() is None:
                victim.kill()
            victim.wait()

        sentinel = run / "gitlab-service-failed"
        return {
            "proc": p,
            "signalled": signalled,
            "armed": (run / "gitlab-services-armed").exists(),
            "sentinel": sentinel.read_text() if sentinel.exists() else None,
        }

    return call


def test_the_script_exists():
    assert SCRIPT.is_file(), f"{SCRIPT} is missing; every test below would be vacuous"


def test_a_settled_stack_arms_the_contract(armenv):
    r = armenv(status=UP, serving=True)
    assert r["armed"], r["proc"].stderr
    assert r["proc"].returncode == 0
    assert not r["signalled"], "a healthy boot killed the container"
    assert r["sentinel"] is None


def test_a_service_that_never_starts_fails_the_container(armenv):
    """#85: the case that used to be a warning. nginx down => nothing answers."""
    r = armenv(status=DOWN, serving=False)
    assert r["signalled"], (
        "the container was left running with a service that never started — "
        "this is exactly the state #85 removed")
    assert not r["armed"], (
        "armed on the way out: the shutdown's own exits can then overwrite the "
        "sentinel that explains the failure")
    assert r["sentinel"] and "never finished starting" in r["sentinel"], r["sentinel"]


def test_the_failure_names_what_is_down(armenv):
    """An operator reading the exit needs the culprit, not just the verdict."""
    r = armenv(status=DOWN, serving=False)
    assert "puma" in r["sentinel"], r["sentinel"]
    assert "nothing answered" in r["sentinel"], r["sentinel"]
    assert "FATAL" in r["proc"].stderr, r["proc"].stderr


def test_serving_but_never_quiescing_is_reported_as_such(armenv):
    """A different problem from a dead port, and the message must not conflate them."""
    r = armenv(status=CHURNING, serving=True)
    assert r["signalled"] and not r["armed"]
    assert "it served" in r["sentinel"], r["sentinel"]
    assert "nothing answered" not in r["sentinel"], r["sentinel"]


def test_a_down_service_blocks_arming_even_while_serving(armenv):
    """nginx can answer while the app behind it is gone — #84 in one line."""
    r = armenv(status=DOWN, serving=True)
    assert not r["armed"], "armed with a service down"
    assert r["signalled"]
    assert "puma" in r["sentinel"], r["sentinel"]


def test_an_earlier_crash_keeps_the_sentinel(armenv):
    """First failure wins, same rule as the finish hook: a real crash explains
    the timeout better than the timeout explains itself."""
    r = armenv(status=DOWN, serving=False,
               already_failed="puma exited with code=7 signal=0\n")
    assert "code=7" in r["sentinel"], r["sentinel"]
    assert "never finished starting" not in r["sentinel"], r["sentinel"]
    assert r["signalled"], "still has to take the container down"


def test_the_sentinel_is_shaped_for_pid_1s_parser(armenv):
    """entrypoint.sh's on_term reads code=/signal= out of this file to choose the
    container's exit status; a sentinel it cannot parse exits 1 by accident
    rather than on purpose."""
    r = armenv(status=DOWN, serving=False)
    assert "code=1" in r["sentinel"] and "signal=0" in r["sentinel"], r["sentinel"]
    assert r["sentinel"].count("\n") == 1, "on_term cats this into one log line"
