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
import re
import shlex
import subprocess
import textwrap
import time
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
    return _fake_oras(artifact_manifest(files))


def artifact_manifest(files=DEFAULT_FILES):
    layers = []
    for name, content in files:
        raw = content.encode()
        layers.append({
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "annotations": {"org.opencontainers.image.title": name},
        })
    return json.dumps(
        {"artifactType": "application/vnd.beep.derived.v1", "layers": layers})


def _fake_oras(manifest):
    """A fake `oras manifest fetch` that honours `-o`, because the library now
    asks for the manifest as a file and hashes those exact bytes to get the
    entry's digest. A fake that only ever wrote to stdout would leave the
    library hashing an empty file and would model no registry at all.

    `printf %s`, not `echo`: the trailing newline `echo` adds is not in the
    manifest the registry serves, and it would change the digest.
    """
    return (
        'if [ "$1 $2" = "manifest fetch" ]; then\n'
        '  shift 2\n'
        '  out=-\n'
        '  while [ $# -gt 0 ]; do\n'
        '    case "$1" in -o) out=$2; shift 2 ;; *) shift ;; esac\n'
        '  done\n'
        f'  if [ "$out" = - ]; then printf %s {shlex.quote(manifest)};\n'
        f'  else printf %s {shlex.quote(manifest)} > "$out"; fi\n'
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
        'out=""; url=""; hdr=/dev/null\n'
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -o) out=$2; shift 2 ;;\n'
        # -D is where the library asks for the response headers, and the only
        # way a Retry-After can reach it. Named for the same reason the timeout
        # flags below are: swept up by `-*` its value would be read as the URL.
        '    -D) hdr=$2; shift 2 ;;\n'
        '    -C|-H|-u|-w) shift 2 ;;\n'
        # Named rather than swept up by the -* arm below, so their values are
        # consumed as values. Left to `-*) shift`, each one would fall through
        # to the `*)` arm and be mistaken for the URL.
        '    --connect-timeout|--max-time|--speed-limit|--speed-time) shift 2 ;;\n'
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
ORAS_NOT_AN_ARTIFACT = _fake_oras(
    '{"mediaType":"application/vnd.oci.image.index.v1+json"}')


def rate_limited(retry_after="45", tries=None):
    """A fake curl that answers a blob request with a 429, as GHCR did.

    Writes the header block the library reads with -D, so Retry-After travels
    the same path it does from a real registry rather than being injected into
    the parser directly. `tries` limits the refusals: after that many the blob
    is served, which is how a test can assert the pull SURVIVES a rate limit
    rather than merely backs off from one.
    """
    ra = f'  printf "retry-after: {retry_after}\\r\\n" >> "$hdr"\n' if retry_after else ""
    count = ''
    if tries is not None:
        count = (f'  n=$(cat $TMP/429s 2>/dev/null || echo 0); n=$((n+1)); echo $n > $TMP/429s\n'
                 f'  if [ "$n" -gt {tries} ]; then\n'
                 '    echo "$have" >> "$OFFSETS"\n'
                 '    printf %s "${full:$have}" >> "$out"\n'
                 '    echo 206; exit 0\n'
                 '  fi\n')
    return (
        count
        + '  printf "HTTP/2 429\\r\\n" > "$hdr"\n'
        + ra
        + '  echo "curl: (22) The requested URL returned error: 429" >&2\n'
        '  echo 429\n'
        '  exit 22\n'
    )


# A `sleep` that records what it was asked to wait and returns at once. Opt-in
# per test: the progress watcher sleeps in a loop too, so a test that installs
# this must also set DCACHE_PROGRESS_SECS=0 or the watcher spins.
FAKE_SLEEP = "exit 0"


def waits(calls):
    """Seconds passed to every `sleep` the library ran, in order."""
    return [float(c.split()[1]) for c in calls if c.startswith("sleep ")]


