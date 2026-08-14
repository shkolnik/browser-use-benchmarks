"""builder/derive_cache_fetch.py — the derived-cache transfer engine.

These run against a real HTTP server (tests/fake_registry.py) rather than a
faked `curl`, so the behaviours under test are wire behaviours: a Range answered
with 206 or ignored with 200, a body cut short, a 429 carrying Retry-After, a
redirect to another host. The decision table around this engine — hit, miss,
pin enforcement, push — stays in test_derive_cache_lib.py against the shell.
"""
import hashlib
import json
import os

import pytest

from builder import derive_cache_fetch as dcf
from tests.fake_registry import Behaviour, FakeRegistry, manifest_for

PAYLOAD = {"a.dat": b"payload-a" * 40, "b.dat": b"payload-b" * 40}


class Recorder:
    """Collects the engine's log lines and the waits it asked for, so a test
    can assert on the retry policy without spending the wall-clock."""

    def __init__(self):
        self.lines = []
        self.waits = []

    def log(self, msg):
        self.lines.append(msg)

    def sleep(self, s):
        self.waits.append(s)

    @property
    def text(self):
        return "\n".join(self.lines)


def pull(tmp_path, registry, files=PAYLOAD, rec=None, **env):
    """Drive one pull, with any DCACHE_* overrides applied for its duration."""
    rec = rec or Recorder()
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        ok = dcf.fetch_layers(registry.ref, tmp_path / "out", manifest_for(files),
                              sleep=rec.sleep, log_=rec.log)
    finally:
        for k, v in saved.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    return ok, rec


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """No test may depend on the wall-clock or on jitter's randomness; the two
    tests that are ABOUT those set them back explicitly."""
    monkeypatch.setenv("DCACHE_RETRY_SLEEP", "10")
    monkeypatch.setenv("DCACHE_RETRY_JITTER", "0")
    monkeypatch.setenv("DCACHE_PROGRESS_SECS", "0")


def test_a_clean_pull_writes_every_layer(tmp_path):
    with FakeRegistry(PAYLOAD) as reg:
        ok, _ = pull(tmp_path, reg)
    assert ok
    for name, body in PAYLOAD.items():
        assert (tmp_path / "out" / name).read_bytes() == body


def test_a_blob_cut_off_mid_body_resumes_at_the_byte_it_stopped_on(tmp_path):
    """The failure this engine exists for. Before byte ranges, the partial file
    was discarded and every pass re-fetched the same bytes, so a transfer
    interrupted on a period shorter than itself never converged."""
    with FakeRegistry(PAYLOAD, Behaviour(cut_after=100)) as reg:
        ok, rec = pull(tmp_path, reg)
        by_layer = {}
        for path, rng in reg.behaviour.requests:
            by_layer.setdefault(path, []).append(
                int(rng.split("=")[1].split("-")[0]) if rng else 0)
    assert ok
    # Every layer's requests start at zero once and then strictly advance: a
    # pass that re-asked from zero is the deadlock this engine was built for.
    for path, starts in by_layer.items():
        assert starts == sorted(starts), (path, starts)
        assert len(set(starts)) == len(starts), (path, starts)
        assert starts[0] == 0, (path, starts)
    assert (tmp_path / "out" / "a.dat").read_bytes() == PAYLOAD["a.dat"]


def test_a_layer_already_on_disk_is_verified_not_refetched(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.dat").write_bytes(PAYLOAD["a.dat"])
    with FakeRegistry(PAYLOAD) as reg:
        ok, rec = pull(tmp_path, reg)
        fetched = [p for p, _ in reg.behaviour.requests]
    assert ok
    assert "a.dat verified, already complete" in rec.text
    a_digest = hashlib.sha256(PAYLOAD["a.dat"]).hexdigest()
    assert not any(a_digest in p for p in fetched), fetched


def test_a_leftover_file_of_the_right_size_and_wrong_bytes_is_replaced(tmp_path):
    """The only check standing between a half-written file from a previous run
    and a benchmark image built on corrupt data."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.dat").write_bytes(b"x" * len(PAYLOAD["a.dat"]))
    with FakeRegistry(PAYLOAD) as reg:
        ok, _ = pull(tmp_path, reg)
    assert ok
    assert (tmp_path / "out" / "a.dat").read_bytes() == PAYLOAD["a.dat"]


def test_bytes_that_hash_wrong_are_discarded_not_resumed(tmp_path):
    """A bad prefix that is re-appended every pass is immortal."""
    with FakeRegistry(PAYLOAD, Behaviour(corrupt=True)) as reg:
        ok, rec = pull(tmp_path, reg, DCACHE_PULL_TRIES="2")
    assert not ok
    assert "does not match" in rec.text
    assert not (tmp_path / "out" / "a.dat").exists()


def test_a_range_answered_with_the_whole_body_restarts_from_zero(tmp_path):
    """A 200 to a Range request means the server ignored it. Appending that to
    what we have yields a file of exactly the right length and garbage content
    — the digest catches it, but only after paying for the transfer."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.dat").write_bytes(PAYLOAD["a.dat"][:20])
    with FakeRegistry(PAYLOAD, Behaviour(ignore_range=True)) as reg:
        ok, rec = pull(tmp_path, reg)
    assert ok, rec.text
    assert "server ignored the range, restarting from zero" in rec.text
    assert (tmp_path / "out" / "a.dat").read_bytes() == PAYLOAD["a.dat"]


