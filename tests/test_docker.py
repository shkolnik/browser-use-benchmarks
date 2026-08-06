import json
from pathlib import Path
from builder.discover import ImageRef
from builder.manifest import Manifest, Prepare, Source
from builder import docker as docker_mod
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


def test_clean_cmds_removes_registry_tags():
    m = Manifest()
    [cmd] = docker_mod.clean_cmds(REF, m, "ghcr.io/x", "v1")
    assert cmd[:4] == ["docker", "image", "rm", "-f"]
    assert "ghcr.io/x/miniwob-server:v1" in cmd and "ghcr.io/x/miniwob-server:latest" in cmd


def test_clean_cmds_docker_save_removes_embedded_tag():
    m = Manifest(source=Source(kind="docker-save", dataset="d.tar", tag="upstream:latest"))
    [cmd] = docker_mod.clean_cmds(REF, m, "ghcr.io/x", "v1")
    assert "upstream:latest" in cmd


def test_run_clean_continues_past_failures(monkeypatch):
    calls, logs = [], []

    class R:
        returncode = 1

    monkeypatch.setattr(docker_mod, "version_tag", lambda root: "v1")
    monkeypatch.setattr(docker_mod, "load_manifest", lambda path: Manifest())
    docker_mod.run_clean([REF], "ghcr.io/x", Path("/repo"),
                         runner=lambda c: calls.append(c) or R(), log=logs.append)
    assert calls, "runner never invoked"
    assert any("warning" in l for l in logs)


def test_run_prepare_skips_only_when_outputs_are_STAMPED(tmp_path, capsys):
    # This used to assert that mere presence was enough to skip. It was not:
    # an empty dump left by a superseded recipe satisfied it for days. The
    # artifact must still match the stamp a successful derive wrote.
    from builder.docker import run_prepare, prepare_fingerprint, prepare_stamp_path
    (tmp_path / "derive.sh").write_text("echo derive\n")
    (tmp_path / "derived.tar").write_text("x")
    m = Manifest(prepare=Prepare("derive.sh", ["derived.tar"]))
    ref = ImageRef("bench", "svc", tmp_path)
    stamp = prepare_stamp_path(tmp_path, ref)
    stamp.parent.mkdir(parents=True)
    stamp.write_text(json.dumps(prepare_fingerprint(ref, m, tmp_path)))
    run_prepare(ref, m, "ghcr.io/x", tmp_path)  # must not try to run the script
    assert "skipping" in capsys.readouterr().out


def test_run_prepare_runs_script_with_env(tmp_path):
    from builder.docker import run_prepare
    from builder.manifest import Manifest, Prepare
    from builder.discover import ImageRef
    imgdir = tmp_path / "img"
    dsdir = tmp_path / "ds"
    imgdir.mkdir()
    dsdir.mkdir()
    (imgdir / "derive.sh").write_text(
        '#!/bin/bash\necho "$REGISTRY" > "$DATASETS_DIR/derived.tar"\n')
    m = Manifest(prepare=Prepare("derive.sh", ["derived.tar"]))
    run_prepare(ImageRef("bench", "svc", imgdir), m, "ghcr.io/x", dsdir)
    assert (dsdir / "derived.tar").read_text().strip() == "ghcr.io/x"


def test_run_prepare_missing_output_fails_loud(tmp_path):
    import pytest
    from builder.docker import run_prepare
    from builder.manifest import Manifest, Prepare
    from builder.discover import ImageRef
    (tmp_path / "derive.sh").write_text("#!/bin/bash\ntrue\n")
    m = Manifest(prepare=Prepare("derive.sh", ["never-made.tar"]))
    with pytest.raises(SystemExit, match="did not produce"):
        run_prepare(ImageRef("bench", "svc", tmp_path), m, "ghcr.io/x", tmp_path)


def test_run_prepare_script_failure_fails_loud(tmp_path):
    import pytest
    from builder.docker import run_prepare
    from builder.manifest import Manifest, Prepare
    from builder.discover import ImageRef
    (tmp_path / "derive.sh").write_text("#!/bin/bash\nexit 3\n")
    m = Manifest(prepare=Prepare("derive.sh", ["x.tar"]))
    with pytest.raises(SystemExit, match="failed"):
        run_prepare(ImageRef("bench", "svc", tmp_path), m, "ghcr.io/x", tmp_path)


def test_version_tag_uses_commit_date_not_wall_clock(tmp_path):
    # The tag must be a pure function of HEAD: CI computes it independently in
    # the build, push, and clean steps, and a slow build crossing midnight made
    # push retry a tag that was never created (run 31071250255, webarena/gitlab).
    import subprocess
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_AUTHOR_DATE": "2020-01-02T03:04:05Z",
        "GIT_COMMITTER_DATE": "2020-01-02T03:04:05Z",
        "TZ": "UTC",
    }
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty",
                    "-m", "x"], check=True, env=env)
    sha = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert docker_mod.version_tag(tmp_path) == f"20200102.{sha}"