def run(body, tmp_path, fake_oras=ORAS_ARTIFACT, fake_docker="exit 1",
        fake_curl_=None, env=None, fake_sleep=None):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    fakes = [("oras", fake_oras), ("docker", fake_docker),
             ("curl", fake_curl() if fake_curl_ is None else fake_curl_)]
    if fake_sleep is not None:
        fakes.append(("sleep", fake_sleep))
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
         "TMP": str(tmp_path),
         **(env or {})}
    # A None means "leave it unset", which is the only way to exercise a
    # default that the harness itself pins for everyone else.
    e = {k: v for k, v in e.items() if v is not None}
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


def test_a_blob_fetch_is_bounded_so_a_dead_socket_cannot_hang_the_pull(tmp_path):
    """Byte-range resume is unreachable while curl is still blocked in recv().

    curl applies no transfer timeout by default. A connection that is
    blackholed rather than reset — a link failing over mid-flow, on a
    connection that already exists — leaves it waiting on a socket the kernel
    still believes is open
    until tcp_retries2 expires, ~15 minutes and unbounded if the peer keeps
    the window alive. Nothing has failed, so no pass retries and no byte is
    resumed: the job is not slow, it is stopped, and it looks identical to
    working from outside. The stall floor is what converts that into an error
    the retry loop can act on.
    """
    _, calls = run('dcache_pull reg/x:t out', tmp_path)
    blob = blob_calls(calls)[0]
    assert "--speed-limit 102400" in blob, blob
    assert "--speed-time 60" in blob, blob
    assert "--connect-timeout 30" in blob, blob


def test_the_token_request_is_bounded_too(tmp_path):
    """It runs before every blob, and hanging here stalls the pull just as
    completely — with nothing growing on disk for the watcher to report on."""
    _, calls = run('dcache_pull reg/x:t out', tmp_path)
    tok = [c for c in calls if c.startswith("curl ") and "/token?" in c]
    assert tok, calls
    assert "--max-time 60" in tok[0], tok[0]


def test_progress_is_reported_while_a_blob_is_in_flight(tmp_path):
    """Slow and stuck must be different lines in the log, not the same silence.

    Between "resuming" and its outcome a 95 GB pull used to print nothing at
    all — hours in which a working transfer and a dead socket are reported
    identically. The watcher polls the file, because curl's own progress goes
    into dcache__transfer's capture buffer and is printed only on failure.
    """
    body = (
        '  echo "$have" >> "$OFFSETS"\n'
        '  i=$have\n'
        '  while [ "$i" -lt 3 ]; do\n'
        '    printf %s "${full:$i:1}" >> "$out"; i=$((i+1)); sleep 1\n'
        '  done\n'
        '  printf %s "${full:$i}" >> "$out"\n'
        '  echo 206; exit 0\n'
    )
    r, _ = run('dcache_pull reg/x:t out && echo HIT', tmp_path,
               fake_curl_=fake_curl(body=body),
               env={"DCACHE_PROGRESS_SECS": "1"})
    assert "HIT" in r.stdout, r.stderr
    assert re.search(r"derive-cache: a\.dat [1-6]/7 bytes \(\d+%, \d+ KB/s\)",
                     r.stderr), r.stderr
    assert "a.dat complete (7 bytes)" in r.stderr, r.stderr


def test_a_transfer_that_moves_no_bytes_is_named_as_stalled(tmp_path):
    """The distinction the operator actually needs at 3am.

    A transfer crawling at 200 KB/s and one that has been dead for four
    minutes both print nothing without this. STALLED, with a count of seconds
    since the last byte, is the line that says which one is happening.
    """
    body = '  sleep 3\n  echo "Operation timed out" >&2\n  echo 000; exit 28\n'
    r, _ = run('dcache_pull reg/x:t out || true', tmp_path,
               fake_curl_=fake_curl(body=body),
               env={"DCACHE_PROGRESS_SECS": "1", "DCACHE_PULL_TRIES": "1"})
    assert re.search(r"a\.dat STALLED at 0/7 bytes \(0%\) — no new bytes in \d+s",
                     r.stderr), r.stderr


