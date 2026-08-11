"""Round-trip builder/stage-lib/derive-cache.sh against a real registry.

tests/test_derive_cache_lib.py never touches a network or a real image; it
only asserts on which commands the library invokes, with fake `oras`/`docker`
standing in. This is the test that proves the library actually moves bytes
correctly, and that its two non-hit outcomes — an absent tag and a tag holding
something that is not an artifact — behave against a REAL registry rather than
against a fake's idea of one.
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
    r = _run(f'dcache_pull "{ref}" "{dest}" && echo "FORMAT=$DCACHE_HIT_FORMAT"',
              tmp_path, oras_tool_dir)
    assert r.returncode == 0, r.stderr
    assert "FORMAT=oras" in r.stdout, r.stdout

    got = {p.name: _sha(p) for p in dest.iterdir()}
    assert got == want, (got, want)


def test_an_absent_tag_is_a_miss(tmp_path, registry, oras_tool_dir):
    """The one outcome that legitimately means "derive from scratch"."""
    ref = f"{registry}/dcache-test/never-pushed:t1"
    dest = tmp_path / "dest"
    r = _run(f'DCACHE_RETRY_SLEEP=0 dcache_pull "{ref}" "{dest}" || echo MISS',
             tmp_path, oras_tool_dir)
    assert r.returncode == 0, r.stderr
    assert "MISS" in r.stdout, (r.stdout, r.stderr)


def test_a_legacy_image_is_refused_rather_than_read(tmp_path, registry, oras_tool_dir):
    """The old `FROM scratch` format is gone, and a tag still holding one fails.

    Built and pushed exactly the way the six derive scripts used to, so this is
    a real legacy entry, not a fake's approximation of one. Two things must be
    true of the refusal: it is NOT reported as a miss — that would send a caller
    off to re-derive, up to ~24 h for wikipedia, over a format mismatch — and it
    leaves the destination untouched. `oras pull` of such a tag exits 0 while
    writing nothing, so a reader that trusted the exit code would hand back an
    empty directory and call it a hit.
    """
    ref = f"{registry}/dcache-test/legacy:t1"
    work = tmp_path / "legacy_work"
    work.mkdir()
    (work / "x_one.dat").write_bytes(b"legacy one\n")
    (work / "x_two.dat").write_bytes(b"legacy two\n")
    (work / "Dockerfile").write_text(
        "FROM scratch\nCOPY x_one.dat /\nCOPY x_two.dat /\n")
    subprocess.run(["docker", "build", "-t", ref, str(work)],
                   capture_output=True, text=True, check=True)
    subprocess.run(["docker", "push", ref], capture_output=True, text=True, check=True)
    try:
        dest = tmp_path / "dest"
        r = _run(f'dcache_pull "{ref}" "{dest}" || echo MISS', tmp_path, oras_tool_dir)
        assert r.returncode != 0, f"a legacy entry was accepted: {r.stdout!r}"
        assert "MISS" not in r.stdout, "a format mismatch was reported as a miss"
        assert "not an oras artifact" in r.stderr, r.stderr
        assert _tree_names(dest) == set(), (
            f"a refused read still wrote into the destination: {_tree_names(dest)}")
    finally:
        subprocess.run(["docker", "rmi", "-f", ref], capture_output=True)
