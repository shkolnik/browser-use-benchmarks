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

DOCKER_SAVE = '''
[[datasets]]
filename = "app.tar"
sha256 = "%s"
urls = ["https://mirror/app.tar"]

[source]
kind = "docker-save"
dataset = "app.tar"
tag = "app-upstream:latest"

[service]
healthcheck = "http://localhost:7770/"
''' % ("b" * 64)

def test_default_source_kind_is_build(tmp_path):
    m = load_manifest(write(tmp_path, GOOD))
    assert m.source.kind == "build"
    assert m.source.dataset is None and m.source.tag is None

def test_docker_save_source(tmp_path):
    m = load_manifest(write(tmp_path, DOCKER_SAVE))
    assert m.source.kind == "docker-save"
    assert m.source.dataset == "app.tar"
    assert m.source.tag == "app-upstream:latest"

def test_unknown_source_kind_fails_loud(tmp_path):
    bad = DOCKER_SAVE.replace('kind = "docker-save"', 'kind = "docker-export"')
    with pytest.raises(SystemExit, match="source.kind"):
        load_manifest(write(tmp_path, bad))

def test_docker_save_missing_tag_fails_loud(tmp_path):
    bad = DOCKER_SAVE.replace('tag = "app-upstream:latest"\n', "")
    with pytest.raises(SystemExit, match="source.tag"):
        load_manifest(write(tmp_path, bad))

def test_docker_save_missing_dataset_fails_loud(tmp_path):
    bad = DOCKER_SAVE.replace('dataset = "app.tar"\n', "")
    with pytest.raises(SystemExit, match="source.dataset"):
        load_manifest(write(tmp_path, bad))

def test_docker_save_dataset_must_match_declared_filename(tmp_path):
    bad = DOCKER_SAVE.replace('dataset = "app.tar"', 'dataset = "other.tar"')
    with pytest.raises(SystemExit, match="does not match any datasets"):
        load_manifest(write(tmp_path, bad))

def test_docker_save_rejects_build_section(tmp_path):
    bad = DOCKER_SAVE + '\n[build.args]\nFOO = "bar"\n'
    with pytest.raises(SystemExit, match="no .build. section"):
        load_manifest(write(tmp_path, bad))

def test_build_kind_rejects_stray_source_keys(tmp_path):
    bad = GOOD + '\n[source]\nkind = "build"\ntag = "x:latest"\n'
    with pytest.raises(SystemExit, match="only valid with kind"):
        load_manifest(write(tmp_path, bad))
