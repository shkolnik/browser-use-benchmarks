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

PREPARE = GOOD + '''
[prepare]
script = "derive.sh"
outputs = ["derived.tar", "extra.tar.gz"]
'''

def test_prepare_good(tmp_path):
    (tmp_path / "derive.sh").write_text("#!/bin/bash\n")
    m = load_manifest(write(tmp_path, PREPARE))
    assert m.prepare.script == "derive.sh"
    assert m.prepare.outputs == ["derived.tar", "extra.tar.gz"]

def test_prepare_missing_script_file_fails_loud(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        load_manifest(write(tmp_path, PREPARE))

def test_prepare_empty_outputs_fails_loud(tmp_path):
    (tmp_path / "derive.sh").write_text("#!/bin/bash\n")
    bad = PREPARE.replace('outputs = ["derived.tar", "extra.tar.gz"]', "outputs = []")
    with pytest.raises(SystemExit, match="outputs"):
        load_manifest(write(tmp_path, bad))

def test_prepare_on_docker_save_fails_loud(tmp_path):
    (tmp_path / "derive.sh").write_text("#!/bin/bash\n")
    (tmp_path / "image.toml")  # DOCKER_SAVE fixture defined below in file order; inline here
    text = '''
[[datasets]]
filename = "app.tar"
sha256 = "%s"
urls = ["https://mirror/app.tar"]

[source]
kind = "docker-save"
dataset = "app.tar"
tag = "app:latest"

[prepare]
script = "derive.sh"
outputs = ["x"]
''' % ("b" * 64)
    with pytest.raises(SystemExit, match="prepare"):
        load_manifest(write(tmp_path, text))


# --- prepare_input: datasets that only feed the prepare script ----------------

PREPARE_INPUT = '''
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
''' % ("a" * 64, "b" * 64)

def write_with_script(tmp_path: Path, text: str) -> Path:
    (tmp_path / "derive.sh").write_text("#!/bin/bash\ntrue\n")
    return write(tmp_path, text)

def test_prepare_input_parsed(tmp_path):
    m = load_manifest(write_with_script(tmp_path, PREPARE_INPUT))
    by_name = {d.filename: d for d in m.datasets}
    assert by_name["upstream.tar"].prepare_input is True
    assert by_name["plain.tar"].prepare_input is False

def test_prepare_input_without_prepare_section_fails_loud(tmp_path):
    # A prepare-input dataset is never downloaded by the normal path, so with no
    # [prepare] script to fetch it on demand the build would look for a file
    # nothing is responsible for producing.
    bad = PREPARE_INPUT.split("[prepare]")[0]
    with pytest.raises(SystemExit, match="prepare_input"):
        load_manifest(write(tmp_path, bad))


def test_two_prepare_inputs_accepted(tmp_path):
    # Once rejected, because run_prepare exported ONE PREPARE_INPUT_SHA256 and a
    # second pin could not be represented in the cache key. webshop needs three:
    # a cache that still sends the build to a third-party mirror for the last
    # 4.9M file is not a checkpoint it can rebuild forward from. The key is now
    # PREPARE_INPUTS_DIGEST over the whole set — see test_docker.py.
    two = PREPARE_INPUT.replace(
        'filename = "plain.tar"\nsha256 = "%s"\nurls = ["https://metis/plain.tar"]' % ("b" * 64),
        'filename = "plain.tar"\nsha256 = "%s"\nurls = ["https://metis/plain.tar"]\nprepare_input = true' % ("b" * 64))
    m = load_manifest(write_with_script(tmp_path, two))
    assert [d.filename for d in m.datasets if d.prepare_input] == ["upstream.tar", "plain.tar"]

def test_retired_healthcheck_timeout_fails_loud(tmp_path):
    # Silently ignoring it would leave a 900 in the file doing nothing while
    # everyone assumed it was still the budget.
    (tmp_path / "image.toml").write_text(
        '[service]\nhealthcheck = "http://x/"\nhealthcheck_timeout_s = 900\n')
    try:
        load_manifest(tmp_path)
    except SystemExit as e:
        assert "healthcheck_timeout_s" in str(e)
        assert "HEALTHCHECK" in str(e)
    else:
        raise AssertionError("a retired key was accepted silently")

def test_reachability_timeout_defaults_and_overrides(tmp_path):
    (tmp_path / "image.toml").write_text('[service]\nhealthcheck = "http://x/"\n')
    assert load_manifest(tmp_path).reachability_timeout_s == 30
    (tmp_path / "image.toml").write_text(
        '[service]\nhealthcheck = "http://x/"\nreachability_timeout_s = 90\n')
    assert load_manifest(tmp_path).reachability_timeout_s == 90
