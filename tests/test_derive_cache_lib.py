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


DEFAULT_FILES = (("a.dat", "payload"),)


def oras_artifact(files=DEFAULT_FILES):
    """A fake `oras` serving the manifest of a real artifact.

    The digests are the true sha256 of what the matching fake curl writes,
    because dcache__fetch_blobs decides whether a layer is already on disk by
    hashing it. Invented digests would model a registry that always lies, and
    every skip-if-present test would pass for the wrong reason.

    Blobs are NOT served here: they no longer go through oras at all.
    """
    layers = []
    for name, content in files:
        raw = content.encode()
        layers.append({
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "annotations": {"org.opencontainers.image.title": name},
        })
    manifest = json.dumps(
        {"artifactType": "application/vnd.beep.derived.v1", "layers": layers})
    return (
        'if [ "$1 $2" = "manifest fetch" ]; then\n'
        f'  echo {shlex.quote(manifest)}\n'
        '  exit 0\n'
        'fi\n'
        'exit 0\n'
    )


def fake_curl(files=DEFAULT_FILES, body=None):
    """A fake `curl` that serves blobs the way a registry does — with ranges.

    It answers the token endpoint, then serves a blob by digest, appending only
    the bytes the caller does not already have. `$have` is the resume offset it
    was asked for and is recorded to `curl.offsets`, which is how these tests
    tell "resumed at 4" apart from "restarted at 0" — the distinction the whole
    change is about, and one a fake serving whole blobs could not express.

    body overrides the serving half, for modelling a transfer that dies.
    """
    cases = [f'    {hashlib.sha256(c.encode()).hexdigest()}) full={shlex.quote(c)} ;;'
             for _, c in files]
    serve = body or (
        '  echo "$have" >> "$OFFSETS"\n'
        '  printf %s "${full:$have}" >> "$out"\n'
        '  echo 206\n'
        '  exit 0\n'
    )
    return (
        'out=""; url=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -o) out=$2; shift 2 ;;\n'
        '    -C|-H|-u|-w) shift 2 ;;\n'
        '    -*) shift ;;\n'
        '    *) url=$1; shift ;;\n'
        '  esac\n'
        'done\n'
        'case "$url" in\n'
        '  */token\\?*) echo \'{"token":"faketoken"}\'; exit 0 ;;\n'
        'esac\n'
        'digest=${url##*/sha256:}\n'
        'case "$digest" in\n'
        + "\n".join(cases) + "\n"
        '  *) echo 404; exit 22 ;;\n'
        'esac\n'
        'have=$(stat -c %s "$out" 2>/dev/null || echo 0)\n'
        + serve
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


def run(body, tmp_path, fake_oras=ORAS_ARTIFACT, fake_docker="exit 1",
        fake_curl_=None, env=None):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    fakes = (("oras", fake_oras), ("docker", fake_docker),
             ("curl", fake_curl() if fake_curl_ is None else fake_curl_))
    for name, script in fakes:
        p = bin_ / name
        p.write_text(f"#!/bin/bash\necho \"{name} $*\" >> {tmp_path}/calls\n{script}\n")
        p.chmod(0o755)
    script = tmp_path / "t.sh"
    script.write_text(f"set -u\n. {LIB}\n" + textwrap.dedent(body))
    e = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}",
         "DCACHE_SKIP_ORAS_INSTALL": "1",
         # No test wants to sleep between passes, and the default 30s times
         # eight passes turns one mis-wired fake into a four-minute "hang"
         # that reads as an infinite loop. Tests that care set it back.
         "DCACHE_RETRY_SLEEP": "0",
         # Isolate from the developer's real Docker credentials: whether this
         # machine happens to be logged in to ghcr must not change a result.
         "DOCKER_CONFIG": str(tmp_path / "no-docker-config"),
         "OFFSETS": str(tmp_path / "curl.offsets"),
         **(env or {})}
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=e, cwd=tmp_path)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return r, calls


