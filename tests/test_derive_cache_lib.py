"""builder/stage-lib/derive-cache.sh, exercised without a registry.

The round trip against a real registry lives in tests/integration/. These
cover the decision table: what counts as a hit, what a miss returns, which
failures are fatal, and that a failed push is fatal exactly as #80 requires.

There is one format now. The library used to read a legacy `FROM scratch`
Docker image as well, and migrate it to oras on the fly; all seven entries in
builder/derived-cache.lock were confirmed to be artifacts against the live
registry on 2026-08-11, so that reader is gone. The tests that covered it are
gone with it, replaced by the two that matter afterwards: a non-artifact tag
must fail loudly rather than silently miss, and no legacy reader may return
unnoticed.
"""
import hashlib
import json
import os
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "builder" / "stage-lib" / "derive-cache.sh"


def oras_artifact(files=(("a.dat", "payload"),), blob_fetch=None):
    """A fake `oras` serving a real artifact: a manifest, and digest-exact blobs.

    The digests are the true sha256 of what the fake writes, because
    dcache__fetch_blobs decides whether a layer is already on disk by hashing
    it. A fake with invented digests would model a registry that always lies,
    and every skip-if-present test would pass for the wrong reason.

    blob_fetch overrides the `blob fetch` body — for modelling a transfer that
    dies partway.
    """
    layers, cases = [], []
    for name, content in files:
        raw = content.encode()
        digest = hashlib.sha256(raw).hexdigest()
        layers.append({
            "digest": "sha256:" + digest,
            "size": len(raw),
            "annotations": {"org.opencontainers.image.title": name},
        })
        cases.append(f'    {digest}) printf %s {shlex.quote(content)} > "$out" ;;')
    manifest = json.dumps(
        {"artifactType": "application/vnd.beep.derived.v1", "layers": layers})
    serve = blob_fetch or (
        '  shift 2; out=""; ref=""\n'
        '  while [ $# -gt 0 ]; do\n'
        '    case "$1" in --output) out=$2; shift 2 ;; *) ref=$1; shift ;; esac\n'
        '  done\n'
        '  case "${ref##*@sha256:}" in\n'
        + "\n".join(cases) + "\n"
        '    *) echo "no such blob" >&2; exit 1 ;;\n'
        '  esac\n'
        '  exit 0\n'
    )
    return (
        'if [ "$1 $2" = "manifest fetch" ]; then\n'
        f'  echo {shlex.quote(manifest)}\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$1 $2" = "blob fetch" ]; then\n'
        + serve +
        'fi\n'
        'exit 0\n'
    )


# The default fake answers `manifest fetch` the way a real artifact pushed by
# this library does. That is the signal dcache_pull decides on, so a fake that
# stayed silent would model a tag that exists in no registry.
ORAS_ARTIFACT = oras_artifact()
# A tag holding a Docker image rather than an artifact: a manifest, but no
# artifactType. This is what the fleet's entries looked like before the
# migration — and, measured live with oras 1.3.3 on 2026-08-08, `oras pull` of
# one exits 0 while writing nothing at all, because oras materializes a layer
# only when it carries an `org.opencontainers.image.title`. Deciding a hit on
# the exit code would report success with an empty datasets dir.
ORAS_NOT_AN_ARTIFACT = (
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


def test_a_pull_reads_the_oras_artifact(tmp_path):
    r, calls = run('dcache_pull reg/x:t out && echo "FORMAT=$DCACHE_HIT_FORMAT"', tmp_path)
    assert "FORMAT=oras" in r.stdout, r.stderr
    assert any(c.startswith("oras blob fetch") for c in calls), calls
    # No docker anywhere on the read path: holding a cache image locally cost
    # roughly TWICE its content size on a runner whose disk is its scarcest
    # resource, and nothing ever read it after the export.
    assert not any(c.startswith("docker") for c in calls), calls
    assert (tmp_path / "out" / "a.dat").read_text() == "payload"


def test_every_layer_lands_as_its_own_file(tmp_path):
    """Blob-by-blob must extract exactly what `oras pull` would have."""
    files = (("a.dat", "one"), ("b.dat", "two"), (".sizes", "three"))
    r, _ = run('dcache_pull reg/x:t out && echo HIT', tmp_path,
               fake_oras=oras_artifact(files))
    assert "HIT" in r.stdout, r.stderr
    for name, content in files:
        assert (tmp_path / "out" / name).read_text() == content


def test_a_layer_already_on_disk_is_not_fetched_again(tmp_path):
    """The whole point: an interrupted pull keeps what it finished.

    Runs 31460767854 and 31464177083 failed six times against wikipedia's
    95 GB entry, and one of those attempts transferred 19 GB correctly before
    being cut off — then threw it away, because `oras pull` restarts from
    zero. With interruptions arriving every 10-15 minutes and a clean pull
    needing ~19, that loop never terminates however many retries it gets.
    """
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "a.dat").write_text("payload")   # already complete and correct
    _, calls = run(f'dcache_pull reg/x:t {dest}', tmp_path,
                   fake_oras=oras_artifact((("a.dat", "payload"), ("b.dat", "more"))))
    fetched = [c for c in calls if c.startswith("oras blob fetch")]
    assert len(fetched) == 1, f"a completed layer was re-fetched: {fetched}"
    assert "b.dat" in fetched[0], fetched[0]


