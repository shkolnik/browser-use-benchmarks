"""Invariants over the captured benchmark task suites.

The suites are vendored upstream bytes plus a first-party manifest describing
them. Both halves rot in different ways: a vendored file can be edited in place
and stop being what its provenance claims, and a manifest can name a service or
a placeholder token that no longer exists. Read the real files rather than
fixtures — the invariant is about what this repo ships.

The manifest's own grammar is enforced by harness/suite.py and tested against
synthetic manifests in test_task_schema.py. What is left here is everything that
needs the rest of the repo: the bytes on disk, the images tree, the deployed
fleet, and whether a suite actually resolves to a runnable task.

Hermetic: this reads only what is committed and starts nothing. The one thing it
deliberately does not check is MiniWoB's per-task episode deadline, which lives
in the pinned HTML archive rather than in git; tasks/miniwob/derive-tasks.py
--check covers that where the archive is present.
"""

import hashlib
import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from harness.fleet import load_fleet
from harness.resolve import _records, _substituted_text, blocking, missing_services, resolve
from harness.suite import CLAIMED_KEYS, CORE_SECTIONS, discover_suites

REPO = Path(__file__).resolve().parent.parent
SUITES = discover_suites(REPO)
IMAGES = {
    f"{p.parent.parent.name}/{p.parent.name}"
    for p in REPO.glob("images/*/*/image.toml")
}
DEPLOYED = set(yaml.safe_load((REPO / "deploy" / "compose.yml").read_text())["services"])

CASES = [pytest.param(s, id=s.id) for s in SUITES]


def test_there_are_suites():
    # A glob that found nothing would make every test below vacuous, and a
    # vacuous suite is indistinguishable from a passing one.
    assert CASES, "no tasks/*/*/suite.toml was discovered"


@pytest.mark.parametrize("suite", CASES)
def test_vendored_files_match_their_recorded_bytes(suite):
    for entry in suite.upstream.files:
        target = suite.dir.parent / entry.path
        assert target.is_file(), (
            f"{entry.path} does not exist under {suite.dir.parent.relative_to(REPO)}; "
            "a vendored path is relative to the benchmark directory")
        raw = target.read_bytes()
        assert len(raw) == entry.bytes, (
            f"{entry.path}: manifest says {entry.bytes} bytes, file is {len(raw)}")
        assert hashlib.sha256(raw).hexdigest() == entry.sha256, (
            f"{entry.path}: sha256 does not match the manifest")


@pytest.mark.parametrize("suite", CASES)
def test_the_task_file_is_where_the_manifest_says(suite):
    target = suite.dir / suite.tasks.file
    assert target.is_file(), (
        f"tasks.file {suite.tasks.file!r} is not in {suite.dir.relative_to(REPO)}; "
        "it is relative to the suite directory, not the benchmark one")


@pytest.mark.parametrize("suite", CASES)
def test_external_pins_point_at_a_file_that_carries_them(suite):
    for pin in suite.external_pins:
        target = REPO / pin.pinned_at
        assert target.is_file(), (
            f"external pin for {pin.what!r} names {pin.pinned_at}, which does not exist")
        if pin.sha256:
            assert pin.sha256 in target.read_text(), (
                f"{pin.pinned_at} no longer carries the sha256 this manifest expects for "
                f"{pin.what!r}. Restating a pin is the thing external_pins exists to "
                "avoid, so a drift here means the one source of truth moved")


@pytest.mark.parametrize("suite", CASES)
def test_required_services_are_named_the_way_images_are(suite):
    for name in suite.requires.services:
        assert name in IMAGES or name in suite.requires.not_built, (
            f"{name!r} is not an images/*/*/ directory and is not declared not_built")


@pytest.mark.parametrize("suite", CASES)
def test_not_built_names_no_image_this_repo_has(suite):
    # The two gaps are different problems with different fixes, and the day one
    # of them closes the manifests still claiming it should fail rather than
    # quietly describe a fleet that has moved on.
    for name in suite.requires.not_built:
        assert name not in IMAGES, (
            f"{name!r} is declared not_built but images/{name}/ exists — it belongs "
            "in not_deployed, or nowhere")


@pytest.mark.parametrize("suite", CASES)
def test_not_deployed_names_a_built_image_the_fleet_omits(suite):
    for name in suite.requires.not_deployed:
        assert name in IMAGES, f"{name!r} is declared not_deployed but has no image"
        service = name.split("/")[-1]
        assert service not in DEPLOYED, (
            f"{name!r} is declared not_deployed but deploy/compose.yml brings up "
            f"{service!r} — the manifest describes a fleet this repo no longer has")


