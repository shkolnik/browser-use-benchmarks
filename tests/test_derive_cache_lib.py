"""builder/stage-lib/derive-cache.sh — the protocol around the transfer.

What is left in the shell after the port: the oras manifest fetch, the digest
lock, the hit/miss/fatal decision table, and the push. The transfer engine it
delegates to is Python now, and its own tests are in test_derive_cache_fetch.py
— everything here that used to assert on curl invocations was really asserting
on that engine, and says more, more directly, from the other side.

Blobs are served by a real HTTP registry (tests/fake_registry.py) rather than a
faked `curl`, so a test that says "the pull happened" means bytes moved over a
socket. The manifest is still faked at the `oras` binary, because that is the
seam the shell still owns.

There is one format. The library used to read a legacy `FROM scratch` Docker
image as well, and migrate it to oras on the fly; all seven entries in
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

from tests.fake_registry import FakeRegistry, manifest_for

LIB = Path(__file__).resolve().parent.parent / "builder" / "stage-lib" / "derive-cache.sh"

PAYLOAD = {"a.dat": b"payload"}
MANIFEST = manifest_for(PAYLOAD)
MANIFEST_DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()


def _fake_oras(manifest):
    """A fake `oras manifest fetch` that honours `-o`, because the library asks
    for the manifest as a file and hashes those exact bytes to get the entry's
    digest. A fake that only ever wrote to stdout would leave the library
    hashing an empty file and would model no registry at all.

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


def oras_artifact(files=None):
    return _fake_oras((manifest_for(files) if files else MANIFEST).decode())


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


@pytest.fixture
def reg():
    """A live registry serving PAYLOAD, and the ref that reaches it."""
    with FakeRegistry(PAYLOAD) as r:
        r.blob_requests = r.behaviour.requests
        yield r


def run(body, tmp_path, fake_oras=ORAS_ARTIFACT, fake_docker="exit 1", env=None):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for name, script in (("oras", fake_oras), ("docker", fake_docker)):
        f = bin_ / name
        f.write_text(f"#!/bin/bash\necho \"{name} $*\" >> {tmp_path}/calls\n{script}\n")
        f.chmod(0o755)
    script = tmp_path / "t.sh"
    script.write_text(f"set -u\n. {LIB}\n" + textwrap.dedent(body))
    e = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}",
         "DCACHE_SKIP_ORAS_INSTALL": "1",
         # No test wants to sleep between passes, and the shipped 30s times ten
         # passes turns one mis-wired fake into an hour-long "hang" that reads
         # as an infinite loop. The retry policy itself is covered against the
         # engine, in test_derive_cache_fetch.py.
         "DCACHE_RETRY_SLEEP": "0",
         "DCACHE_PROGRESS_SECS": "0",
         # Isolate from the developer's real Docker credentials: whether this
         # machine happens to be logged in to ghcr must not change a result.
         "DOCKER_CONFIG": str(tmp_path / "no-docker-config"),
         **(env or {})}
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       env=e, cwd=tmp_path)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return r, calls


def lock(tmp_path, *lines):
    p = tmp_path / "test.lock"
    p.write_text("# a test lock\n" + "".join(f"{l}\n" for l in lines))
    return {"DCACHE_LOCK": str(p)}


def test_the_library_exists():
    assert LIB.is_file()


def test_a_pull_reads_the_oras_artifact(tmp_path, reg):
    r, calls = run(f'dcache_pull {reg.ref} out && echo "FORMAT=$DCACHE_HIT_FORMAT"', tmp_path)
    assert "FORMAT=oras" in r.stdout, r.stderr
    assert reg.blob_requests, 'no blob was fetched'
    # No docker anywhere on the read path: holding a cache image locally cost
    # roughly TWICE its content size on a runner whose disk is its scarcest
    # resource, and nothing ever read it after the export.
    assert not any(c.startswith("docker") for c in calls), calls
    assert (tmp_path / "out" / "a.dat").read_bytes() == PAYLOAD["a.dat"]