# --- the rate limit that caused the port -------------------------------------
# Run 31654975042 put eight ephemeral runners on GHCR under one GITHUB_TOKEN.
# webarena/gitlab and vwa/classifieds both died having spent every pass on
# `curl: (22) The requested URL returned error: 429`, while the one fetch that
# landed mid-storm ran at 68 MB/s and the S3 downloads on the same VMs took no
# 429s at all. It was a request-rate limiter meeting a retry budget built for a
# world without one: 8 passes a flat 30s apart, against a limiter hot for six
# minutes, with every VM retrying in lockstep.

def test_a_rate_limit_that_lets_up_ends_in_a_complete_pull(tmp_path):
    with FakeRegistry(PAYLOAD, Behaviour(rate_limit=4, retry_after=1)) as reg:
        ok, rec = pull(tmp_path, reg)
    assert ok, rec.text
    assert "rate-limited by the registry (HTTP 429), retry-after=1" in rec.text
    assert (tmp_path / "out" / "a.dat").read_bytes() == PAYLOAD["a.dat"]


def test_the_wait_doubles_across_passes_that_move_no_bytes(tmp_path):
    with FakeRegistry(PAYLOAD, Behaviour(rate_limit=99)) as reg:
        ok, rec = pull(tmp_path, reg, DCACHE_PULL_TRIES="5")
    assert not ok
    assert rec.waits == [10, 20, 40, 80], rec.waits


def test_a_pass_that_moved_bytes_resets_the_wait(tmp_path):
    """Backoff is for a resource refusing us, which is not what is happening
    while bytes are still landing. gitlab's part-00 landed at full speed on
    pass 5; penalising that would punish the incremental progress this engine
    exists to make."""
    with FakeRegistry({"a.dat": PAYLOAD["a.dat"]}, Behaviour(cut_after=100)) as reg:
        ok, rec = pull(tmp_path, reg, files={"a.dat": PAYLOAD["a.dat"]})
    assert ok, rec.text
    assert rec.waits and set(rec.waits) == {10}, rec.waits


def test_retry_after_is_a_floor_under_the_backoff_not_a_replacement():
    """Obeying a 5s ask from a limiter we have annoyed five times running puts
    us straight back into it."""
    assert dcf.backoff_wait(40, retry_after=1) == 40
    assert dcf.backoff_wait(10, retry_after=90) == 90


def test_a_retry_after_beyond_the_cap_cannot_park_the_job(monkeypatch):
    monkeypatch.setenv("DCACHE_RETRY_MAX_SLEEP", "60")
    assert dcf.backoff_wait(10, retry_after=3600) == 60
    assert dcf.backoff_wait(1000) == 60


def test_a_date_valued_retry_after_is_ignored():
    """RFC 9110 allows an HTTP-date: three legal formats plus the clock skew
    between us and the registry. Getting it wrong yields a wait that is useless
    or enormous, and the backoff already stands underneath it."""
    assert dcf.retry_after_seconds({"Retry-After": "Wed, 13 Aug 2026 01:00:00 GMT"}) is None
    assert dcf.retry_after_seconds({"Retry-After": "45"}) == 45
    assert dcf.retry_after_seconds({}) is None


def test_jitter_spreads_the_wait_without_leaving_its_bounds(monkeypatch):
    """Every VM in a fleet run boots within seconds of the others and hits the
    limiter together; a fixed schedule has them retrying in lockstep forever,
    each wave re-tripping the limit for everyone."""
    monkeypatch.setenv("DCACHE_RETRY_JITTER", "25")
    seen = {dcf.backoff_wait(100) for _ in range(40)}
    assert all(75 <= w <= 125 for w in seen), seen
    assert len(seen) > 1, f"nothing is jittered: {seen}"


def test_the_shipped_defaults_outlast_the_limiter_that_broke_the_fleet(monkeypatch, tmp_path):
    """A guard on the numbers, not the mechanism. gitlab and classifieds were
    refused for a solid six minutes; defaults that buy less patience than that
    reproduce the failure with prettier logs."""
    monkeypatch.delenv("DCACHE_RETRY_SLEEP")
    with FakeRegistry(PAYLOAD, Behaviour(rate_limit=99)) as reg:
        ok, rec = pull(tmp_path, reg)
    assert not ok
    assert rec.waits == [30, 60, 120, 240, 300, 300, 300, 300, 300], rec.waits
    assert sum(rec.waits) > 6 * 60