def test_the_watcher_does_not_outlive_the_blob_it_watches(tmp_path):
    """A watcher left running holds the pull open for a whole poll interval
    per layer, and survives a fatal exit to print into a job already dead."""
    start = time.monotonic()
    r, _ = run('dcache_pull reg/x:t out && echo HIT', tmp_path,
               env={"DCACHE_PROGRESS_SECS": "60"})
    assert "HIT" in r.stdout, r.stderr
    # The `wait` after the kill would block for the full 60s poll if the
    # watcher were still alive.
    assert time.monotonic() - start < 20, "the pull waited on its own watcher"


def test_a_layer_title_that_escapes_the_destination_is_refused(tmp_path):
    """The title is registry-controlled and is used as a path."""
    r, _ = run('dcache_pull reg/x:t out || echo REFUSED', tmp_path,
               fake_oras=oras_artifact(((".././../etc/cron.d/x", "evil"),)))
    assert "not a bare filename" in r.stderr, r.stderr
    assert not (tmp_path / "etc").exists()


def test_a_blob_cut_off_mid_transfer_resumes_on_the_next_pass(tmp_path):
    """The failure this exists for, end to end.

    The first request delivers part of the blob and dies the way a connection
    cut mid-transfer does; the second must ask for the REMAINDER. Before byte
    ranges
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


# --- the digest lock, enforced on the hit path (#42) --------------------------
#
# `dcache_require` can only police a MISS, so until now the lock's digest
# column was a record of what was true when someone last looked. These cover
# the other half: on a HIT, the manifest the tag resolves to must be the
# manifest that was reviewed into builder/derived-cache.lock.

MANIFEST_DIGEST = "sha256:" + hashlib.sha256(artifact_manifest().encode()).hexdigest()


def lock(tmp_path, *lines):
    p = tmp_path / "test.lock"
    p.write_text("# a test lock\n" + "".join(f"{l}\n" for l in lines))
    return {"DCACHE_LOCK": str(p)}


def test_a_pinned_entry_whose_digest_matches_is_pulled(tmp_path):
    r, calls = run('dcache_pull reg/x:t out && echo "FORMAT=$DCACHE_HIT_FORMAT"',
                   tmp_path, env=lock(tmp_path, f"reg/x:t {MANIFEST_DIGEST}"))
    assert "FORMAT=oras" in r.stdout, r.stderr
    assert blob_calls(calls), "a matching pin must not stop the pull"


def test_the_digest_compared_is_the_sha256_of_the_raw_manifest_bytes(tmp_path):
    """Not oras's descriptor, and not a hash of something re-serialized.

    An entry's digest is defined over the exact bytes the registry served, so
    this is the only definition that can ever agree with the registry — and
    with the way every digest in the lock was resolved. The unpinned path
    prints the line to add, which is where the computed value is observable.
    """
    r, _ = run('dcache_pull reg/x:t out', tmp_path, env=lock(tmp_path))
    assert f"reg/x:t {MANIFEST_DIGEST}" in r.stderr, r.stderr


def test_a_pinned_entry_whose_digest_moved_is_fatal(tmp_path):
    r, calls = run('dcache_pull reg/x:t out; echo "rc=$?"', tmp_path,
                   env=lock(tmp_path, f"reg/x:t sha256:{'b' * 64}"))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "rc=" not in r.stdout, "a moved tag must stop the caller, not return to it"
    assert MANIFEST_DIGEST in r.stderr and "b" * 64 in r.stderr, (
        "both digests must be printed — the reader has to compare them")


def test_a_moved_tag_is_refused_before_any_bytes_are_transferred(tmp_path):
    """The check is worth nothing if it lands after a 95 GB download."""
    _, calls = run('dcache_pull reg/x:t out', tmp_path,
                   env=lock(tmp_path, f"reg/x:t sha256:{'b' * 64}"))
    assert not blob_calls(calls), calls


def test_a_moved_tag_is_not_reported_as_a_miss(tmp_path):
    """A miss sends the caller off to re-derive — up to ~24 h for wikipedia —
    over what is really "this is not the entry we pinned"."""
    r, _ = run('dcache_pull reg/x:t out || echo MISS', tmp_path,
               env=lock(tmp_path, f"reg/x:t sha256:{'b' * 64}"))
    assert "MISS" not in r.stdout, r.stdout


def test_an_unpinned_entry_is_pulled_and_names_the_line_to_add(tmp_path):
    """A new image, or a deliberate RECIPE bump: expected, and not fatal."""
    r, calls = run('dcache_pull reg/x:t out && echo "FORMAT=$DCACHE_HIT_FORMAT"',
                   tmp_path, env=lock(tmp_path, f"reg/other:t {MANIFEST_DIGEST}"))
    assert "FORMAT=oras" in r.stdout, r.stderr
    assert blob_calls(calls), calls
    assert "not pinned" in r.stderr and f"reg/x:t {MANIFEST_DIGEST}" in r.stderr


def test_the_mismatch_escape_hatch_is_not_the_miss_escape_hatch(tmp_path):
    """ALLOW_DERIVE_CACHE_MISS means "GHCR is unwell, derive instead". It must
    not also mean "accept content nobody pinned" — those are different risks,
    and the CI input is wired to the first one only.
    """
    r, _ = run('dcache_pull reg/x:t out', tmp_path,
               env={**lock(tmp_path, f"reg/x:t sha256:{'b' * 64}"),
                    "ALLOW_DERIVE_CACHE_MISS": "1"})
    assert r.returncode != 0, r.stdout + r.stderr


def test_the_mismatch_escape_hatch_downgrades_to_a_warning(tmp_path):
    r, calls = run('dcache_pull reg/x:t out && echo "FORMAT=$DCACHE_HIT_FORMAT"',
                   tmp_path,
                   env={**lock(tmp_path, f"reg/x:t sha256:{'b' * 64}"),
                        "ALLOW_DERIVE_CACHE_DIGEST_MISMATCH": "1"})
    assert "FORMAT=oras" in r.stdout, r.stderr
    assert "WARNING" in r.stderr, r.stderr
    assert blob_calls(calls), calls


def test_an_entry_pinned_without_a_digest_is_still_pulled(tmp_path):
    """"Pinned" and "pinned to a digest" are different claims: a ref listed
    with no digest column still makes a miss fatal (dcache_require), but there
    is nothing to compare on a hit, and inventing a failure there would break
    a lock that is merely incomplete."""
    r, _ = run('dcache_pull reg/x:t out && echo "FORMAT=$DCACHE_HIT_FORMAT"',
               tmp_path, env=lock(tmp_path, "reg/x:t"))
    assert "FORMAT=oras" in r.stdout, r.stderr


def test_a_comment_after_an_entry_is_not_read_as_its_digest(tmp_path):
    r, _ = run('dcache_pull reg/x:t out && echo "FORMAT=$DCACHE_HIT_FORMAT"',
               tmp_path,
               env=lock(tmp_path, f"reg/x:t {MANIFEST_DIGEST}  # rebuilt 2026-08-11"))
    assert "FORMAT=oras" in r.stdout, r.stderr


# A 429 is a different animal from every other failure this library handles.
# Run 31654975042 — the first fleet run to build all eleven images at once —
# put eight ephemeral runners on GHCR under a single GITHUB_TOKEN, and
# webarena/gitlab and vwa/classifieds both died in prepare having spent every
# pass on `curl: (22) The requested URL returned error: 429`. Nothing was wrong
# with the link: the one fetch that landed in the middle of it ran at 68 MB/s.
# The retry budget was the whole problem — eight passes, one attempt each,
# spaced a flat 30s, is about four minutes of patience against a limiter that
# stayed hot for six, and eight runners waiting the same flat 30s re-trip it
# together on every wave.


def test_a_rate_limited_blob_reports_the_delay_the_registry_asked_for(tmp_path):
    """Retry-After has to reach the log, or an operator cannot tell a
    rate-limited pull from a dead link — they are the same `curl (22)` line."""
    r, _ = run('dcache_pull reg/x:t out || echo GAVEUP', tmp_path,
               fake_curl_=fake_curl(body=rate_limited(retry_after="45")),
               env={"DCACHE_PULL_TRIES": "2", "DCACHE_RETRY_JITTER": "0",
                    "DCACHE_PROGRESS_SECS": "0"},
               fake_sleep=FAKE_SLEEP)
    assert "was rate-limited by the registry (HTTP 429), retry-after=45" in r.stderr, r.stderr
    assert "registry asked for 45s" in r.stderr, r.stderr


def test_the_wait_doubles_across_passes_that_move_no_bytes(tmp_path):
    """The decay. A limiter that is still hot must be met with a longer wait
    each time, not the same 30s that just failed."""
    r, calls = run('dcache_pull reg/x:t out || echo GAVEUP', tmp_path,
                   fake_curl_=fake_curl(body=rate_limited(retry_after=None)),
                   env={"DCACHE_PULL_TRIES": "5", "DCACHE_RETRY_SLEEP": "10",
                        "DCACHE_RETRY_JITTER": "0", "DCACHE_PROGRESS_SECS": "0"},
                   fake_sleep=FAKE_SLEEP)
    assert r.returncode != 0, r.stdout
    assert waits(calls) == [10, 20, 40, 80], waits(calls)


def test_the_doubling_is_capped(tmp_path):
    """Unbounded doubling would park a job for hours on its own arithmetic."""
    _, calls = run('dcache_pull reg/x:t out || true', tmp_path,
                   fake_curl_=fake_curl(body=rate_limited(retry_after=None)),
                   env={"DCACHE_PULL_TRIES": "6", "DCACHE_RETRY_SLEEP": "10",
                        "DCACHE_RETRY_MAX_SLEEP": "30", "DCACHE_RETRY_JITTER": "0",
                        "DCACHE_PROGRESS_SECS": "0"},
                   fake_sleep=FAKE_SLEEP)
    assert waits(calls) == [10, 20, 30, 30, 30], waits(calls)


def test_a_pass_that_moved_bytes_resets_the_wait(tmp_path):
    """Backoff is for a resource refusing us, which is not what is happening
    while bytes are still landing. A big entry pulled in fragments over many
    passes must not be punished for the incremental progress this loop exists
    to make — that is how gitlab's part-00 got in, at full speed, on pass 5."""
    body = (
        '  echo "$have" >> "$OFFSETS"\n'
        '  printf %s "${full:$have:1}" >> "$out"\n'
        '  if [ "$(stat -c %s "$out")" -lt "${#full}" ]; then\n'
        '    echo "cut off" >&2; echo 206; exit 18\n'
        '  fi\n'
        '  echo 206; exit 0\n'
    )
    r, calls = run('dcache_pull reg/x:t out && echo HIT', tmp_path,
                   fake_curl_=fake_curl(body=body),
                   env={"DCACHE_RETRY_SLEEP": "10", "DCACHE_RETRY_JITTER": "0",
                        "DCACHE_PROGRESS_SECS": "0"},
                   fake_sleep=FAKE_SLEEP)
    assert "HIT" in r.stdout, r.stderr
    # One byte per pass of a 7-byte payload: every pass advanced, so every wait
    # is the base one. A doubling here would mean progress was being penalised.
    assert waits(calls) == [10, 10, 10, 10, 10, 10], waits(calls)