def offsets(tmp_path):
    """The resume offset of every blob request the fake curl served."""
    f = tmp_path / "curl.offsets"
    return [int(x) for x in f.read_text().split()] if f.exists() else []


def blob_calls(calls):
    return [c for c in calls if c.startswith("curl ") and "/blobs/" in c]


def test_the_library_exists():
    assert LIB.is_file()


def test_a_pull_reads_the_oras_artifact(tmp_path):
    r, calls = run('dcache_pull reg/x:t out && echo "FORMAT=$DCACHE_HIT_FORMAT"', tmp_path)
    assert "FORMAT=oras" in r.stdout, r.stderr
    assert blob_calls(calls), calls
    # No docker anywhere on the read path: holding a cache image locally cost
    # roughly TWICE its content size on a runner whose disk is its scarcest
    # resource, and nothing ever read it after the export.
    assert not any(c.startswith("docker") for c in calls), calls
    assert (tmp_path / "out" / "a.dat").read_text() == "payload"


def test_a_blob_is_fetched_by_digest_against_the_repository(tmp_path):
    """The URL is the contract with the registry; a wrong one 404s at 3am."""
    _, calls = run('dcache_pull reg/x:t out', tmp_path)
    want = hashlib.sha256(b"payload").hexdigest()
    assert any(f"https://reg/v2/x/blobs/sha256:{want}" in c for c in blob_calls(calls)), calls


def test_a_localhost_registry_is_reached_over_http(tmp_path):
    """Docker treats localhost as insecure, and the round-trip test needs it."""
    _, calls = run('dcache_pull localhost:5000/x:t out', tmp_path)
    assert any("http://localhost:5000/v2/" in c for c in blob_calls(calls)), calls
    assert not any("https://" in c for c in blob_calls(calls)), calls


def test_every_layer_lands_as_its_own_file(tmp_path):
    """Blob-by-blob must extract exactly what `oras pull` would have."""
    files = (("a.dat", "one"), ("b.dat", "two"), (".sizes", "three"))
    r, _ = run('dcache_pull reg/x:t out && echo HIT', tmp_path,
               fake_oras=oras_artifact(files), fake_curl_=fake_curl(files))
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
    files = (("a.dat", "payload"), ("b.dat", "more"))
    _, calls = run(f'dcache_pull reg/x:t {dest}', tmp_path,
                   fake_oras=oras_artifact(files), fake_curl_=fake_curl(files))
    fetched = blob_calls(calls)
    assert len(fetched) == 1, f"a completed layer was re-fetched: {fetched}"
    assert hashlib.sha256(b"more").hexdigest() in fetched[0], fetched[0]


def test_a_truncated_leftover_resumes_from_where_it_stopped(tmp_path):
    """The point of the byte-range rewrite, in one assertion.

    Run 31497034118 lost vwa/classifieds AND webarena/gitlab to blob-granular
    resume: 8.59 GB layers against a ~10-minute interruption period meant no
    layer ever finished, so no pass could skip anything, and eight passes
    moved zero bytes forward. A partial file must be built ON, not discarded.
    """
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "a.dat").write_text("payl")      # the interrupted write, 4 of 7
    run(f'dcache_pull reg/x:t {dest}', tmp_path)
    assert offsets(tmp_path) == [4], (
        f"the fetch restarted from zero instead of resuming: {offsets(tmp_path)}")
    assert (dest / "a.dat").read_text() == "payload"


def test_a_leftover_of_the_right_size_but_wrong_bytes_is_restarted(tmp_path):
    """Size is the cheap screen, not the verdict.

    A full-length file that fails its digest cannot be resumed — there is
    nothing past the end to ask for — so it is discarded and re-fetched from
    zero. Skipping on size alone would hand a benchmark image corrupt data
    that no later step re-checks.
    """
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "a.dat").write_text("PAYLOAD")   # same length, different bytes
    run(f'dcache_pull reg/x:t {dest}', tmp_path)
    assert offsets(tmp_path) == [0], offsets(tmp_path)
    assert (dest / "a.dat").read_text() == "payload"