# --- credentials --------------------------------------------------------------

def test_authorization_is_dropped_when_a_redirect_crosses_hosts(tmp_path):
    """A registry answers a blob GET with a redirect to storage, and that URL
    carries its own signature. Forwarding our bearer token to the redirect
    target hands a credential to whoever the registry pointed us at. curl drops
    it; urllib does not, so the port had to put it back."""
    with FakeRegistry(PAYLOAD) as storage:
        with FakeRegistry(PAYLOAD, Behaviour(
                redirect_to=f"http://{storage.host}")) as reg:
            ok, rec = pull(tmp_path, reg)
        blob_auth = [a for a in storage.auth_seen]
    assert ok, rec.text
    assert blob_auth and all(a is None for a in blob_auth), blob_auth


def test_a_bearer_token_is_sent_to_the_registrys_own_blobs(tmp_path):
    with FakeRegistry(PAYLOAD) as reg:
        ok, _ = pull(tmp_path, reg)
        sent = [a for a in reg.auth_seen if a]
    assert ok
    assert any(a == "Bearer faketoken" for a in sent), reg.auth_seen


def test_credentials_come_from_the_docker_config(tmp_path, monkeypatch):
    """No credentials of its own: on the runner this is the GITHUB_TOKEN that
    the `login to ghcr` step logged in with."""
    import base64
    cfg = tmp_path / "dockercfg"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"auths": {"reg.example": {
        "auth": base64.b64encode(b"user:secret").decode()}}}))
    monkeypatch.setenv("DOCKER_CONFIG", str(cfg))
    monkeypatch.delenv("REGISTRY_TOKEN", raising=False)
    assert dcf.registry_creds("reg.example") == ("user", "secret")
    assert dcf.registry_creds("other.example") is None


def test_a_credential_helper_entry_reads_as_no_credentials(tmp_path, monkeypatch):
    """Anonymous is then tried, which is enough for a public package; a private
    one fails loudly rather than writing a 401 body into a blob file."""
    cfg = tmp_path / "dockercfg"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps(
        {"auths": {"reg.example": {}}, "credHelpers": {"reg.example": "ecr"}}))
    monkeypatch.setenv("DOCKER_CONFIG", str(cfg))
    monkeypatch.delenv("REGISTRY_TOKEN", raising=False)
    assert dcf.registry_creds("reg.example") is None


def test_registry_token_overrides_the_docker_config(tmp_path, monkeypatch):
    """A caller that only reads a manifest should not have to authenticate a
    docker daemon to do it, nor inherit whatever login an earlier job left."""
    import base64
    cfg = tmp_path / "dockercfg"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"auths": {"reg.example": {
        "auth": base64.b64encode(b"user:secret").decode()}}}))
    monkeypatch.setenv("DOCKER_CONFIG", str(cfg))
    monkeypatch.setenv("REGISTRY_TOKEN", "from-env")
    assert dcf.registry_creds("reg.example") == ("x", "from-env")


# --- manifest handling --------------------------------------------------------

def test_only_titled_layers_become_files():
    """oras's own rule; matching it is what keeps this equivalent to `oras
    pull`."""
    m = json.dumps({"layers": [
        {"digest": "sha256:" + "a" * 64, "size": 1,
         "annotations": {"org.opencontainers.image.title": "keep.dat"}},
        {"digest": "sha256:" + "b" * 64, "size": 1},
    ]}).encode()
    assert [l.title for l in dcf.parse_layers(m)] == ["keep.dat"]


@pytest.mark.parametrize("title", ["../../etc/cron.d/x", "sub/dir.dat", ".."])
def test_a_layer_title_that_is_not_a_bare_filename_is_refused(title):
    """The title comes from the registry and is used as a PATH. Refused rather
    than sanitised: every entry this fleet pushes is a basename, so one that
    isn't means something is wrong that stripping slashes would hide."""
    m = json.dumps({"layers": [
        {"digest": "sha256:" + "a" * 64, "size": 1,
         "annotations": {"org.opencontainers.image.title": title}}]}).encode()
    with pytest.raises(SystemExit, match="not a bare filename"):
        dcf.parse_layers(m)


def test_an_artifact_with_no_titled_layer_is_fatal_not_an_empty_hit(tmp_path):
    """It would extract to an empty directory and report a hit — the false-hit
    bug in a different costume."""
    with FakeRegistry(PAYLOAD) as reg:
        with pytest.raises(SystemExit, match="no file layers"):
            dcf.fetch_layers(reg.ref, tmp_path / "out",
                             json.dumps({"layers": []}).encode())


def test_localhost_is_served_over_plain_http():
    """The same rule Docker itself applies, and what lets the round-trip test
    run against a plain-HTTP `registry:2`."""
    assert dcf.scheme("localhost:5000") == "http"
    assert dcf.scheme("127.0.0.1:5000") == "http"
    assert dcf.scheme("ghcr.io") == "https"
