from pathlib import Path
import pytest
from builder.discover import find_images, order_by_cost

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


def _repo_with_costs(tmp_path: Path, costs: dict) -> Path:
    for name, minutes in costs.items():
        b, s = name.split("/")
        d = tmp_path / "images" / b / s
        d.mkdir(parents=True)
        (d / "image.toml").write_text(
            "" if minutes is None else f"build_minutes = {minutes}\n")
    return tmp_path

def test_cost_order_is_longest_first(tmp_path):
    # The matrix is enqueued in this order and the quota gives fewer slots than
    # images, so whatever lands last waits for a slot to free and then extends
    # the run by its own duration. Alphabetical put miniwob (1 minute) first
    # and wikipedia (70) tenth.
    root = _repo_with_costs(tmp_path, {
        "miniwob/server": 1, "webarena/shopping": 87, "webarena/wikipedia": 70})
    assert [r.name for r in order_by_cost(find_images(root, "all"))] == [
        "webarena-shopping", "webarena-wikipedia", "miniwob-server"]

def test_an_unmeasured_image_is_scheduled_as_if_it_were_expensive(tmp_path):
    # The two errors are not symmetric. A cheap image scheduled early wastes
    # one slot for a few minutes; an expensive one scheduled last extends the
    # whole run by its own duration. So an image with no number goes first.
    root = _repo_with_costs(tmp_path, {
        "webarena/shopping": 87, "webarena/newcomer": None})
    assert [r.name for r in order_by_cost(find_images(root, "all"))] == [
        "webarena-newcomer", "webarena-shopping"]

def test_equal_costs_break_by_name(tmp_path):
    # Stable across runs of the same commit: an order that moved on its own
    # would make two runs of one commit hard to compare.
    root = _repo_with_costs(tmp_path, {
        "webarena/gitlab": 70, "webarena/wikipedia": 70, "vwa/classifieds": 70})
    assert [r.name for r in order_by_cost(find_images(root, "all"))] == [
        "vwa-classifieds", "webarena-gitlab", "webarena-wikipedia"]

def test_every_ci_image_carries_a_cost(tmp_path):
    # A missing number is not an error — order_by_cost schedules it first — but
    # every image that CI actually builds has been measured or estimated at
    # least once, and an unmeasured newcomer sitting at the head of the matrix
    # forever is a slot mis-spent on every run.
    from builder.manifest import load_manifest
    repo = Path(__file__).resolve().parent.parent
    missing = [r.name for r in find_images(repo, "all")
               if r.benchmark != "probe"
               and load_manifest(r.path).build_minutes is None]
    assert not missing, (
        f"{missing} have no build_minutes. Add one to image.toml — read it off "
        f"the build job's duration in the last green run.")
