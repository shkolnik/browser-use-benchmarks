from pathlib import Path
import pytest
from builder.manifest import load_manifest

GOOD = '''
[[datasets]]
filename = "data.tar.gz"
sha256 = "a" * 0  # replaced below
urls = ["https://mirror1/x", "https://mirror2/x"]

[service]
healthcheck = "http://localhost:8080/"

[build.args]
FOO = "bar"
'''.replace('"a" * 0  # replaced below', '"%s"' % ("a" * 64))

def write(tmp_path: Path, text: str) -> Path:
    (tmp_path / "image.toml").write_text(text)
    return tmp_path

def test_load_good(tmp_path):
    m = load_manifest(write(tmp_path, GOOD))
    assert m.datasets[0].filename == "data.tar.gz"
    assert len(m.datasets[0].sha256) == 64
    assert m.datasets[0].urls[1].endswith("mirror2/x")
    assert m.healthcheck == "http://localhost:8080/"
    assert m.build_args == {"FOO": "bar"}

def test_no_datasets_ok(tmp_path):
    m = load_manifest(write(tmp_path, "[service]\nhealthcheck='http://x/'\n"))
    assert m.datasets == []

def test_bad_sha256_fails_loud(tmp_path):
    bad = GOOD.replace("a" * 64, "nothex")
    with pytest.raises(SystemExit, match="sha256"):
        load_manifest(write(tmp_path, bad))

def test_missing_urls_fails_loud(tmp_path):
    bad = GOOD.replace('urls = ["https://mirror1/x", "https://mirror2/x"]', "urls = []")
    with pytest.raises(SystemExit, match="urls"):
        load_manifest(write(tmp_path, bad))

def test_missing_manifest_fails_loud(tmp_path):
    with pytest.raises(SystemExit, match="image.toml"):
        load_manifest(tmp_path)