def test_retry_after_is_a_floor_under_the_backoff_not_a_replacement(tmp_path):
    """Honouring a 5s Retry-After from a limiter we have annoyed five times
    running would put us straight back into it."""
    _, calls = run('dcache_pull reg/x:t out || true', tmp_path,
                   fake_curl_=fake_curl(body=rate_limited(retry_after="1")),
                   env={"DCACHE_PULL_TRIES": "4", "DCACHE_RETRY_SLEEP": "10",
                        "DCACHE_RETRY_JITTER": "0", "DCACHE_PROGRESS_SECS": "0"},
                   fake_sleep=FAKE_SLEEP)
    assert waits(calls) == [10, 20, 40], waits(calls)


def test_a_retry_after_longer_than_the_backoff_is_honoured_but_capped(tmp_path):
    """The registry knows something we do not, so a longer ask wins — up to the
    cap, so a misconfigured one cannot park the job."""
    _, calls = run('dcache_pull reg/x:t out || true', tmp_path,
                   fake_curl_=fake_curl(body=rate_limited(retry_after="90")),
                   env={"DCACHE_PULL_TRIES": "3", "DCACHE_RETRY_SLEEP": "10",
                        "DCACHE_RETRY_MAX_SLEEP": "60", "DCACHE_RETRY_JITTER": "0",
                        "DCACHE_PROGRESS_SECS": "0"},
                   fake_sleep=FAKE_SLEEP)
    assert waits(calls) == [60, 60], waits(calls)