# ===# smoke — the step that stands between "the image assembled" and "the image is
# published". These pin the targeting, because a smoke that quietly brings up
# the WRONG set of services is worse than no smoke: it reports success.
# ===
def _smoke_repo(tmp_path, compose_body="services:\n  reddit:\n    image: x\n"):
    d = tmp_path / "images" / "webarena"
    (d / "reddit").mkdir(parents=True)
    (d / "compose.yml").write_text(compose_body)
    (d / "reddit" / "image.toml").write_text(
        '[service]\nhealthcheck = "http://localhost:9999/forums"\n')
    return tmp_path

def test_smoke_brings_up_only_the_targeted_service(tmp_path, monkeypatch):
    # webarena's compose declares four services; a reddit build has only built
    # one of them. Bringing the file up wholesale would try to pull the other
    # three (~75G) — so the `up` command must name reddit explicitly.
    repo = _smoke_repo(tmp_path)
    ref = ImageRef("webarena", "reddit", repo / "images" / "webarena" / "reddit")
    cmds = []
    monkeypatch.setattr(docker_mod, "run", lambda c: cmds.append(c))
    monkeypatch.setattr(docker_mod, "compose_services",
                        lambda p: ["shopping", "shopping-admin", "reddit", "gitlab"])
    monkeypatch.setattr(docker_mod, "poll_health", lambda url: True)
    docker_mod.run_smoke([ref], repo)
    up = cmds[0]
    assert up[-1] == "reddit"
    for other in ("shopping", "shopping-admin", "gitlab"):
        assert other not in up
    assert cmds[-1][-2:] == ["down", "-v"]

def test_smoke_fails_loud_when_compose_lacks_the_service(tmp_path, monkeypatch):
    # A renamed service would otherwise make `up` a no-op and smoke a rubber
    # stamp — compose exits 0 when told to start nothing.
    repo = _smoke_repo(tmp_path)
    ref = ImageRef("webarena", "reddit", repo / "images" / "webarena" / "reddit")
    monkeypatch.setattr(docker_mod, "run", lambda c: None)
    monkeypatch.setattr(docker_mod, "compose_services", lambda p: ["postmill"])
    monkeypatch.setattr(docker_mod, "poll_health", lambda url: True)
    try:
        docker_mod.run_smoke([ref], repo)
    except SystemExit as e:
        assert "declares no service named reddit" in str(e)
    else:
        raise AssertionError("run_smoke accepted a compose file missing the service")

def test_smoke_fails_loud_when_image_never_becomes_healthy(tmp_path, monkeypatch):
    repo = _smoke_repo(tmp_path)
    ref = ImageRef("webarena", "reddit", repo / "images" / "webarena" / "reddit")
    cmds = []
    monkeypatch.setattr(docker_mod, "run", lambda c: cmds.append(c))
    monkeypatch.setattr(docker_mod, "compose_services", lambda p: ["reddit"])
    monkeypatch.setattr(docker_mod, "poll_health", lambda url: False)
    try:
        docker_mod.run_smoke([ref], repo)
    except SystemExit as e:
        assert "smoke FAILED" in str(e)
    else:
        raise AssertionError("run_smoke passed an image that never served")
    # ...and it still tears the stack down, or the runner leaks a 73G container.
    assert cmds[-1][-2:] == ["down", "-v"]

def test_compose_services_parses_docker_output():
    out = "shopping\nshopping-admin\nreddit\ngitlab\n"
    got = docker_mod.compose_services(Path("/repo/images/webarena/compose.yml"),
                                      check_output=lambda cmd, text: out)
    assert got == ["shopping", "shopping-admin", "reddit", "gitlab"]

# ===# prepare provenance stamp — artifacts are reused only when they still match
# what a successful derivation left behind. Presence alone let an empty dump
# from a superseded recipe survive on the runner indefinitely.
# ===
def _prep_image(tmp_path, script_body="echo hi\n"):
    img = tmp_path / "images" / "webarena" / "reddit"
    img.mkdir(parents=True)
    (img / "derive-backup.sh").write_text(script_body)
    ds = tmp_path / "datasets"
    ds.mkdir()
    ref = ImageRef("webarena", "reddit", img)
    m = Manifest(prepare=Prepare("derive-backup.sh", ["reddit_db.sql.gz"]))
    return ref, m, ds

def _write_output(ds, size=1000):
    (ds / "reddit_db.sql.gz").write_bytes(b"x" * size)