def test_a_corrupt_prefix_is_discarded_rather_than_resumed_forever(tmp_path):
    """Resuming makes a bad prefix immortal unless something deletes it.

    The bytes already on disk are never re-read by the registry, so a wrong
    prefix can only be caught by the digest of the ASSEMBLED file — after
    which resuming again would append to the same wrong prefix and fail the
    same way every pass, forever.
    """
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "a.dat").write_text("XXXX")      # right-length prefix, wrong bytes
    r, _ = run(f'dcache_pull reg/x:t {dest} && echo HIT', tmp_path)
    assert "HIT" in r.stdout, r.stderr
    # Resumed at 4 onto the bad prefix, failed its digest, dropped the file,
    # then started clean at 0 and succeeded.
    assert offsets(tmp_path) == [4, 0], offsets(tmp_path)
    assert "does not" in r.stderr and "match" in r.stderr, r.stderr
    assert (dest / "a.dat").read_text() == "payload"


def test_a_registry_that_refuses_the_range_starts_over_instead_of_looping(tmp_path):
    """A 416, or curl's own exit 33, means this file can never complete."""
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "a.dat").write_text("payl")
    # Refuses any resumed request, serves a fresh one.
    body = (
        '  if [ "$have" != 0 ]; then echo 416; exit 22; fi\n'
        '  echo "$have" >> "$OFFSETS"\n'
        '  printf %s "$full" >> "$out"\n'
        '  echo 200\n'
        '  exit 0\n'
    )
    r, _ = run(f'dcache_pull reg/x:t {dest} && echo HIT', tmp_path,
               fake_curl_=fake_curl(body=body))
    assert "HIT" in r.stdout, r.stderr
    assert "range request refused" in r.stderr, r.stderr
    assert (dest / "a.dat").read_text() == "payload"


def test_a_layer_title_that_escapes_the_destination_is_refused(tmp_path):
    """The title is registry-controlled and is used as a path."""
    r, _ = run('dcache_pull reg/x:t out || echo REFUSED', tmp_path,
               fake_oras=oras_artifact(((".././../etc/cron.d/x", "evil"),)))
    assert "not a bare filename" in r.stderr, r.stderr
    assert not (tmp_path / "etc").exists()


def test_a_blob_cut_off_mid_transfer_resumes_on_the_next_pass(tmp_path):
    """The failure this exists for, end to end.

    The first request delivers part of the blob and dies the way the runner's
    teardowns do; the second must ask for the REMAINDER. Before byte ranges
    this was the deadlock: the partial file was discarded, so every pass
    re-fetched the same bytes and none ever finished.
    """
    body = (
        '  echo "$have" >> "$OFFSETS"\n'
        f'  n=$(cat {tmp_path}/n 2>/dev/null || echo 0); n=$((n+1)); echo $n > {tmp_path}/n\n'
        '  if [ "$n" = 1 ]; then\n'
        '    printf %s "${full:$have:3}" >> "$out"\n'
        '    echo "stream error: stream ID 1; PROTOCOL_ERROR; received from peer" >&2\n'
        '    echo 206; exit 18\n'
        '  fi\n'
        '  printf %s "${full:$have}" >> "$out"\n'
        '  echo 206\n'
        '  exit 0\n'
    )
    r, _ = run('dcache_pull reg/x:t out && echo HIT', tmp_path,
               fake_curl_=fake_curl(body=body))
    assert "HIT" in r.stdout, r.stderr
    # Pass 1 asked from 0 and got 3 bytes in; pass 2 asked from 3, not 0.
    assert offsets(tmp_path) == [0, 3], (
        f"the interrupted blob restarted from zero: {offsets(tmp_path)}")
    assert "PROTOCOL_ERROR" in r.stderr, r.stderr
    assert (tmp_path / "out" / "a.dat").read_text() == "payload"


