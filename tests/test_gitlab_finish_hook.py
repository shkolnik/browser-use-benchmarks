"""gitlab's runit `finish` hook: the decision table, without booting gitlab.

gitlab is the one image that keeps its upstream supervisor (runit, from the
omnibus base, which gitlab-ctl and `gitlab-ctl reconfigure` are built on), so it
implements the #73 contract with runit's per-service hook instead of
run-services.sh. Everything the hook decides comes from four inputs — the exit
code, the signal, whether the stack is armed, and whether a shutdown or an
earlier failure is already in progress — and every one of those branches was
put there by a measured failure of the previous version:

  unarmed          boot has real exits: sidekiq exits 1 while settling, and
                   reconfigure restarts puma/nginx/gitlab-kas
  signal 15        an explicit stop
  stopping         `gitlab-ctl stop` makes graceful services exit 0, which
                   without this reads as a crash and turned `docker stop` into
                   exit 1 instead of 143
  already failed   the same shutdown overwrote the sentinel, so the container
                   reported the wrong exit code and lost the first cause

A booted-gitlab test costs ~2.5 minutes per boot; this costs milliseconds and
covers the branches. The live proof that the whole thing works end to end lives
in the PR description, not here.
"""
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "images" / "webarena" / "gitlab" / "runit-finish.sh"


@pytest.fixture
def hookenv(tmp_path, monkeypatch):
    """Run the hook with /run redirected into tmp_path and a real pid to signal."""
    svcdir = tmp_path / "sv" / "puma"
    svcdir.mkdir(parents=True)
    run = tmp_path / "run"
    run.mkdir()

    # A live process standing in for PID 1, so a hook that decides to fail the
    # container has something to signal that is not this machine's init.
    victim = subprocess.Popen(["sleep", "30"])

    def call(code="-1", sig="9", armed=False, stopping=False, failed=False):
        if armed:
            (run / "gitlab-services-armed").touch()
        if stopping:
            (run / "gitlab-stopping").touch()
        if failed:
            (run / "gitlab-service-failed").write_text("earlier exited with code=7 signal=0\n")
        body = HOOK.read_text().replace("/run/", f"{run}/")
        script = tmp_path / "finish"
        script.write_text(body)
        script.chmod(0o755)
        p = subprocess.run(["sh", str(script), code, sig], cwd=svcdir,
                           capture_output=True, text=True,
                           env={**os.environ, "SUPERVISOR_PID": str(victim.pid)})
        time.sleep(0.2)
        signalled = victim.poll() is not None
        sentinel = (run / "gitlab-service-failed")
        return p, signalled, (sentinel.read_text() if sentinel.exists() else None)

    yield call
    if victim.poll() is None:
        victim.kill()
    victim.wait()


def test_the_hook_exists():
    assert HOOK.is_file(), f"{HOOK} is missing; every test below would be vacuous"


def test_a_crash_after_arming_fails_the_container(hookenv):
    p, signalled, sentinel = hookenv(code="-1", sig="9", armed=True)
    assert signalled, "PID 1 was not signalled, so the container would keep running"
    assert sentinel and "signal=9" in sentinel, sentinel
    assert "FATAL" in p.stderr, p.stderr


def test_an_exit_during_boot_is_forgiven(hookenv):
    """sidekiq really does exit 1 once while a normal boot settles."""
    p, signalled, sentinel = hookenv(code="1", sig="0", armed=False)
    assert not signalled, "a boot-time exit took the container down"
    assert sentinel is None
    assert "during boot" in p.stderr, p.stderr


def test_sigterm_after_arming_is_not_a_failure(hookenv):
    p, signalled, sentinel = hookenv(code="-1", sig="15", armed=True)
    assert not signalled and sentinel is None


def test_a_clean_exit_during_shutdown_is_not_a_failure(hookenv):
    """`gitlab-ctl stop`: graceful services exit 0, which is not a crash."""
    p, signalled, sentinel = hookenv(code="0", sig="0", armed=True, stopping=True)
    assert not signalled, "shutdown was reported as a service failure"
    assert sentinel is None


def test_the_first_failure_wins(hookenv):
    """A later exit must not overwrite the one that explains the failure."""
    p, signalled, sentinel = hookenv(code="0", sig="0", armed=True, failed=True)
    assert sentinel is not None and "code=7" in sentinel, (
        f"the original cause was overwritten: {sentinel!r}")
    assert not signalled


def test_a_clean_exit_after_arming_still_fails_the_container(hookenv):
    """Gone is gone: a service that exits 0 on its own is still missing."""
    p, signalled, sentinel = hookenv(code="0", sig="0", armed=True)
    assert signalled, "a service exiting 0 left the container up and degraded"
    assert sentinel and "code=0" in sentinel