def test_a_date_valued_retry_after_falls_back_to_the_backoff(tmp_path):
    """RFC 9110 allows an HTTP-date. Parsing one means three legal formats and
    the clock skew between us and the registry; getting it wrong yields a wait
    that is useless or enormous, so it is ignored and the backoff stands."""
    r, calls = run('dcache_pull reg/x:t out || true', tmp_path,
                   fake_curl_=fake_curl(
                       body=rate_limited(retry_after="Wed, 13 Aug 2026 01:00:00 GMT")),
                   env={"DCACHE_PULL_TRIES": "3", "DCACHE_RETRY_SLEEP": "10",
                        "DCACHE_RETRY_JITTER": "0", "DCACHE_PROGRESS_SECS": "0"},
                   fake_sleep=FAKE_SLEEP)
    assert waits(calls) == [10, 20], waits(calls)
    assert "retry-after=none" in r.stderr, r.stderr
    assert "registry asked for" not in r.stderr, r.stderr


def test_jitter_spreads_the_wait_without_leaving_the_bounds(tmp_path):
    """Every VM in a fleet run boots within seconds of the others and hits the
    limiter together; a fixed schedule has them retrying in lockstep forever,
    each wave re-tripping the limit for the whole fleet."""
    seen = set()
    for _ in range(6):
        d = tmp_path / f"j{len(seen)}{time.time()}"
        d.mkdir()
        _, calls = run('dcache_pull reg/x:t out || true', d,
                       fake_curl_=fake_curl(body=rate_limited(retry_after=None)),
                       env={"DCACHE_PULL_TRIES": "2", "DCACHE_RETRY_SLEEP": "100",
                            "DCACHE_RETRY_JITTER": "25", "DCACHE_PROGRESS_SECS": "0"},
                       fake_sleep=FAKE_SLEEP)
        seen.update(waits(calls))
    assert all(75 <= w <= 125 for w in seen), seen
    assert len(seen) > 1, f"every wait was identical, so nothing is jittered: {seen}"