@pytest.mark.parametrize("suite", CASES)
def test_declared_tokens_occur_in_the_task_file(suite):
    sites = suite.addressing.sites
    if not sites:
        return
    task_file = suite.dir / suite.tasks.file
    text = task_file.read_text()
    for token, service in sites.items():
        # A token the manifest maps but the task file never uses is a claim about
        # nothing, and it hides the opposite mistake: a token that IS used and is
        # not mapped substitutes to nothing and produces a relative URL.
        assert token in text, (
            f"{token} is mapped to {service} but never occurs in {task_file.name}")
    for token in set(re.findall(r"__[A-Z][A-Z_]*__", text)):
        assert token in sites, (
            f"{token} occurs in {task_file.name} but [addressing.sites] does not map it")


@pytest.mark.parametrize("suite", CASES)
def test_the_substitution_order_covers_every_mapped_token(suite):
    order = suite.addressing.substitution.get("order")
    if not order:
        return
    for token in suite.addressing.sites:
        assert token in order, (
            f"{token} is mapped but is not in [addressing.substitution].order, so a "
            "reimplementation following that list leaves it unsubstituted")


def test_a_repeated_decision_is_the_same_question_everywhere():
    # One question asked in several places has to be answerable once. Two suites
    # carrying the same id with different wording would take one answer and act
    # on it differently, so a repeated id must repeat the whole question.
    seen = {}
    for suite in SUITES:
        for decision in suite.open_decisions:
            first_suite, first = seen.setdefault(decision.id, (suite, decision))
            assert decision.question == first.question, (
                f"{decision.id!r} asks a different question in {suite.id} than in "
                f"{first_suite.id}")
            assert [(o.id, o.text) for o in decision.options] == \
                   [(o.id, o.text) for o in first.options], (
                f"{decision.id!r} offers different options in {suite.id} than in "
                f"{first_suite.id}")
            assert (decision.scope, decision.kind) == (first.scope, first.kind), (
                f"{decision.id!r} claims a different scope or kind in {suite.id} than in "
                f"{first_suite.id}; one question answered once has one classification")
    assert seen, "no suite records an open decision"


def test_every_fleet_decision_is_answered_or_excused_everywhere():
    # A fleet-scoped question one suite omits is indistinguishable from one
    # nobody considered, which is exactly what [decisions_not_applicable] is for.
    fleet_ids = {
        d.id for s in SUITES for d in s.open_decisions if d.scope == "fleet"
    }
    assert fleet_ids, "no fleet-scoped decision exists; this guard would be vacuous"
    for suite in SUITES:
        carried = {d.id for d in suite.open_decisions}
        for ident in fleet_ids:
            assert ident in carried or ident in suite.decisions_not_applicable, (
                f"{suite.id} neither asks {ident!r} nor says why it does not apply")


def test_decision_kinds_group_the_questions_that_recur():
    by_kind = {}
    for suite in SUITES:
        for decision in suite.open_decisions:
            by_kind.setdefault(decision.kind, set()).add(decision.id)
    # The factoring is the point of the kind field: at least one class of
    # question is asked by more than one suite under different ids, and a kind
    # nothing shares would mean the grouping had stopped doing anything.
    shared = {k: v for k, v in by_kind.items() if len(v) > 1}
    assert shared, (
        "no decision kind covers more than one id, so the kind field groups nothing")


@pytest.mark.parametrize("suite", CASES)
def test_a_suite_resolves_to_a_runnable_task(suite):
    """The acceptance test: the schema carries enough to start a task.

    Reads the manifest, the images tree and deploy/compose.yml and produces the
    instruction, the start URLs and the session file for one real task. A fact
    missing from any of the three shows up here rather than at run time.
    """
    fleet = load_fleet(REPO)
    instance = first_instance(suite)
    case = resolve(suite, instance, fleet, "bench.example")

    assert case.uid == suite.uid(instance)
    assert case.start_urls, "resolved no start URL"
    # A token survives substitution only where the service it names is one the
    # fleet cannot serve, and then the case is not runnable. A leftover token on
    # a runnable case is a URL the browser would resolve against nothing.
    for url in case.start_urls:
        leftover = set(re.findall(r"__[A-Z][A-Z_]*__", url))
        if case.runnable:
            assert url.startswith("http://"), (
                f"{case.uid} is runnable but starts at {url!r}, which is not an absolute "
                "URL — a missing base_url leaves a relative path here")
            assert not leftover, (
                f"{case.uid} is runnable but starts at {url!r}, which still carries a "
                "placeholder token")
        else:
            for token in leftover:
                assert suite.addressing.sites.get(token) in case.unavailable, (
                    f"{case.uid} starts at {url!r} and {token} is unsubstituted for no "
                    "recorded reason")
    if suite.tasks.instruction_source == "literal":
        assert case.instruction, "a literal instruction resolved to nothing"
    else:
        assert case.instruction is None
    # Every service the fleet cannot serve is one the manifest already declared.
    declared = set(suite.requires.not_built) | set(suite.requires.not_deployed)
    assert set(case.unavailable) <= declared, (
        f"{case.uid} cannot reach {sorted(set(case.unavailable) - declared)}, which the "
        "manifest does not declare as a gap")