def test_every_pass_makes_progress_when_each_one_is_cut_off(tmp_path):
    """Convergence, which is the property that actually failed on the runner.

    vwa/classifieds got eight passes and finished nothing, because each pass
    started over. With byte ranges the offsets must strictly increase — a pull
    interrupted every single time still completes, given enough passes.
    """
    body = (
        '  echo "$have" >> "$OFFSETS"\n'
        '  printf %s "${full:$have:2}" >> "$out"\n'
        '  if [ "$(stat -c %s "$out")" -lt "${#full}" ]; then\n'
        '    echo "stream error: PROTOCOL_ERROR; received from peer" >&2\n'
        '    echo 206; exit 18\n'
        '  fi\n'
        '  echo 206; exit 0\n'
    )
    r, _ = run('dcache_pull reg/x:t out && echo HIT', tmp_path,
               fake_curl_=fake_curl(body=body))
    assert "HIT" in r.stdout, r.stderr
    # 7 bytes, 2 per pass: 0, 2, 4, 6 — every pass strictly ahead of the last.
    assert offsets(tmp_path) == [0, 2, 4, 6], offsets(tmp_path)
    assert (tmp_path / "out" / "a.dat").read_text() == "payload"
    assert "resuming a.dat at 4/7 bytes" in r.stderr, r.stderr


def test_a_persistently_interrupted_pull_gives_up_after_its_passes(tmp_path):
    """Passes are bounded, and running out of them is NOT a miss.

    The manifest already proved the entry is there, so a pull that still fails
    is a real error. Returning 1 would send the caller off to re-derive an
    entry that exists — the exact cost this library was written to stop paying.
    """
    r, calls = run('dcache_pull reg/x:t out || echo MISS', tmp_path,
                   fake_curl_=fake_curl(body='  echo boom >&2; echo 000; exit 18\n'),
                   env={"DCACHE_PULL_TRIES": "3"})
    assert r.returncode != 0 and "MISS" not in r.stdout, r.stdout
    assert len(blob_calls(calls)) == 3, calls


def test_a_failed_transfer_reports_all_of_its_output(tmp_path):
    """The old `tail -n 3` is why run 31460767854's log could not be read.

    On a concurrent pull the last three lines are chosen by interleaving, not
    by relevance — and the attempt whose failure is fatal printed nothing at
    all, because only non-final attempts were logged.
    """
    noisy = ("\n".join(f'  echo "line{i}" >&2' for i in range(1, 6))
             + "\n  echo 000\n  exit 18\n")
    r, _ = run('dcache_pull reg/x:t out || true', tmp_path,
               fake_curl_=fake_curl(body=noisy),
               env={"DCACHE_PULL_TRIES": "1"})
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
    """A reset is transient by construction here; it must not read as a miss."""
    r, _ = run('dcache_pull reg/x:t out && echo "FORMAT=$DCACHE_HIT_FORMAT"', tmp_path,
               fake_curl_=fake_curl(body=(
                   f'  n=$(cat {tmp_path}/o.n 2>/dev/null || echo 0); n=$((n+1));'
                   f' echo $n > {tmp_path}/o.n\n'
                   f'  if [ "$n" = 1 ]; then echo "{RESET}" >&2; echo 000; exit 56; fi\n'
                   '  printf %s "${full:$have}" >> "$out"\n'
                   '  echo 206; exit 0\n')))
    assert "FORMAT=oras" in r.stdout, (
        f"a transient reset was reported as a cache miss: {r.stdout!r} {r.stderr!r}")


# --- writes ------------------------------------------------------------------


def test_a_push_retries_three_times_then_exits_nonzero(tmp_path):
    r, calls = run('DCACHE_RETRY_SLEEP=0 dcache_push reg/x:t . a.dat; echo "rc=$?"',
                   tmp_path, fake_oras="exit 1")
    assert len([c for c in calls if c.startswith("oras push")]) == 3, calls
    assert r.returncode != 0, "a failed push must be fatal (#80)"