def test_a_layer_title_that_escapes_the_destination_is_refused(tmp_path, reg):
    """The title is registry-controlled and is used as a path."""
    r, _ = run(f'dcache_pull {reg.ref} out || echo REFUSED', tmp_path,
               fake_oras=oras_artifact({".././../etc/cron.d/x": b"evil"}))
    assert "not a bare filename" in r.stderr, r.stderr
    assert not (tmp_path / "etc").exists()


def test_a_miss_returns_one_and_says_so(tmp_path, reg):
    r, calls = run(f'dcache_pull {reg.ref} out || echo MISS', tmp_path, fake_oras="exit 1")
    assert "MISS" in r.stdout, r.stderr


def test_an_absent_entry_misses_immediately_without_burning_retries(tmp_path, reg):
    """A first build of a NEW image has no entry, and must not pay 3 attempts.

    This is why presence is decided by the manifest and not by the transfer:
    an absent ref returns no manifest, so there is nothing to retry.
    """
    r, calls = run(f'DCACHE_RETRY_SLEEP=0 dcache_pull {reg.ref} out || echo MISS',
                   tmp_path, fake_oras="exit 1")
    assert "MISS" in r.stdout, r.stderr
    assert not any(c.startswith("oras pull") for c in calls), (
        f"an absent entry was pulled anyway: {calls}")


def test_a_miss_carries_the_reason_it_missed(tmp_path, reg):
    """`denied`, a 503 and a genuinely absent tag must not look identical.

    Run 31268159790: webarena/shopping missed and spent two hours re-downloading
    41 GB, and the log could not say why, because the idiom of the day threw the
    error away. The manifest fetch is now the only thing that can explain a
    miss, so its stderr has to survive.
    """
    r, _ = run(f'dcache_pull {reg.ref} out || echo MISS', tmp_path,
               fake_oras='echo "denied: requested access to the resource is denied" >&2\nexit 1\n')
    assert "MISS" in r.stdout, r.stderr
    assert "denied" in r.stderr, f"the miss swallowed its cause: {r.stderr!r}"