def test_prepare_refuses_unstamped_artifacts(tmp_path):
    # The exact production failure: the file is present, so the old code
    # skipped the derive entirely and built on it.
    ref, m, ds = _prep_image(tmp_path)
    _write_output(ds)
    assert docker_mod.prepare_reuse_check(ref, m, ds) == \
        "no provenance stamp — artifacts of unknown origin"

def test_prepare_reuses_stamped_artifacts(tmp_path):
    ref, m, ds = _prep_image(tmp_path)
    _write_output(ds)
    stamp = docker_mod.prepare_stamp_path(ds, ref)
    stamp.parent.mkdir(parents=True)
    stamp.write_text(json.dumps(docker_mod.prepare_fingerprint(ref, m, ds)))
    assert docker_mod.prepare_reuse_check(ref, m, ds) is None

def test_prepare_rederives_when_the_script_changes(tmp_path):
    # This is the mechanism that failed in production: bumping RECIPE inside
    # the derive script must invalidate everything it previously produced.
    ref, m, ds = _prep_image(tmp_path, "RECIPE=r1\n")
    _write_output(ds)
    stamp = docker_mod.prepare_stamp_path(ds, ref)
    stamp.parent.mkdir(parents=True)
    stamp.write_text(json.dumps(docker_mod.prepare_fingerprint(ref, m, ds)))
    assert docker_mod.prepare_reuse_check(ref, m, ds) is None
    (ref.path / "derive-backup.sh").write_text("RECIPE=r2\n")
    assert docker_mod.prepare_reuse_check(ref, m, ds) == \
        "derive-backup.sh changed since these artifacts were derived"

def test_prepare_rederives_when_an_artifact_is_truncated(tmp_path):
    ref, m, ds = _prep_image(tmp_path)
    _write_output(ds, size=1000)
    stamp = docker_mod.prepare_stamp_path(ds, ref)
    stamp.parent.mkdir(parents=True)
    stamp.write_text(json.dumps(docker_mod.prepare_fingerprint(ref, m, ds)))
    _write_output(ds, size=12)   # truncated after the fact
    assert docker_mod.prepare_reuse_check(ref, m, ds) == \
        "reddit_db.sql.gz is 12 bytes, was 1000 when derived"

def test_run_prepare_stamps_after_a_successful_derive(tmp_path):
    # A script that really produces the artifact, run through the real code
    # path — then a second call must skip it.
    ref, m, ds = _prep_image(
        tmp_path, 'printf "%s" "$(head -c 500 /dev/zero | tr "\\0" "y")" '
                  '> "$DATASETS_DIR/reddit_db.sql.gz"\n')
    docker_mod.run_prepare(ref, m, "ghcr.io/x", ds)
    assert docker_mod.prepare_stamp_path(ds, ref).is_file()
    assert (ds / "reddit_db.sql.gz").stat().st_size == 500
    assert docker_mod.prepare_reuse_check(ref, m, ds) is None

def test_run_prepare_RE_DERIVES_over_an_unstamped_artifact(tmp_path):
    # The production failure, end to end at the level it actually happened:
    # a stale artifact sits in the runner's datasets dir with no stamp, and
    # run_prepare must run the derive script anyway rather than build on it.
    # Asserting this through prepare_reuse_check alone does NOT pin it — the
    # bug was in run_prepare's use of the check, and a regression that trusts
    # presence again leaves every check-level test green.
    ref, m, ds = _prep_image(
        tmp_path, 'printf "%s" REDERIVED > "$DATASETS_DIR/reddit_db.sql.gz"\n')
    (ds / "reddit_db.sql.gz").write_bytes(b"stale empty artifact")
    docker_mod.run_prepare(ref, m, "ghcr.io/x", ds)
    assert (ds / "reddit_db.sql.gz").read_bytes() == b"REDERIVED", \
        "run_prepare reused an unstamped artifact instead of re-deriving"

def test_run_prepare_exports_repo_root_and_image(tmp_path):
    # The derive script fetches its own upstream tar on a cache miss, so it
    # needs to find bin/build and name itself as a target.
    from builder.docker import run_prepare
    from builder.manifest import Manifest, Prepare
    from builder.discover import ImageRef
    imgdir = tmp_path / "img"
    dsdir = tmp_path / "ds"
    imgdir.mkdir()
    dsdir.mkdir()
    (imgdir / "derive.sh").write_text(
        '#!/bin/bash\necho "$IMAGE $REPO_ROOT" > "$DATASETS_DIR/derived.tar"\n')
    m = Manifest(prepare=Prepare("derive.sh", ["derived.tar"]))
    run_prepare(ImageRef("bench", "svc", imgdir), m, "ghcr.io/x", dsdir)
    image, repo_root = (dsdir / "derived.tar").read_text().split()
    assert image == "bench/svc"
    assert (Path(repo_root) / "bin" / "build").is_file()
