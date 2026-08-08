"""builder/stage-lib/derive-cache.sh, exercised without a registry.

The round trip against a real registry lives in tests/integration/. These
cover the decision table: which format is tried first, what a miss returns,
and that a failed push is fatal exactly as #80 requires.
"""
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "builder" / "stage-lib" / "derive-cache.sh"


def run(body, tmp_path, fake_oras="exit 0", fake_docker="exit 1", env=None):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for name, script in (("oras", fake_oras), ("docker", fake_docker)):
        p = bin_ / name
        p.write_text(f"#!/bin/bash\necho \"{name} $*\" >> {tmp_path}/calls\n{script}\n")
        p.chmod(0o755)
    script = tmp_path / "t.sh"
    script.write_text(f"set -u\n. {LIB}\n" + textwrap.dedent(body))
    e = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}",
         "DCACHE_SKIP_ORAS_INSTALL": "1", **(env or {})}
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=e, cwd=tmp_path)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return r, calls


def test_the_library_exists():
    assert LIB.is_file()


def test_a_pull_prefers_the_oras_artifact(tmp_path):
    r, calls = run('dcache_pull reg/x:t out "*.dat" && echo HIT', tmp_path)
    assert "HIT" in r.stdout, r.stderr
    assert calls[0].startswith("oras pull"), calls
    assert not any(c.startswith("docker pull") for c in calls), calls


def test_a_pull_falls_back_to_the_legacy_image(tmp_path):
    r, calls = run('dcache_pull reg/x:t out "*.dat" && echo HIT',
                   tmp_path, fake_oras="exit 1", fake_docker="exit 0")
    assert "HIT" in r.stdout, r.stderr
    assert any(c.startswith("docker pull") for c in calls), calls


def test_a_legacy_hit_is_re_pushed_as_oras(tmp_path):
    r, calls = run('dcache_pull reg/x:t out "*.dat"', tmp_path,
                   fake_oras="[ \"$1\" = pull ] && exit 1; exit 0", fake_docker="exit 0")
    assert any(c.startswith("oras push") for c in calls), (
        "a legacy hit did not migrate the entry, so it stays legacy forever")


def test_a_re_push_failure_does_not_fail_the_build(tmp_path):
    """Migration is opportunistic: the bytes are already in hand."""
    r, calls = run('dcache_pull reg/x:t out "*.dat" && echo HIT', tmp_path,
                   fake_oras="exit 1", fake_docker="exit 0")
    assert "HIT" in r.stdout and r.returncode == 0, r.stderr


def test_a_miss_returns_one_and_says_so(tmp_path):
    r, calls = run('dcache_pull reg/x:t out "*.dat" || echo MISS', tmp_path,
                   fake_oras="exit 1", fake_docker="exit 1")
    assert "MISS" in r.stdout, r.stderr


def test_a_push_retries_three_times_then_exits_nonzero(tmp_path):
    r, calls = run('DCACHE_RETRY_SLEEP=0 dcache_push reg/x:t . a.dat; echo "rc=$?"',
                   tmp_path, fake_oras="exit 1")
    assert len([c for c in calls if c.startswith("oras push")]) == 3, calls
    assert r.returncode != 0, "a failed push must be fatal (#80)"


def test_an_extract_filter_is_required(tmp_path):
    """#79: an unfiltered docker export drops /dev,/etc,/proc,/sys into datasets."""
    r, _ = run('dcache_pull reg/x:t out; echo "rc=$?"', tmp_path)
    assert r.returncode != 0 or "rc=0" not in r.stdout
