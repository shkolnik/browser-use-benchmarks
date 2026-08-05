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