def test_a_truncated_leftover_is_refetched_not_trusted(tmp_path):
    """A killed transfer leaves a short file; resuming must not accept it."""
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "a.dat").write_text("payl")      # the interrupted write
    _, calls = run(f'dcache_pull reg/x:t {dest}', tmp_path)
    assert any("a.dat" in c for c in calls if c.startswith("oras blob fetch")), calls
    assert (dest / "a.dat").read_text() == "payload"


def test_a_leftover_of_the_right_size_but_wrong_bytes_is_refetched(tmp_path):
    """Size is the cheap screen, not the verdict.

    Skipping on size alone would hand a benchmark image corrupt data that no
    later step re-checks: oras verifies what IT downloads, and nothing else
    verifies what a previous run left behind.
    """
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "a.dat").write_text("PAYLOAD")   # same length, different bytes
    _, calls = run(f'dcache_pull reg/x:t {dest}', tmp_path)
    assert any("a.dat" in c for c in calls if c.startswith("oras blob fetch")), calls
    assert (dest / "a.dat").read_text() == "payload"


def test_a_layer_title_that_escapes_the_destination_is_refused(tmp_path):
    """The title is registry-controlled and is used as a path."""
    r, _ = run('dcache_pull reg/x:t out || echo REFUSED', tmp_path,
               fake_oras=oras_artifact(((".././../etc/cron.d/x", "evil"),)))
    assert "not a bare filename" in r.stderr, r.stderr
    assert not (tmp_path / "etc").exists()


def test_an_interrupted_pass_resumes_instead_of_restarting(tmp_path):
    """Pass 2 must re-fetch only what pass 1 did not finish."""
    # Dies on b.dat the first time it is asked for, serves it the second.
    serve = (
        '  shift 2; out=""; ref=""\n'
        '  while [ $# -gt 0 ]; do\n'
        '    case "$1" in --output) out=$2; shift 2 ;; *) ref=$1; shift ;; esac\n'
        '  done\n'
        f'  case "$out" in *b.dat)\n'
        f'    n=$(cat {tmp_path}/b.n 2>/dev/null || echo 0); n=$((n+1));'
        f' echo $n > {tmp_path}/b.n\n'
        '    if [ "$n" = 1 ]; then\n'
        '      echo "stream error: PROTOCOL_ERROR; received from peer" >&2; exit 1\n'
        '    fi\n'
        '    printf %s two > "$out"; exit 0 ;;\n'
        '  esac\n'
        '  printf %s one > "$out"; exit 0\n'
    )
    r, calls = run('dcache_pull reg/x:t out && echo HIT', tmp_path,
                   fake_oras=oras_artifact((("a.dat", "one"), ("b.dat", "two")),
                                           blob_fetch=serve),
                   env={"DCACHE_RETRY_SLEEP": "0"})
    assert "HIT" in r.stdout, r.stderr
    fetched = [c for c in calls if c.startswith("oras blob fetch")]
    # a.dat once (pass 1, kept), b.dat twice (failed, then retried in pass 2).
    assert len([c for c in fetched if "a.dat" in c]) == 1, fetched
    assert len([c for c in fetched if "b.dat" in c]) == 2, fetched
    assert "PROTOCOL_ERROR" in r.stderr, r.stderr