def test_a_pull_survives_a_rate_limit_that_lets_up(tmp_path):
    """The point of all of it. Four refusals then a green light must end in a
    HIT — under the old flat budget this shape is what failed the fleet run."""
    r, _ = run('dcache_pull reg/x:t out && echo HIT', tmp_path,
               fake_curl_=fake_curl(body=rate_limited(retry_after="1", tries=4)),
               env={"DCACHE_RETRY_SLEEP": "1", "DCACHE_RETRY_JITTER": "0",
                    "DCACHE_PROGRESS_SECS": "0"},
               fake_sleep=FAKE_SLEEP)
    assert "HIT" in r.stdout, r.stderr
    assert (tmp_path / "out" / "a.dat").read_text() == "payload"


def test_the_default_pass_budget_outlasts_the_limiter_that_broke_the_fleet(tmp_path):
    """A guard on the numbers themselves, not on the mechanism.

    gitlab and classifieds were refused for a solid six minutes. The defaults
    have to buy more patience than that, or the next fleet run reproduces the
    failure with prettier logs. 10 passes from a 30s base, doubling to a 300s
    cap, is about half an hour.
    """
    _, calls = run('dcache_pull reg/x:t out || true', tmp_path,
                   fake_curl_=fake_curl(body=rate_limited(retry_after=None)),
                   env={"DCACHE_RETRY_JITTER": "0", "DCACHE_PROGRESS_SECS": "0",
                        "DCACHE_RETRY_SLEEP": None},
                   fake_sleep=FAKE_SLEEP)
    assert waits(calls) == [30, 60, 120, 240, 300, 300, 300, 300, 300], waits(calls)
    assert sum(waits(calls)) > 6 * 60, "less patience than the outage that caused this"
