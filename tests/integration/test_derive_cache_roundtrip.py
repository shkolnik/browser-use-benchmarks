"""Round-trip builder/stage-lib/derive-cache.sh against a real registry.

tests/test_derive_cache_lib.py never touches a network or a real image; it
only asserts on which commands the library invokes, with fake `oras`/`docker`
standing in. This is the test that proves the library actually moves bytes
correctly through both formats, and that a legacy hit really migrates the
registry entry to oras — not just logs a line saying it tried.
"""
import hashlib
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "builder" / "stage-lib" / "derive-cache.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or subprocess.run(
        ["docker", "info"], capture_output=True).returncode != 0,
    reason="docker unavailable")

REGISTRY_NAME = "dcache-test-registry"
REGISTRY_PORT = 5555


@pytest.fixture
def registry():
    # -f first: a stray container from a previous killed run must not make
    # `docker run --name ...` fail before this run even starts.
    subprocess.run(["docker", "rm", "-f", REGISTRY_NAME], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", REGISTRY_NAME,
         "-p", f"127.0.0.1:{REGISTRY_PORT}:5000", "registry:2"],
        capture_output=True, text=True, check=True)
    try:
        yield f"localhost:{REGISTRY_PORT}"
    finally:
        # Runs even on failure (fixture teardown, not a test-body finally) —
        # this container must never outlive the test that started it.
        subprocess.run(["docker", "rm", "-f", REGISTRY_NAME], capture_output=True)


@pytest.fixture(scope="session")
def oras_tool_dir(tmp_path_factory):
    """A DCACHE_TOOL_DIR provisioned by the library's OWN dcache_ensure_oras —
    a real download-and-verify of the pinned oras release, not a stand-in.
    This is session-scoped so it only pays the download once, and it doubles
    as live proof that the pinned URL + checksum in the library actually work
    end to end (a fresh environment with no pre-installed oras is exactly the
    case a CI runner is in). Skips, rather than erroring, if that provisioning
    itself cannot complete (e.g. no network) — same posture as the docker
    skip above.
    """
    d = tmp_path_factory.mktemp("dcache-tool-dir")
    script = d / "ensure.sh"
    script.write_text(f"set -euo pipefail\n. {LIB}\ndcache_ensure_oras\n"
                       "command -v oras\n")
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                        env={**os.environ, "DCACHE_TOOL_DIR": str(d)})
    if r.returncode != 0:
        pytest.skip(f"could not provision the pinned oras: {r.stderr}")
    return d


def _run(body, cwd, tool_dir, env=None):
    """Run BODY with the real library sourced, under the same set -euo
    pipefail every derive-backup.sh runs under — the unit tests deliberately
    do NOT do this (their fakes can't survive it); this integration test is
    exactly where that contract needs to be proven for real.
    """
    script = cwd / "t.sh"
    script.write_text(f"set -euo pipefail\n. {LIB}\n" + textwrap.dedent(body))
    e = {**os.environ, "DCACHE_TOOL_DIR": str(tool_dir), **(env or {})}
    return subprocess.run(["bash", str(script)], capture_output=True, text=True,
                           env=e, cwd=cwd)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_names(root: Path):
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_oras_round_trip_is_byte_identical(tmp_path, registry, oras_tool_dir):
    ref = f"{registry}/dcache-test/pkg:t1"
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hello a\n")
    (src / "b.txt").write_bytes(b"hello b\n")
    # >= 1 MiB, per the plan: prove this isn't only correct for tiny payloads.
    (src / "big.bin").write_bytes(os.urandom(1024 * 1024 + 1))
    want = {p.name: _sha(p) for p in src.iterdir()}

    r = _run(f'dcache_push "{ref}" "{src}" a.txt b.txt big.bin', tmp_path, oras_tool_dir)
    assert r.returncode == 0, r.stderr

    dest = tmp_path / "dest"
    r = _run(f'dcache_pull "{ref}" "{dest}" "*.txt" && echo "FORMAT=$DCACHE_HIT_FORMAT"',
              tmp_path, oras_tool_dir)
    assert r.returncode == 0, r.stderr
    assert "FORMAT=oras" in r.stdout, r.stdout

    got = {p.name: _sha(p) for p in dest.iterdir()}
    assert got == want, (got, want)


def test_legacy_hit_falls_back_and_migrates(tmp_path, registry, oras_tool_dir):
    ref = f"{registry}/dcache-test/legacy:t1"
    files_dir = tmp_path / "legacy_src"
    files_dir.mkdir()
    (files_dir / "x_one.dat").write_bytes(b"legacy one\n")
    (files_dir / "x_two.dat").write_bytes(b"legacy two\n")
    want = {p.name: _sha(p) for p in files_dir.iterdir()}

    # Build the legacy `FROM scratch` image exactly as the six derive scripts
    # do today and push it — this is the format a real cache entry is in
    # before this task's library ever touches it.
    work = tmp_path / "legacy_work"
    work.mkdir()
    for f in files_dir.iterdir():
        shutil.copy(f, work / f.name)
    dockerfile = "FROM scratch\n" + "".join(
        f"COPY {f.name} /\n" for f in sorted(files_dir.iterdir()))
    (work / "Dockerfile").write_text(dockerfile)
    subprocess.run(["docker", "build", "-t", ref, str(work)],
                    capture_output=True, text=True, check=True)
    subprocess.run(["docker", "push", ref], capture_output=True, text=True, check=True)
    try:
        # First pull: the registry only has the legacy image. dcache_pull
        # must fall back and still return the files correctly.
        dest1 = tmp_path / "dest1"
        r = _run(f'dcache_pull "{ref}" "{dest1}" "x_*" '
                  '&& echo "FORMAT=$DCACHE_HIT_FORMAT"', tmp_path, oras_tool_dir)
        assert r.returncode == 0, r.stderr
        assert "FORMAT=legacy" in r.stdout, r.stdout
        got = {p.name: _sha(p) for p in dest1.iterdir() if p.is_file()}
        assert got == want, (got, want)

        # #79 regression guard: `docker export` unfiltered would have dropped
        # dev/, etc/, proc/, sys/ and .dockerenv alongside the real outputs.
        names = _tree_names(dest1)
        assert not (names & {"dev", "etc", "proc", "sys", ".dockerenv"}), names

        # Second pull, fresh dest: the first pull's non-fatal migration must
        # have replaced the registry's tag with an oras artifact — proven by
        # actually re-reading the registry, not by inspecting a log line.
        dest2 = tmp_path / "dest2"
        r = _run(f'dcache_pull "{ref}" "{dest2}" "x_*" '
                  '&& echo "FORMAT=$DCACHE_HIT_FORMAT"', tmp_path, oras_tool_dir)
        assert r.returncode == 0, r.stderr
        assert "FORMAT=oras" in r.stdout, r.stdout
        got2 = {p.name: _sha(p) for p in dest2.iterdir() if p.is_file()}
        assert got2 == want, (got2, want)
    finally:
        subprocess.run(["docker", "rmi", "-f", ref], capture_output=True)