def test_a_persistently_interrupted_pull_gives_up_after_its_passes(tmp_path):
    """Passes are bounded, and running out of them is NOT a miss.

    The manifest already proved the entry is there, so a pull that still fails
    is a real error. Returning 1 would send the caller off to re-derive an
    entry that exists — the exact cost this library was written to stop paying.
    """
    r, calls = run('dcache_pull reg/x:t out || echo MISS', tmp_path,
                   fake_oras=oras_artifact(blob_fetch='  echo boom >&2; exit 1\n'),
                   env={"DCACHE_PULL_TRIES": "3", "DCACHE_RETRY_SLEEP": "0"})
    assert r.returncode != 0 and "MISS" not in r.stdout, r.stdout
    assert len([c for c in calls if c.startswith("oras blob fetch")]) == 3, calls


def test_a_failed_transfer_reports_all_of_its_output(tmp_path):
    """The old `tail -n 3` is why run 31460767854's log could not be read.

    On a concurrent pull the last three lines are chosen by interleaving, not
    by relevance — and the attempt whose failure is fatal printed nothing at
    all, because only non-final attempts were logged.
    """
    noisy = "\n".join(f'  echo "line{i}"' for i in range(1, 6)) + "\n  exit 1\n"
    r, _ = run('dcache_pull reg/x:t out || true', tmp_path,
               fake_oras=oras_artifact(blob_fetch=noisy),
               env={"DCACHE_PULL_TRIES": "1", "DCACHE_RETRY_SLEEP": "0"})
    for i in range(1, 6):
        assert f"line{i}" in r.stderr, r.stderr


def test_a_miss_returns_one_and_says_so(tmp_path):
    r, calls = run('dcache_pull reg/x:t out || echo MISS', tmp_path, fake_oras="exit 1")
    assert "MISS" in r.stdout, r.stderr


def test_an_absent_entry_misses_immediately_without_burning_retries(tmp_path):
    """A first build of a NEW image has no entry, and must not pay 3 attempts.

    This is why presence is decided by the manifest and not by the transfer:
    an absent ref returns no manifest, so there is nothing to retry.
    """
    r, calls = run('DCACHE_RETRY_SLEEP=0 dcache_pull reg/x:t out || echo MISS',
                   tmp_path, fake_oras="exit 1")
    assert "MISS" in r.stdout, r.stderr
    assert not any(c.startswith("oras pull") for c in calls), (
        f"an absent entry was pulled anyway: {calls}")


def test_a_miss_carries_the_reason_it_missed(tmp_path):
    """`denied`, a 503 and a genuinely absent tag must not look identical.

    Run 31268159790: webarena/shopping missed and spent two hours re-downloading
    41 GB, and the log could not say why, because the idiom of the day threw the
    error away. The manifest fetch is now the only thing that can explain a
    miss, so its stderr has to survive.
    """
    r, _ = run('dcache_pull reg/x:t out || echo MISS', tmp_path,
               fake_oras='echo "denied: requested access to the resource is denied" >&2\nexit 1\n')
    assert "MISS" in r.stdout, r.stderr
    assert "denied" in r.stderr, f"the miss swallowed its cause: {r.stderr!r}"


