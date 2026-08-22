"""Invariants over the captured benchmark task suites.

The suites are vendored upstream bytes plus a first-party manifest describing
them. Both halves rot in different ways: a vendored file can be edited in place
and stop being what its provenance claims, and a manifest can name a service or
a placeholder token that no longer exists. Read the real files rather than
fixtures — the invariant is about what this repo ships.

Hermetic: this reads only what is committed. The one thing it deliberately does
not check is MiniWoB's per-task episode deadline, which lives in the pinned HTML
archive rather than in git; tasks/miniwob/derive-tasks.py --check covers that
where the archive is present.
"""

import hashlib
import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SUITES = sorted(REPO.glob("tasks/*/*/suite.toml"))
IMAGES = {
    f"{p.parent.parent.name}/{p.parent.name}"
    for p in REPO.glob("images/*/*/image.toml")
}

REQUIRED_SECTIONS = ("suite", "upstream", "tasks", "requires", "reset", "scoring")


def load(path: Path) -> dict:
    return tomllib.loads(path.read_text())


CASES = [pytest.param(p, id=str(p.parent.relative_to(REPO / "tasks"))) for p in SUITES]


def test_there_are_suites():
    # A glob that found nothing would make every test below vacuous, and a
    # vacuous suite is indistinguishable from a passing one.
    assert CASES, "no tasks/*/*/suite.toml was discovered"


@pytest.mark.parametrize("path", CASES)
def test_manifest_carries_the_skeleton(path):
    data = load(path)
    missing = [s for s in REQUIRED_SECTIONS if s not in data]
    assert not missing, f"missing section(s): {', '.join(missing)}"


@pytest.mark.parametrize("path", CASES)
def test_upstream_is_pinned_to_a_commit(path):
    upstream = load(path)["upstream"]
    for key in ("repo", "license"):
        assert upstream.get(key), f"[upstream].{key} is empty"
    commits = [v for k, v in upstream.items() if k.startswith("commit")]
    assert commits, "[upstream] names no commit"
    for commit in commits:
        assert re.fullmatch(r"[0-9a-f]{40}", str(commit)), (
            f"{commit!r} is not a full commit sha — a branch or a tag moves, and "
            "a moving ref cannot say which bytes were vendored"
        )


@pytest.mark.parametrize("path", CASES)
def test_vendored_files_match_their_recorded_bytes(path):
    entries = load(path)["upstream"].get("files", [])
    assert entries, "[[upstream.files]] records nothing"
    for entry in entries:
        target = resolve(path, entry["path"])
        assert target.is_file(), f"{entry['path']} does not exist"
        raw = target.read_bytes()
        assert len(raw) == entry["bytes"], (
            f"{entry['path']}: manifest says {entry['bytes']} bytes, file is {len(raw)}"
        )
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"], (
            f"{entry['path']}: sha256 does not match the manifest"
        )


def resolve(manifest: Path, rel: str) -> Path:
    """A vendored path is relative to the suite dir, or to the benchmark dir.

    Scoring code and licences are shared by every suite of a benchmark and live
    one level up, so both roots are legitimate and the manifest does not have to
    say which it meant.
    """
    for root in (manifest.parent, manifest.parent.parent):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return manifest.parent / rel


@pytest.mark.parametrize("path", CASES)
def test_required_services_are_named_the_way_images_are(path):
    requires = load(path)["requires"]
    declared = list(requires.get("services", []))
    not_built = set(requires.get("not_built", []))
    not_deployed = set(requires.get("not_deployed", []))
    assert declared, "[requires].services is empty"
    assert not (not_built & not_deployed), (
        f"{sorted(not_built & not_deployed)} is both not_built and not_deployed"
    )
    for name in not_built | not_deployed:
        assert name in declared, f"{name!r} is not among [requires].services"
    for name in declared:
        assert name in IMAGES or name in not_built, (
            f"{name!r} is not an images/*/*/ directory and is not declared not_built"
        )


