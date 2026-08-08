"""The digest lock: a miss on a pinned entry must fail the build (#42).

`dcache_pull`'s miss return (exit 1) is silent about WHY: a brand-new image
and a broken cache look identical to it. `dcache_require`, called right
after a `dcache_pull` miss, distinguishes them via `builder/derived-cache.lock`:
a ref this fleet has depended on before but can no longer fetch is a fatal
error (something broke); a ref never seen before is expected to derive.
`ALLOW_DERIVE_CACHE_MISS=1` is the documented escape hatch for a GHCR outage.
"""
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "builder" / "stage-lib" / "derive-cache.sh"
LOCK = REPO / "builder" / "derived-cache.lock"

PINNED_REF = "ghcr.io/shkolnik/webarena-gitlab-derived:6269a90527a6-r1"
UNPINNED_REF = "ghcr.io/shkolnik/webarena-gitlab-derived:doesnotexist-r99"

LOCK_LINE = re.compile(r"^(\S+)\s+(sha256:[0-9a-f]{64})$")

PACKAGES = {
    "vwa-classifieds-derived",
    "webarena-gitlab-derived",
    "webarena-reddit-derived",
    "webarena-shopping-derived",
    "webarena-shopping-admin-derived",
    "webshop-server-derived",
    "webarena-wikipedia-derived",
}


def run(body, tmp_path, env=None):
    script = tmp_path / "t.sh"
    script.write_text(f"set -u\n. {LIB}\n" + textwrap.dedent(body))
    e = {**os.environ, "DCACHE_SKIP_ORAS_INSTALL": "1", **(env or {})}
    return subprocess.run(["bash", str(script)], capture_output=True, text=True,
                           env=e, cwd=tmp_path)


def test_the_lock_file_exists():
    assert LOCK.is_file()


def _lock_entries():
    entries = []
    for line in LOCK.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def test_the_lock_parses_ref_and_digest():
    entries = _lock_entries()
    assert entries, "lock has no entries"
    for line in entries:
        m = LOCK_LINE.match(line)
        assert m, f"malformed lock line: {line!r}"


def test_lock_refs_are_unique():
    refs = [LOCK_LINE.match(l).group(1) for l in _lock_entries()]
    assert len(refs) == len(set(refs)), refs


def test_every_ref_belongs_to_one_of_the_seven_packages():
    for line in _lock_entries():
        ref = LOCK_LINE.match(line).group(1)
        # ghcr.io/shkolnik/<package>:<tag>
        pkg = ref.split("/")[-1].split(":")[0]
        assert pkg in PACKAGES, f"{ref}: package {pkg!r} is not one of the seven"


def test_a_miss_on_a_pinned_ref_is_fatal(tmp_path):
    r = run(f'dcache_require "{PINNED_REF}"; echo "rc=$?"', tmp_path)
    assert r.returncode != 0, r.stdout + r.stderr
    assert PINNED_REF in (r.stdout + r.stderr)
    assert "ALLOW_DERIVE_CACHE_MISS" in (r.stdout + r.stderr)


def test_a_miss_on_an_unpinned_ref_returns_cleanly(tmp_path):
    r = run(f'dcache_require "{UNPINNED_REF}"; echo "rc=$?"', tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0" in r.stdout, r.stdout + r.stderr


def test_the_escape_hatch_downgrades_to_a_warning(tmp_path):
    r = run(f'dcache_require "{PINNED_REF}"; echo "rc=$?"', tmp_path,
            env={"ALLOW_DERIVE_CACHE_MISS": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0" in r.stdout, r.stdout + r.stderr
    assert PINNED_REF in (r.stdout + r.stderr), "the warning should still name the ref"


def test_a_successful_push_prints_the_lock_line_to_add(tmp_path):
    fake_oras = (
        'if [ "$1" = push ]; then '
        'echo "Pushed [registry] reg/x:t"; '
        'echo "Digest: sha256:' + ("a" * 64) + '"; exit 0; fi; exit 0'
    )
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    p = bin_ / "oras"
    p.write_text(f"#!/bin/bash\n{fake_oras}\n")
    p.chmod(0o755)
    r = run('dcache_push reg/x:t . a.dat', tmp_path,
            env={"PATH": f"{bin_}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "reg/x:t" in r.stdout
    assert f"sha256:{'a' * 64}" in r.stdout
