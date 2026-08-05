from pathlib import Path
from builder.discover import ImageRef
from builder.manifest import Manifest
from builder.docker import build_cmd, push_cmds, poll_health

REF = ImageRef("miniwob", "server", Path("/repo/images/miniwob/server"))

def test_build_cmd():
    m = Manifest(build_args={"FOO": "bar"})
    cmd = build_cmd(REF, m, "ghcr.io/shkolnik", Path("/repo/datasets"), "20260805.abc1234")
    assert cmd == [
        "docker", "build",
        "--build-context", "datasets=/repo/datasets",
        "--build-arg", "FOO=bar",
        "-t", "ghcr.io/shkolnik/miniwob-server:20260805.abc1234",
        "-t", "ghcr.io/shkolnik/miniwob-server:latest",
        "/repo/images/miniwob/server",
    ]

def test_push_cmds():
    cmds = push_cmds(REF, "localhost:5000", "20260805.abc1234")
    assert cmds == [
        ["docker", "push", "localhost:5000/miniwob-server:20260805.abc1234"],
        ["docker", "push", "localhost:5000/miniwob-server:latest"],
    ]

class FakeResp:
    def __init__(self, code): self.status = code
    def __enter__(self): return self
    def __exit__(self, *a): return False

def test_poll_health_succeeds_after_flaps():
    seq = [ConnectionRefusedError(), FakeResp(500), FakeResp(200)]
    def opener(url, timeout):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    assert poll_health("http://x/", timeout_s=10, opener=opener, sleep=lambda s: None)

def test_poll_health_times_out():
    def opener(url, timeout):
        raise ConnectionRefusedError()
    assert not poll_health("http://x/", timeout_s=0, opener=opener, sleep=lambda s: None)

from builder.docker import run_with_retry
import pytest

class FakeProc:
    def __init__(self, rc): self.returncode = rc

def test_push_retry_banks_progress_and_succeeds():
    rcs = [1, 1, 0]
    calls = []
    def runner(cmd):
        calls.append(list(cmd))
        return FakeProc(rcs.pop(0))
    run_with_retry(["docker", "push", "x"], attempts=5, runner=runner, sleep=lambda s: None)
    assert len(calls) == 3 and all(c == ["docker", "push", "x"] for c in calls)

def test_push_retry_exhausted_fails_loud():
    def runner(cmd):
        return FakeProc(1)
    with pytest.raises(SystemExit, match="after 2 attempts"):
        run_with_retry(["docker", "push", "x"], attempts=2, runner=runner, sleep=lambda s: None)

from builder.docker import load_cmds, run_build
from builder.manifest import Source

def _docker_save_manifest():
    return Manifest(source=Source(kind="docker-save",
                                  dataset="shopping_final_0712.tar",
                                  tag="shopping_final_0712:latest"))

def test_load_cmds(tmp_path):
    (tmp_path / "shopping_final_0712.tar").write_bytes(b"tar")
    ref = ImageRef("webarena", "shopping", Path("/repo/images/webarena/shopping"))
    cmds = load_cmds(ref, _docker_save_manifest(), "ghcr.io/shkolnik", tmp_path, "20260805.abc1234")
    assert cmds == [
        ["docker", "load", "-i", str(tmp_path / "shopping_final_0712.tar")],
        ["docker", "tag", "shopping_final_0712:latest",
         "ghcr.io/shkolnik/webarena-shopping:20260805.abc1234"],
        ["docker", "tag", "shopping_final_0712:latest",
         "ghcr.io/shkolnik/webarena-shopping:latest"],
    ]

def test_load_cmds_missing_tar_fails_loud(tmp_path):
    ref = ImageRef("webarena", "shopping", Path("/repo/images/webarena/shopping"))
    with pytest.raises(SystemExit, match="run download"):
        load_cmds(ref, _docker_save_manifest(), "ghcr.io/shkolnik", tmp_path, "v")

def test_run_build_dispatches_docker_save_to_load_path(tmp_path, monkeypatch):
    import builder.docker as dk
    svc = tmp_path / "images" / "webarena" / "shopping"
    svc.mkdir(parents=True)
    (svc / "image.toml").write_text(
        '[[datasets]]\nfilename = "app.tar"\nsha256 = "%s"\nurls = ["https://m/app.tar"]\n'
        '[source]\nkind = "docker-save"\ndataset = "app.tar"\ntag = "app:latest"\n' % ("c" * 64))
    dsdir = tmp_path / "datasets"
    dsdir.mkdir()
    (dsdir / "app.tar").write_bytes(b"tar")
    calls = []
    monkeypatch.setattr(dk, "run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(dk, "version_tag", lambda root: "v1")
    ref = ImageRef("webarena", "shopping", svc)
    dk.run_build([ref], "ghcr.io/shkolnik", dsdir, tmp_path)
    assert [c[:2] for c in calls] == [["docker", "load"], ["docker", "tag"], ["docker", "tag"]]
    assert not any("build" in c for c in calls)
