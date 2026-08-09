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


# The default fake answers `manifest fetch` the way a real artifact pushed by
# this library does. That is the signal dcache_pull decides on, so a fake that
# stayed silent would model a tag that exists in no registry.
ORAS_ARTIFACT = (
    'if [ "$1 $2" = "manifest fetch" ]; then\n'
    '  echo \'{"artifactType":"application/vnd.beep.derived.v1","layers":[]}\'\n'
    'fi\n'
    'exit 0\n'
)
# A legacy `FROM scratch` image: no artifactType, and — measured live against a
# real registry with oras 1.3.3 on 2026-08-08 — `oras pull` still exits 0 while
# writing nothing at all. Treating that as a hit is the false-hit bug.
ORAS_LEGACY_TAG = (
    'if [ "$1 $2" = "manifest fetch" ]; then\n'
    '  echo \'{"mediaType":"application/vnd.oci.image.index.v1+json"}\'\n'
    'fi\n'
    'exit 0\n'
)


def run(body, tmp_path, fake_oras=ORAS_ARTIFACT, fake_docker="exit 1", env=None):
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
    assert any(c.startswith("oras pull") for c in calls), calls
    # The whole point of preferring the artifact: no legacy image is fetched,
    # so the ~2x local-storage cost of holding a cache image is never paid.
    assert not any(c.startswith("docker pull") for c in calls), calls


def test_a_pull_falls_back_to_the_legacy_image(tmp_path):
    r, calls = run('dcache_pull reg/x:t out "*.dat" && echo HIT',
                   tmp_path, fake_oras="exit 1", fake_docker="exit 0")
    assert "HIT" in r.stdout, r.stderr
    assert any(c.startswith("docker pull") for c in calls), calls