@pytest.mark.parametrize("path", CASES)
def test_not_built_names_no_image_this_repo_has(path):
    # The two gaps are different problems with different fixes, and the day one
    # of them closes the manifests still claiming it should fail rather than
    # quietly describe a fleet that has moved on.
    for name in load(path)["requires"].get("not_built", []):
        assert name not in IMAGES, (
            f"{name!r} is declared not_built but images/{name}/ exists — it belongs "
            "in not_deployed, or nowhere"
        )


DEPLOYED = {
    name
    for name in yaml.safe_load((REPO / "deploy" / "compose.yml").read_text())["services"]
}


@pytest.mark.parametrize("path", CASES)
def test_not_deployed_names_a_built_image_the_fleet_omits(path):
    for name in load(path)["requires"].get("not_deployed", []):
        assert name in IMAGES, f"{name!r} is declared not_deployed but has no image"
        service = name.split("/")[-1]
        assert service not in DEPLOYED, (
            f"{name!r} is declared not_deployed but deploy/compose.yml brings up "
            f"{service!r} — the manifest describes a fleet this repo no longer has"
        )


@pytest.mark.parametrize("path", CASES)
def test_runnable_and_blocked_account_for_every_task(path):
    data = load(path)
    requires = data["requires"]
    total = data["suite"]["task_count"]
    assert requires["runnable"] + requires["blocked"] == total, (
        f"runnable {requires['runnable']} + blocked {requires['blocked']} "
        f"does not account for the suite's {total} tasks"
    )


def test_open_decision_ids_are_namespaced_and_unique():
    seen = {}
    for path in SUITES:
        benchmark = path.parent.parent.name
        ids = []
        for decision in load(path).get("open_decisions", []):
            ident = decision["id"]
            assert re.fullmatch(r"[a-z][a-z0-9-]*", ident), f"{ident!r} is not a slug"
            assert ident.startswith((f"{benchmark}-", "fleet-")), (
                f"{ident!r} in {path} is namespaced to neither {benchmark} nor fleet"
            )
            ids.append(ident)
            seen.setdefault(ident, []).append((path, decision))
        assert len(ids) == len(set(ids)), f"{path} repeats an open-decision id"
    assert seen, "no suite records an open decision"

    # One question asked in several places has to be answerable once. Two suites
    # carrying the same id with different wording would take one answer and act
    # on it differently, so a repeated id must repeat the whole question.
    for ident, occurrences in seen.items():
        first_path, first = occurrences[0]
        for path, decision in occurrences[1:]:
            for key in ("question", "options"):
                assert decision[key] == first[key], (
                    f"{ident!r} states a different {key} in {path} than in {first_path}"
                )


@pytest.mark.parametrize("path", CASES)
def test_declared_tokens_occur_in_the_task_file(path):
    data = load(path)
    sites = data.get("sites")
    if not sites:
        return
    task_file = resolve(path, data["tasks"]["file"])
    text = task_file.read_text()
    for token, service in sites.items():
        # A token the manifest maps but the task file never uses is a claim about
        # nothing, and it hides the opposite mistake: a token that IS used and is
        # not mapped substitutes to nothing and produces a relative URL.
        assert token in text, f"{token} is mapped to {service} but never occurs in {task_file.name}"
    for token in set(re.findall(r"__[A-Z][A-Z_]*__", text)):
        assert token in sites, f"{token} occurs in {task_file.name} but [sites] does not map it"


@pytest.mark.parametrize("path", CASES)
def test_open_decisions_are_answerable(path):
    for decision in load(path).get("open_decisions", []):
        for key in ("id", "question", "options"):
            assert decision.get(key), f"an [[open_decisions]] entry has no {key}"


def test_miniwob_task_list_matches_the_vendored_registry():
    registry = REPO / "tasks/miniwob/upstream/miniwob/registration.py"
    derived = REPO / "tasks/miniwob/core/tasks.jsonl"
    if not (registry.is_file() and derived.is_file()):
        pytest.skip("miniwob suite is not captured")
    registered = set(re.findall(r'id="(miniwob/[^"]+)"', registry.read_text()))
    listed = {json.loads(line)["env_id"] for line in derived.read_text().splitlines()}
    assert registered, "no register() ids found in the vendored registry"
    assert listed == registered, (
        "tasks.jsonl and the vendored registry disagree; regenerate with "
        "tasks/miniwob/derive-tasks.py"
    )
