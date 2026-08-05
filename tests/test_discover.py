from pathlib import Path
import pytest
from builder.discover import find_images

def make_repo(tmp_path: Path) -> Path:
    for b, s in [("miniwob", "server"), ("webarena", "gitlab"), ("webarena", "shopping")]:
        d = tmp_path / "images" / b / s
        d.mkdir(parents=True)
        (d / "image.toml").write_text("")
    # a service dir WITHOUT image.toml must be ignored
    (tmp_path / "images" / "webarena" / "notes").mkdir()
    return tmp_path

def test_all_finds_only_manifest_dirs(tmp_path):
    refs = find_images(make_repo(tmp_path), "all")
    assert sorted(r.name for r in refs) == ["miniwob-server", "webarena-gitlab", "webarena-shopping"]

def test_benchmark_target(tmp_path):
    refs = find_images(make_repo(tmp_path), "webarena")
    assert sorted(r.service for r in refs) == ["gitlab", "shopping"]

def test_service_target(tmp_path):
    refs = find_images(make_repo(tmp_path), "miniwob/server")
    assert [r.name for r in refs] == ["miniwob-server"]
    assert refs[0].path == tmp_path / "images" / "miniwob" / "server"

def test_unknown_target_fails_loud(tmp_path):
    with pytest.raises(SystemExit, match="no images match"):
        find_images(make_repo(tmp_path), "nope")