def fake_docker_serving(tmp_path, *names):
    """A docker whose `export` really emits a tar of NAMES.

    A fake that just `exit 0`s models a legacy hit which extracts nothing —
    a state that cannot occur — and then cannot distinguish "migrated the
    entry" from "migrated whatever else was lying in $dest".
    """
    for n in names:
        (tmp_path / n).write_text(f"content of {n}")
    return (
        'case "$1" in\n'
        '  pull) exit 0 ;;\n'
        '  create) echo cid; exit 0 ;;\n'
        f'  export) tar -cf - -C {tmp_path} {" ".join(names)}; exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    )


def test_a_legacy_hit_is_re_pushed_as_oras(tmp_path):
    r, calls = run('dcache_pull reg/x:t out "*.dat"', tmp_path,
                   fake_oras=ORAS_LEGACY_TAG,
                   fake_docker=fake_docker_serving(tmp_path, "a.dat"))
    assert any(c.startswith("oras push") for c in calls), (
        f"a legacy hit did not migrate the entry, so it stays legacy forever: {r.stderr}")


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


def test_a_legacy_tag_is_not_mistaken_for_an_oras_hit(tmp_path):
    """The false-hit bug: real oras exits 0 on a legacy image and writes nothing.

    Measured 2026-08-08 against a local registry:2 with oras 1.3.3 —
    `oras pull` of a `FROM scratch` image printed "Skipped pulling layers
    without file name", wrote 0 files, and exited 0. Deciding on the exit code
    would report a hit with nothing extracted, and nothing downstream would
    know to fall back to the legacy reader.
    """
    r, calls = run('dcache_pull reg/x:t out "*.dat" && echo "FORMAT=$DCACHE_HIT_FORMAT"',
                   tmp_path, fake_oras=ORAS_LEGACY_TAG, fake_docker="exit 0")
    assert "FORMAT=legacy" in r.stdout, (
        f"a legacy tag was read as an oras hit: {r.stdout!r} {r.stderr!r}")


def test_migration_pushes_only_the_entry_it_read(tmp_path):
    """$dest is the SHARED datasets dir, not a clean directory.

    The first version of dcache__migrate_legacy_hit pushed `find . -type f`
    over $dest. On a runner that is DATASETS_DIR — every other image's inputs
    and the multi-tens-of-GB upstream tars — so a legacy hit would have
    replaced a good cache tag with an artifact containing all of it. The
    round-trip test missed it because its destination was an empty temp dir,
    where "everything in $dest" and "what the entry held" coincide.
    """
    dest = tmp_path / "datasets"
    dest.mkdir()
    # A neighbour that must never be swept into this image's cache entry.
    (dest / "someone-elses-40gb-upstream.tar").write_text("not mine")
    # A fake docker whose `export` emits a tar holding just this entry's file.
    fake_docker = (
        'case "$1" in\n'
        '  pull) exit 0 ;;\n'
        '  create) echo cid; exit 0 ;;\n'
        f'  export) tar -cf - -C {tmp_path} entry.dat; exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    )
    (tmp_path / "entry.dat").write_text("mine")
    r, calls = run(f'dcache_pull reg/x:t {dest} "entry*"', tmp_path,
                   fake_oras=ORAS_LEGACY_TAG, fake_docker=fake_docker)
    push = [c for c in calls if c.startswith("oras push")]
    assert push, f"no migration push happened at all: {calls} {r.stderr}"
    assert "someone-elses-40gb-upstream.tar" not in push[0], (
        f"the migration swept up an unrelated dataset: {push[0]}")
    assert "entry.dat" in push[0], push[0]


def test_no_migrate_leaves_the_entry_alone(tmp_path):
    """wikipedia's ~88 GB entry must not convert as a side effect of a build."""
    r, calls = run('DCACHE_NO_MIGRATE=1 dcache_pull reg/x:t out "*.dat" && echo HIT',
                   tmp_path, fake_oras=ORAS_LEGACY_TAG,
                   fake_docker=fake_docker_serving(tmp_path, "a.dat"))
    assert "HIT" in r.stdout, r.stderr
    assert not any(c.startswith("oras push") for c in calls), (
        f"an opted-out entry was migrated anyway: {calls}")


# --- a broken transfer is not a miss -----------------------------------------
#
# Run 31284811602 / webarena-wikipedia, verbatim from the job log:
#   failed to copy: read tcp ...->185.199.111.154:443: read: connection reset by peer
# GHCR reset an 88 GB pull mid-flight. With no retry that read as "no cache
# entry", and pre-fix a miss meant re-downloading from upstream at a measured
# 1.05 MB/s — about 24 hours. The entry was intact the whole time.

RESET = "failed to copy: read tcp: read: connection reset by peer"


def flaky(cmd, fail_times, tmp_path, then="exit 0"):
    """A fake CMD that fails `fail_times` times, then succeeds.

    The counter is a file because each invocation is a fresh process.
    """
    return (
        f'n=$(cat {tmp_path}/{cmd}.n 2>/dev/null || echo 0); n=$((n+1));'
        f' echo $n > {tmp_path}/{cmd}.n\n'
        f'if [ "$n" -le {fail_times} ]; then echo "{RESET}" >&2; exit 1; fi\n'
        f'{then}\n'
    )


def test_a_reset_transfer_is_retried_rather_than_called_a_miss(tmp_path):
    r, calls = run('DCACHE_RETRY_SLEEP=0 dcache_pull reg/x:t out "*.dat" '
                   '&& echo "FORMAT=$DCACHE_HIT_FORMAT"', tmp_path,
                   fake_oras=ORAS_LEGACY_TAG,
                   fake_docker=flaky("docker", 1, tmp_path,
                                     then=fake_docker_serving(tmp_path, "a.dat")),
                   env={"DCACHE_NO_MIGRATE": "1"})
    assert "FORMAT=legacy" in r.stdout, (
        f"a transient reset was reported as a cache miss: {r.stdout!r} {r.stderr!r}")
    assert len([c for c in calls if c.startswith("docker pull")]) == 2, calls


def test_an_absent_entry_misses_immediately_without_burning_retries(tmp_path):
    """A first build of a NEW image has no entry, and must not pay 3 attempts.

    This is why presence is decided by the manifest and not by the transfer:
    an absent ref returns no manifest, so there is nothing to retry.
    """
    r, calls = run('DCACHE_RETRY_SLEEP=0 dcache_pull reg/x:t out "*.dat" || echo MISS',
                   tmp_path, fake_oras="exit 1", fake_docker="exit 1")
    assert "MISS" in r.stdout, r.stderr
    assert len([c for c in calls if c.startswith("docker pull")]) == 1, (
        f"an absent entry was retried: {calls}")


def test_a_persistently_broken_transfer_still_misses_and_says_why(tmp_path):
    """Retries are bounded; the operator still gets the underlying error."""
    r, calls = run('DCACHE_RETRY_SLEEP=0 dcache_pull reg/x:t out "*.dat" || echo MISS',
                   tmp_path, fake_oras=ORAS_LEGACY_TAG, fake_docker=f'echo "{RESET}" >&2; exit 1')
    assert "MISS" in r.stdout, r.stderr
    assert len([c for c in calls if c.startswith("docker pull")]) == 3, calls
    assert "connection reset by peer" in r.stderr, (
        f"the real cause was swallowed: {r.stderr}")


def test_a_reset_oras_transfer_is_retried_too(tmp_path):
    """The oras path is where the fleet ends up; it must not regress to no-retry."""
    r, calls = run('DCACHE_RETRY_SLEEP=0 dcache_pull reg/x:t out "*.dat" '
                   '&& echo "FORMAT=$DCACHE_HIT_FORMAT"', tmp_path,
                   fake_oras=(
                       'if [ "$1 $2" = "manifest fetch" ]; then\n'
                       '  echo \'{"artifactType":"application/vnd.beep.derived.v1"}\'\n'
                       '  exit 0\n'
                       'fi\n'
                       + flaky("oraspull", 1, tmp_path)),
                   fake_docker="exit 1")
    assert "FORMAT=oras" in r.stdout, (
        f"a reset oras pull was not retried: {r.stdout!r} {r.stderr!r}")
