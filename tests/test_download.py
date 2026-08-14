import hashlib
from pathlib import Path
import pytest
from builder.manifest import Dataset
from builder.download import ensure_dataset, sha256_of, FetchError

PAYLOAD = b"benchmark bytes"
GOOD_SHA = hashlib.sha256(PAYLOAD).hexdigest()

def ds(urls):
    return Dataset(filename="d.bin", sha256=GOOD_SHA, urls=urls)

def writer(payload):
    def fetch(url, dest: Path):
        dest.write_bytes(payload)
    return fetch

def test_downloads_and_verifies(tmp_path):
    out = ensure_dataset(ds(["u1"]), tmp_path, fetch=writer(PAYLOAD))
    assert out.read_bytes() == PAYLOAD

def test_cached_copy_skips_fetch(tmp_path):
    (tmp_path / "d.bin").write_bytes(PAYLOAD)
    def boom(url, dest):
        raise AssertionError("fetch called despite valid cache")
    assert ensure_dataset(ds(["u1"]), tmp_path, fetch=boom).read_bytes() == PAYLOAD

def test_mirror_rotation(tmp_path):
    calls = []
    def flaky(url, dest: Path):
        calls.append(url)
        if url == "bad":
            raise FetchError("conn reset")
        dest.write_bytes(PAYLOAD)
    out = ensure_dataset(ds(["bad", "good"]), tmp_path, fetch=flaky, attempts_per_url=1)
    assert out.read_bytes() == PAYLOAD and calls == ["bad", "good"]

def test_retries_same_url(tmp_path):
    calls = []
    def flaky(url, dest: Path):
        calls.append(url)
        if len(calls) < 2:
            raise FetchError("timeout")
        dest.write_bytes(PAYLOAD)
    ensure_dataset(ds(["u1"]), tmp_path, fetch=flaky, attempts_per_url=3)
    assert calls == ["u1", "u1"]

def test_checksum_mismatch_quarantines_and_dies(tmp_path):
    with pytest.raises(SystemExit, match="sha256 mismatch.*d.bin"):
        ensure_dataset(ds(["u1"]), tmp_path, fetch=writer(b"corrupt"))
    assert (tmp_path / "d.bin.quarantine").read_bytes() == b"corrupt"
    assert not (tmp_path / "d.bin").exists()

def test_stale_cache_requarantined_and_refetched(tmp_path):
    (tmp_path / "d.bin").write_bytes(b"old corrupt")
    out = ensure_dataset(ds(["u1"]), tmp_path, fetch=writer(PAYLOAD))
    assert out.read_bytes() == PAYLOAD

def test_all_mirrors_exhausted_dies_naming_urls(tmp_path):
    def dead(url, dest):
        raise FetchError("404")
    with pytest.raises(SystemExit, match="u1.*u2"):
        ensure_dataset(ds(["u1", "u2"]), tmp_path, fetch=dead, attempts_per_url=2)


# --- lazy prepare-input fetching ---------------------------------------------
# The normal download path must NOT fetch a dataset the derive script only needs
# on a cache miss: on a cold runner with a valid GHCR derived cache that download
# is pure waste (67.6 GB for webarena/shopping) and it is never even opened.

from builder.download import run_download
from builder.discover import ImageRef

def _image(tmp_path, name: str, toml: str) -> ImageRef:
    d = tmp_path / "images" / "bench" / name
    d.mkdir(parents=True)
    (d / "image.toml").write_text(toml)
    (d / "derive.sh").write_text("#!/bin/bash\ntrue\n")
    return ImageRef("bench", name, d)

LAZY_TOML = '''
[[datasets]]
filename = "upstream.tar"
sha256 = "%s"
urls = ["https://metis/upstream.tar"]
prepare_input = true

[[datasets]]
filename = "plain.tar"
sha256 = "%s"
urls = ["https://metis/plain.tar"]

[prepare]
script = "derive.sh"
outputs = ["derived.tar"]
''' % (hashlib.sha256(b"upstream").hexdigest(), hashlib.sha256(b"plain").hexdigest())

def _recording_fetch(fetched):
    def fetch(url, dest: Path):
        fetched.append(url)
        dest.write_bytes(b"upstream" if "upstream" in url else b"plain")
    return fetch

def test_download_skips_prepare_inputs(tmp_path):
    ref = _image(tmp_path, "svc", LAZY_TOML)
    fetched = []
    run_download([ref], tmp_path / "ds", fetch=_recording_fetch(fetched))
    assert fetched == ["https://metis/plain.tar"]

def test_download_prepare_inputs_fetches_only_those(tmp_path):
    ref = _image(tmp_path, "svc", LAZY_TOML)
    fetched = []
    run_download([ref], tmp_path / "ds", fetch=_recording_fetch(fetched),
                 prepare_inputs=True)
    assert fetched == ["https://metis/upstream.tar"]

def test_verification_reports_progress_and_still_returns_the_digest(tmp_path, monkeypatch):
    """Every pinned dataset is re-verified on each run, cache hit included, so
    this phase reads ~180G on a map build that downloads nothing. It used to
    print nothing at all, which is the shape of a hang. The digest is asserted
    alongside the reporting because a progress line that cost correctness would
    be a bad trade.
    """
    import builder.download as dl
    blob = b"x" * (4 << 20)
    f = tmp_path / "big.tar"
    f.write_bytes(blob)
    monkeypatch.setattr(dl, "PROGRESS_INTERVAL_S", 0)
    lines = []
    assert dl.sha256_of(f, lines.append) == hashlib.sha256(blob).hexdigest()
    assert lines, "hashing reported nothing"
    assert all(l.startswith("big.tar: verifying, ") for l in lines)
    assert "of 0.0G at" in lines[0]

def test_verification_is_silent_without_a_log(tmp_path):
    """The log is optional, and sha256_of is called in contexts that have none."""
    import builder.download as dl
    f = tmp_path / "small.tar"
    f.write_bytes(b"abc")
    assert dl.sha256_of(f) == hashlib.sha256(b"abc").hexdigest()
