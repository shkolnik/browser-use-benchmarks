"""The shared service supervisor: any service that exits fails the container.

These run the REAL builder/stage-lib/run-services.sh against fake services, in
bash, on this machine — no docker and no image needed. The library is ~40 lines
of shell standing in for supervisord across four images, and its whole job is
to get one thing right (a dead service must take the container down with it, at
the right exit code), so it is worth testing directly rather than only through
a 45G image build.
"""
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "builder" / "stage-lib" / "run-services.sh"


def run(body: str, timeout: int = 20, send_term_after: float = 0.0):
    """Source the library, run `body`, return the CompletedProcess."""
    script = f". {LIB}\n{body}\n"
    if not send_term_after:
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=timeout)
    p = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        p.wait(timeout=send_term_after)
    except subprocess.TimeoutExpired:
        p.terminate()
    out, err = p.communicate(timeout=timeout)
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)


def test_library_exists_and_is_sourceable():
    # Everything below is vacuous if the path is wrong: a missing file makes
    # `. <lib>` fail and every "did not crash" assertion trivially true.
    assert LIB.is_file(), f"{LIB} is missing"
    r = run("svc_status")
    assert r.returncode == 0, r.stderr


def test_a_crashed_service_takes_the_container_down_with_its_own_code():
    r = run("""
      svc_start steady -- sleep 30
      svc_start crasher -- bash -c 'sleep 0.2; exit 78'
      svc_supervise
    """)
    assert r.returncode == 78, f"expected the service's own 78, got {r.returncode}"
    assert "crasher" in r.stderr and "exited 78" in r.stderr, r.stderr


def test_a_service_that_exits_zero_still_fails_the_container():
    """0 would read as a clean `docker stop`; the service is gone either way."""
    r = run("""
      svc_start quitter -- bash -c 'sleep 0.2; exit 0'
      svc_supervise
    """)
    assert r.returncode == 1, f"expected 1, got {r.returncode}"
    assert "exited 0" in r.stderr, r.stderr


def test_a_service_killed_by_a_signal_reports_that_signal():
    r = run("""
      svc_start victim -- bash -c 'kill -9 $$'
      svc_supervise
    """)
    assert r.returncode == 137, f"expected 128+9, got {r.returncode}"
    assert "signal 9" in r.stderr, r.stderr


def test_healthy_services_keep_the_container_running():
    """The failure mode that would make every other test here meaningless."""
    with pytest.raises(subprocess.TimeoutExpired):
        run("""
          svc_start a -- sleep 30
          svc_start b -- sleep 30
          svc_supervise
        """, timeout=3)


def test_an_intentional_stop_is_not_a_crash():
    """What a restore stage does: stop a service by hand, keep working."""
    r = run("""
      svc_start nginx -- sleep 30
      svc_start db -- sleep 30
      svc_stop nginx
      echo "still here"
      svc_status
      svc_stop_all
      echo "clean exit"
    """)
    assert r.returncode == 0, f"{r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "still here" in r.stdout and "clean exit" in r.stdout
    assert "nginx: NOT RUNNING" in r.stdout, r.stdout
    assert "db: RUNNING" in r.stdout, r.stdout


def test_sigterm_stops_services_and_exits_143():
    """`docker stop` on a healthy container is not a failure."""
    r = run("""
      svc_start a -- sleep 30
      svc_supervise
    """, send_term_after=1.0)
    assert r.returncode == 143, f"expected 143, got {r.returncode}"
    assert "received SIGTERM" in r.stdout, r.stdout


# The orphan case — grandchildren reparenting to the supervisor — cannot happen
# here, because outside a container this bash is not PID 1 and orphans go to
# init instead. Testing it locally would assert nothing. It lives in
# tests/integration/test_run_services_pid1.py, where the supervisor really is
# PID 1.


def test_log_option_redirects_without_a_pipe(tmp_path):
    """--log must be a redirect: a pipe would make $! the pipe's last command."""
    logfile = tmp_path / "svc.log"
    r = run(f"""
      svc_start noisy --log {logfile} -- bash -c 'echo hello-from-service; sleep 0.2; exit 9'
      svc_supervise
    """)
    assert r.returncode == 9, f"exit code came from the wrong process: {r.returncode}"
    assert "hello-from-service" in logfile.read_text()


def test_stop_all_runs_in_reverse_start_order():
    r = run("""
      svc_start first -- sleep 30
      svc_start second -- sleep 30
      svc_stop_all
    """)
    assert r.returncode == 0, r.stderr
    order = [ln.split()[-1] for ln in r.stdout.splitlines() if ln.startswith("run-services: stopped")]
    assert order == ["second", "first"], f"stopped in {order}, expected reverse start order"


def test_it_works_under_set_e_like_every_entrypoint_that_sources_it():
    """Every image's entrypoint runs `set -eu`, so the library must survive it.

    Without care, errexit kills the shell the moment `wait -n` reports a
    non-zero service — the exit code would still be right, but the log line
    naming the dead service, and the shutdown of the survivors, would both be
    skipped. That is a silent loss of exactly the diagnosis this exists for.
    """
    r = run("""
      set -eu
      svc_start steady -- sleep 30
      svc_start crasher -- bash -c 'sleep 0.2; exit 66'
      svc_supervise
    """)
    assert r.returncode == 66, f"expected 66, got {r.returncode}"
    assert "crasher" in r.stderr, (
        "errexit swallowed the diagnosis: the container died with the right "
        f"code but never said what happened\nstderr: {r.stderr!r}")


def test_a_privilege_tool_that_exists_but_does_not_work_is_skipped(tmp_path):
    """Alpine's busybox `setpriv` rejects --reuid; being on PATH proves nothing.

    Measured in the reddit image: `command -v setpriv` succeeds there and the
    program then refuses every option this library passes it. Picking a tool by
    existence would have broken every privilege-dropping service in that image.
    """
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "setpriv").write_text("#!/bin/sh\necho 'setpriv: unrecognized option' >&2\nexit 1\n")
    (fake / "setpriv").chmod(0o755)
    # runuser stands in for the tool that does work, and records that it ran.
    (fake / "runuser").write_text('#!/bin/sh\nshift 2; shift; exec "$@"\n')
    (fake / "runuser").chmod(0o755)
    r = run(f"""
      export PATH={fake}:/usr/bin:/bin
      svc_start svc --user $(id -un) -- bash -c 'echo ran-anyway; sleep 0.2; exit 5'
      svc_supervise
    """)
    assert r.returncode == 5, f"broken setpriv was not skipped: {r.returncode}\n{r.stderr}"
    assert "ran-anyway" in r.stdout, r.stdout


def test_an_unknown_user_fails_loudly_at_startup():
    """Better a refusal at boot than a service that silently never starts."""
    r = run("""
      svc_start svc --user definitely-no-such-user-4b2f -- sleep 30
      svc_supervise
    """)
    assert r.returncode != 0
    assert "definitely-no-such-user-4b2f" in r.stderr, r.stderr


def test_supervise_refuses_when_nothing_was_started():
    """Silently waiting forever with no services would look identical to health."""
    r = run("svc_supervise")
    assert r.returncode != 0
    assert "no services" in r.stderr, r.stderr