def test_a_non_artifact_tag_fails_loudly_instead_of_missing(tmp_path):
    """The legacy format is no longer read — and that must not read as a miss.

    A miss sends the caller off to re-derive; for wikipedia that is a ~24-hour,
    ~95 GB upstream fetch, spent because a tag held the wrong FORMAT rather
    than because anything was actually missing. Fail, and name the way out.
    """
    r, calls = run('dcache_pull reg/x:t out || echo MISS', tmp_path,
                   fake_oras=ORAS_NOT_AN_ARTIFACT)
    assert r.returncode != 0, f"a non-artifact tag was tolerated: {r.stdout!r}"
    assert "MISS" not in r.stdout, "a format problem was reported as a cache miss"
    assert "not an oras artifact" in r.stderr, r.stderr
    # The operator has to be told how to get out of it, not just what broke.
    assert "RECIPE" in r.stderr, r.stderr


def test_no_legacy_reader_remains(tmp_path):
    """The deletion is the feature; a reader that creeps back is a regression.

    `docker pull` of a cache ref, `docker export`, the migration push and the
    DCACHE_NO_MIGRATE opt-out were one mechanism, and half of it returning
    would be worse than all of it: a partially-restored fallback is a path
    nothing on the fleet exercises.
    """
    code = "\n".join(l for l in LIB.read_text().splitlines()
                      if not l.lstrip().startswith("#"))
    for gone in ("docker pull", "docker export", "docker create",
                 "DCACHE_NO_MIGRATE", "migrate_legacy_hit", "HIT_FORMAT=legacy"):
        assert gone not in code, f"the legacy cache reader is back: {gone!r}"


def test_a_pull_takes_no_filter_argument(tmp_path):
    """#79's wildcard was a property of `docker export`, which is gone.

    oras writes a layer as a file only when it carries an
    `org.opencontainers.image.title`, so an artifact read has nothing injected
    to filter out. A third argument now means a caller was left un-migrated.
    """
    src = LIB.read_text()
    assert "local ref=$1 dest=$2\n" in src, (
        "dcache_pull still binds a third positional argument")


# --- a broken transfer is not a miss -----------------------------------------
#
# Run 31284811602 / webarena-wikipedia, verbatim from the job log:
#   failed to copy: read tcp ...->185.199.111.154:443: read: connection reset by peer
# GHCR reset an 88 GB pull mid-flight. With no retry that read as "no cache
# entry", and pre-fix a miss meant re-downloading from upstream at a measured
# 1.05 MB/s — about 24 hours. The entry was intact the whole time.

RESET = "failed to copy: read tcp: read: connection reset by peer"


def test_a_reset_transfer_is_retried_rather_than_called_a_miss(tmp_path):
    """The oras path is the only path left; it must not regress to no-retry."""
    r, calls = run('DCACHE_RETRY_SLEEP=0 dcache_pull reg/x:t out '
                   '&& echo "FORMAT=$DCACHE_HIT_FORMAT"', tmp_path,
                   fake_oras=oras_artifact(blob_fetch=(
                       f'  n=$(cat {tmp_path}/o.n 2>/dev/null || echo 0); n=$((n+1));'
                       f' echo $n > {tmp_path}/o.n\n'
                       f'  if [ "$n" = 1 ]; then echo "{RESET}" >&2; exit 1; fi\n'
                       '  shift 2; out=""\n'
                       '  while [ $# -gt 0 ]; do\n'
                       '    case "$1" in --output) out=$2; shift 2 ;; *) shift ;; esac\n'
                       '  done\n'
                       '  printf %s payload > "$out"; exit 0\n')))
    assert "FORMAT=oras" in r.stdout, (
        f"a transient reset was reported as a cache miss: {r.stdout!r} {r.stderr!r}")


# --- writes ------------------------------------------------------------------


def test_a_push_retries_three_times_then_exits_nonzero(tmp_path):
    r, calls = run('DCACHE_RETRY_SLEEP=0 dcache_push reg/x:t . a.dat; echo "rc=$?"',
                   tmp_path, fake_oras="exit 1")
    assert len([c for c in calls if c.startswith("oras push")]) == 3, calls
    assert r.returncode != 0, "a failed push must be fatal (#80)"