def first_instance(suite) -> dict:
    """One real instance key: the file's first record, plus stand-ins for the rest."""
    raw = (suite.dir / suite.tasks.file).read_text()
    if suite.tasks.format == "json_array":
        record = json.loads(raw)[0]
    elif suite.tasks.format == "jsonl":
        record = json.loads(raw.splitlines()[0])
    elif suite.tasks.format == "json_array_of_strings":
        record = {"index": 0}
    else:
        raise AssertionError(f"unknown tasks.format {suite.tasks.format!r}")
    instance = {k: record[k] for k in suite.instance.from_file}
    stand_ins = {"seed": 0, "data_mode": "train"}
    for key in suite.instance.chosen_by_harness:
        assert key in stand_ins, (
            f"{suite.id} asks the harness to choose {key!r} and this test has no "
            "stand-in for it")
        instance[key] = stand_ins[key]
    return instance


def test_the_fleet_resolves_every_service_it_deploys():
    fleet = load_fleet(REPO)
    assert len(fleet) == len(DEPLOYED), (
        f"deploy/compose.yml brings up {len(DEPLOYED)} services and the fleet resolved "
        f"{len(fleet)}; a service whose image reference does not name an images/ "
        "directory has no base URL")
    for name, service in fleet.items():
        assert name in IMAGES, f"{name!r} is deployed but is not an images/*/*/ directory"
        assert service.base_url("h").startswith(f"http://h:{service.port}")


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
        "tasks/miniwob/derive-tasks.py")


@pytest.mark.parametrize("suite", CASES)
def test_the_loader_drops_nothing_a_manifest_records(suite):
    """Every key in the file is either modelled by the core or carried in `extra`.

    A manifest states more than a driver consumes, on purpose: what upstream
    does, what it counts, where it is buggy. The loader normalizes what it needs
    and passes the rest through. If a key were silently discarded, the manifest
    would still say it and nothing reading through the loader would ever see it,
    which is the one failure a schema pass can introduce that the file itself
    cannot show.
    """
    raw = tomllib.loads(suite.path.read_text())
    sections = {
        "upstream": suite.upstream, "tasks": suite.tasks,
        "addressing": suite.addressing, "instance": suite.instance,
        "auth": suite.auth, "scoring": suite.scoring, "requires": suite.requires,
    }
    for name, loaded in sections.items():
        kept = CLAIMED_KEYS[name] | set(loaded.extra)
        missing = set(raw.get(name, {})) - kept
        assert not missing, f"[{name}] records {sorted(missing)} and the loader keeps neither"

    repeated = [
        ("reset", suite.resets), ("external_pins", suite.external_pins),
        ("open_decisions", suite.open_decisions),
    ]
    for name, loaded_entries in repeated:
        entries = raw.get(name, [])
        assert len(entries) == len(loaded_entries), f"[[{name}]] lost an entry"
        for i, (entry, obj) in enumerate(zip(entries, loaded_entries)):
            missing = set(entry) - (CLAIMED_KEYS[name] | set(obj.extra))
            assert not missing, f"{name}[{i}] records {sorted(missing)} and the loader keeps neither"

    non_core = set(raw) - set(CORE_SECTIONS)
    assert non_core <= set(suite.extra), (
        f"{sorted(non_core - set(suite.extra))} is a whole section the loader discarded")


@pytest.mark.parametrize("suite", CASES)
def test_the_blocked_count_is_what_blocking_the_tasks_one_by_one_gives(suite):
    """[requires].blocked, recounted from the task file rather than believed.

    The manifest states how many of its tasks the fleet's gaps stop. Deciding
    the same question per task — from the blocked_by task_ids where there are
    any, from the placeholder tokens in the record otherwise — has to reach the
    same number. If it does not, either the count is stale or the rule a driver
    would apply is not the rule the count was made with, and a run would report
    a denominator nobody meant.
    """
    fleet = load_fleet(REPO)
    missing = missing_services(suite, fleet)
    urls = {token: fleet[name].base_url("bench.example")
            for token, name in suite.addressing.sites.items() if name in fleet}
    records = _records(suite, _substituted_text(suite, urls))
    assert len(records) == suite.task_count, (
        f"{suite.tasks.file} holds {len(records)} records and [suite].task_count says "
        f"{suite.task_count}")
    blocked = sum(1 for record in records if blocking(suite, record, missing))
    assert blocked == suite.requires.blocked, (
        f"{blocked} of {suite.task_count} tasks address a service this fleet cannot "
        f"serve, and [requires].blocked says {suite.requires.blocked}")
