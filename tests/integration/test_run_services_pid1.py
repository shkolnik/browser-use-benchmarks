"""The service supervisor doing its job as PID 1, in a real container.

tests/test_run_services.py covers the library's logic in plain bash on the host.
Three of its properties only exist inside a container and cannot be asserted
there at all:

  * being PID 1, where orphaned grandchildren reparent to the supervisor
    instead of to init;
  * the CONTAINER's exit code, which is what the contract in #73 is actually
    about — a caller sees `docker inspect .State.ExitCode`, not a shell's `$?`;
  * `docker stop`, which sends SIGTERM to PID 1 and waits out a grace period.

Kept in tests/integration for the same reason as the build round trip: it needs
docker and pulls a base image, so a Docker Hub rate limit must not discredit
the fast suite.
"""
import json
import subprocess
import shutil
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or subprocess.run(
        ["docker", "info"], capture_output=True).returncode != 0,
    reason="docker unavailable")

LIB = Path(__file__).resolve().parents[2] / "builder" / "stage-lib" / "run-services.sh"
BASE = "debian:bookworm-slim"


def _run_container(entry_body: str, tmp_path: Path, name: str, stop_after=None):
    """Run the supervisor as PID 1; return (exit_code, logs)."""
    entry = tmp_path / "entry.sh"
    entry.write_text("#!/bin/bash\n. /run-services.sh\n" + entry_body)
    entry.chmod(0o755)
    cid = name + str(int(time.time() * 1000) % 100000)
    subprocess.run(["docker", "run", "-d", "--name", cid,
                    "-v", f"{LIB}:/run-services.sh:ro",
                    "-v", f"{entry}:/entry.sh:ro", BASE, "/entry.sh"],
                   check=True, capture_output=True)
    try:
        if stop_after:
            time.sleep(stop_after)
            subprocess.run(["docker", "stop", "-t", "10", cid],
                           check=True, capture_output=True)
        else:
            subprocess.run(["docker", "wait", cid], check=True,
                           capture_output=True, timeout=60)
        state = json.loads(subprocess.run(
            ["docker", "inspect", cid], check=True, capture_output=True,
            text=True).stdout)[0]["State"]
        logs = subprocess.run(["docker", "logs", cid], capture_output=True,
                              text=True)
        return state["ExitCode"], logs.stdout + logs.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def test_a_crashed_service_exits_the_container_with_its_code(tmp_path):
    code, logs = _run_container("""
      svc_start steady -- sleep 300
      svc_start crasher -- bash -c 'sleep 1; exit 42'
      svc_supervise
    """, tmp_path, "svc-crash")
    assert code == 42, f"container exit code was {code}, not the service's 42\n{logs}"
    assert "crasher" in logs, logs


def test_orphaned_grandchildren_do_not_fail_the_container(tmp_path):
    """At PID 1 the supervisor reaps orphans; reaping is not a service dying.

    Without this, any service that leaves a short-lived child behind — which
    real daemons do constantly — could take the whole container down at random.
    """
    code, logs = _run_container("""
      svc_start orphanmaker -- bash -c 'for i in $(seq 8); do (sleep 0.3; exit 3) & done; sleep 300'
      svc_start crasher -- bash -c 'sleep 3; exit 42'
      svc_supervise
    """, tmp_path, "svc-orph")
    assert code == 42, (
        f"container exited {code}; an orphan's exit status was probably "
        f"mistaken for a service's\n{logs}")


def test_docker_stop_is_a_clean_shutdown_not_a_failure(tmp_path):
    code, logs = _run_container("""
      svc_start a -- sleep 300
      svc_start b -- sleep 300
      svc_supervise
    """, tmp_path, "svc-stop", stop_after=2)
    assert code == 143, f"expected 143 (SIGTERM), got {code}\n{logs}"
    assert "received SIGTERM" in logs, logs