def test_a_non_artifact_tag_fails_loudly_instead_of_missing(tmp_path, reg):
    """The legacy format is no longer read — and that must not read as a miss.

    A miss sends the caller off to re-derive; for wikipedia that is a ~24-hour,
    ~95 GB upstream fetch, spent because a tag held the wrong FORMAT rather
    than because anything was actually missing. Fail, and name the way out.
    """
    r, calls = run(f'dcache_pull {reg.ref} out || echo MISS', tmp_path,
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


def test_a_pinned_entry_whose_digest_matches_is_pulled(tmp_path, reg):
    r, calls = run(f'dcache_pull {reg.ref} out && echo "FORMAT=$DCACHE_HIT_FORMAT"',
                   tmp_path, env=lock(tmp_path, f"{reg.ref} {MANIFEST_DIGEST}"))
    assert "FORMAT=oras" in r.stdout, r.stderr
    assert reg.blob_requests, 'a matching pin must not stop the pull'


def test_the_digest_compared_is_the_sha256_of_the_raw_manifest_bytes(tmp_path, reg):
    """Not oras's descriptor, and not a hash of something re-serialized.

    An entry's digest is defined over the exact bytes the registry served, so
    this is the only definition that can ever agree with the registry — and
    with the way every digest in the lock was resolved. The unpinned path
    prints the line to add, which is where the computed value is observable.
    """
    r, _ = run(f'dcache_pull {reg.ref} out', tmp_path, env=lock(tmp_path))
    assert f"{reg.ref} {MANIFEST_DIGEST}" in r.stderr, r.stderr


def test_a_pinned_entry_whose_digest_moved_is_fatal(tmp_path, reg):
    r, calls = run(f'dcache_pull {reg.ref} out; echo "rc=$?"', tmp_path,
                   env=lock(tmp_path, f"{reg.ref} sha256:{'b' * 64}"))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "rc=" not in r.stdout, "a moved tag must stop the caller, not return to it"
    assert MANIFEST_DIGEST in r.stderr and "b" * 64 in r.stderr, (
        "both digests must be printed — the reader has to compare them")


def test_a_moved_tag_is_refused_before_any_bytes_are_transferred(tmp_path, reg):
    """The check is worth nothing if it lands after a 95 GB download."""
    _, calls = run(f'dcache_pull {reg.ref} out', tmp_path,
                   env=lock(tmp_path, f"{reg.ref} sha256:{'b' * 64}"))
    assert not reg.blob_requests, reg.blob_requests


def test_a_moved_tag_is_not_reported_as_a_miss(tmp_path, reg):
    """A miss sends the caller off to re-derive — up to ~24 h for wikipedia —
    over what is really "this is not the entry we pinned"."""
    r, _ = run(f'dcache_pull {reg.ref} out || echo MISS', tmp_path,
               env=lock(tmp_path, f"{reg.ref} sha256:{'b' * 64}"))
    assert "MISS" not in r.stdout, r.stdout


def test_an_unpinned_entry_is_pulled_and_names_the_line_to_add(tmp_path, reg):
    """A new image, or a deliberate RECIPE bump: expected, and not fatal."""
    r, calls = run(f'dcache_pull {reg.ref} out && echo "FORMAT=$DCACHE_HIT_FORMAT"',
                   tmp_path, env=lock(tmp_path, f"reg/other:t {MANIFEST_DIGEST}"))
    assert "FORMAT=oras" in r.stdout, r.stderr
    assert reg.blob_requests, 'no blob was fetched'
    assert "not pinned" in r.stderr and f"{reg.ref} {MANIFEST_DIGEST}" in r.stderr


def test_the_mismatch_escape_hatch_is_not_the_miss_escape_hatch(tmp_path, reg):
    """ALLOW_DERIVE_CACHE_MISS means "GHCR is unwell, derive instead". It must
    not also mean "accept content nobody pinned" — those are different risks,
    and the CI input is wired to the first one only.
    """
    r, _ = run(f'dcache_pull {reg.ref} out', tmp_path,
               env={**lock(tmp_path, f"{reg.ref} sha256:{'b' * 64}"),
                    "ALLOW_DERIVE_CACHE_MISS": "1"})
    assert r.returncode != 0, r.stdout + r.stderr


def test_the_mismatch_escape_hatch_downgrades_to_a_warning(tmp_path, reg):
    r, calls = run(f'dcache_pull {reg.ref} out && echo "FORMAT=$DCACHE_HIT_FORMAT"',
                   tmp_path,
                   env={**lock(tmp_path, f"{reg.ref} sha256:{'b' * 64}"),
                        "ALLOW_DERIVE_CACHE_DIGEST_MISMATCH": "1"})
    assert "FORMAT=oras" in r.stdout, r.stderr
    assert "WARNING" in r.stderr, r.stderr
    assert reg.blob_requests, 'no blob was fetched'


def test_an_entry_pinned_without_a_digest_is_still_pulled(tmp_path, reg):
    """"Pinned" and "pinned to a digest" are different claims: a ref listed
    with no digest column still makes a miss fatal (dcache_require), but there
    is nothing to compare on a hit, and inventing a failure there would break
    a lock that is merely incomplete."""
    r, _ = run(f'dcache_pull {reg.ref} out && echo "FORMAT=$DCACHE_HIT_FORMAT"',
               tmp_path, env=lock(tmp_path, reg.ref))
    assert "FORMAT=oras" in r.stdout, r.stderr


def test_a_comment_after_an_entry_is_not_read_as_its_digest(tmp_path, reg):
    r, _ = run(f'dcache_pull {reg.ref} out && echo "FORMAT=$DCACHE_HIT_FORMAT"',
               tmp_path,
               env=lock(tmp_path, f"{reg.ref} {MANIFEST_DIGEST}  # rebuilt 2026-08-11"))
    assert "FORMAT=oras" in r.stdout, r.stderr
