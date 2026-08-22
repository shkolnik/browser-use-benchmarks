"""The suite loader's rules, exercised against manifests built to break them.

Synthetic here rather than real, unlike test_task_suites.py: these assert what
the loader REJECTS, and the repo's six manifests are all valid by construction.
An error message nobody has run is prose, so every _die path a manifest can
reach has a test that reaches it.
"""

import tomllib
from pathlib import Path

import pytest

from harness.suite import discover_suites, load_suite

REPO = Path(__file__).resolve().parent.parent

MINIMAL = """
[suite]
name = "demo"
benchmark = "bench"
task_count = 10
description = "a suite"

[upstream]
repo = "https://example.invalid/x"
commit = "0123456789abcdef0123456789abcdef01234567"
license = "MIT"

[[upstream.files]]
path = "demo/tasks.json"
upstream_path = "config/tasks.json"
sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
bytes = 1

[tasks]
file = "tasks.json"
format = "json_array"
instruction_source = "literal"
instruction_field = "intent"

[addressing]
kind = "start_url_field"
field = "start_url"

[instance]
key = ["task_id"]
from_file = ["task_id"]
chosen_by_harness = []

[auth]
kind = "none"

[[reset]]
scope = "task"
kind = "none"
contaminates = false

[scoring]
evaluator_kinds = ["string_match"]
reward_range = [0.0, 1.0]
success = "score == 1.0"
offline_scorable = 10
needs_live_page = 0
needs_judge = 0
aggregate = "mean"

[requires]
services = ["bench/demo"]
runnable = 10
blocked = 0
"""


def write(tmp_path: Path, body: str = MINIMAL, *, slot: int = 0) -> Path:
    """Write a manifest under its own root, so one test can write two."""
    target = tmp_path / str(slot) / "tasks" / "bench" / "demo"
    target.mkdir(parents=True)
    path = target / "suite.toml"
    path.write_text(body)
    return path


def mutate(body: str, old: str, new: str) -> str:
    assert old in body, f"fixture no longer contains {old!r}"
    return body.replace(old, new)


def test_the_minimal_manifest_loads(tmp_path):
    # Vacuity guard for the whole file: if the fixture stopped being valid, every
    # rejection test below would pass for the wrong reason.
    suite = load_suite(write(tmp_path))
    assert suite.id == "bench/demo"
    assert suite.task_count == 10


def test_the_directory_is_the_name(tmp_path):
    body = mutate(MINIMAL, 'name = "demo"', 'name = "other"')
    with pytest.raises(SystemExit, match="discovery is by glob"):
        load_suite(write(tmp_path, body))


def test_an_empty_suite_is_rejected(tmp_path):
    body = mutate(MINIMAL, "task_count = 10", "task_count = 0")
    with pytest.raises(SystemExit, match="empty suite passes every test"):
        load_suite(write(tmp_path, body))


def test_a_branch_is_not_a_pin(tmp_path):
    body = mutate(MINIMAL, '"0123456789abcdef0123456789abcdef01234567"', '"main"')
    with pytest.raises(SystemExit, match="a moving ref cannot say which bytes"):
        load_suite(write(tmp_path, body))


def test_a_vendored_path_may_not_climb(tmp_path):
    body = mutate(MINIMAL, 'path = "demo/tasks.json"', 'path = "../demo/tasks.json"')
    with pytest.raises(SystemExit, match="relative to the benchmark directory"):
        load_suite(write(tmp_path, body))


def test_a_literal_instruction_names_its_field(tmp_path):
    body = mutate(MINIMAL, 'instruction_field = "intent"\n', "")
    with pytest.raises(SystemExit, match="instruction_field is required"):
        load_suite(write(tmp_path, body))


def test_a_rendered_instruction_names_no_field(tmp_path):
    body = mutate(MINIMAL, 'instruction_source = "literal"',
                  'instruction_source = "page_rendered"')
    with pytest.raises(SystemExit, match="the text is not in the file"):
        load_suite(write(tmp_path, body))


def test_instruction_recoverability_follows_the_source(tmp_path):
    assert load_suite(write(tmp_path)).tasks.instruction_is_recoverable
    body = mutate(MINIMAL, 'instruction_source = "literal"\ninstruction_field = "intent"',
                  'instruction_source = "server_rendered"')
    other = load_suite(write(tmp_path, body, slot=1))
    assert not other.tasks.instruction_is_recoverable


def test_a_path_template_must_interpolate(tmp_path):
    body = mutate(MINIMAL, 'kind = "start_url_field"\nfield = "start_url"',
                  'kind = "path_template"\ntemplate = "/fixed"')
    with pytest.raises(SystemExit, match="interpolates nothing"):
        load_suite(write(tmp_path, body))


def test_mapped_tokens_need_a_substitution_rule(tmp_path):
    body = MINIMAL + '\n[addressing.sites]\n"__X__" = "bench/demo"\n'
    with pytest.raises(SystemExit, match="the order is load-bearing"):
        load_suite(write(tmp_path, body))


def test_the_instance_key_is_wholly_accounted_for(tmp_path):
    body = mutate(MINIMAL, 'key = ["task_id"]', 'key = ["task_id", "seed"]')
    with pytest.raises(SystemExit, match="comes from exactly one of the two"):
        load_suite(write(tmp_path, body))


def test_a_compound_uid_names_every_field(tmp_path):
    body = mutate(MINIMAL,
                  'key = ["task_id"]\nfrom_file = ["task_id"]\nchosen_by_harness = []',
                  'key = ["env", "seed"]\nfrom_file = ["env"]\nchosen_by_harness = ["seed"]')
    suite = load_suite(write(tmp_path, body))
    assert suite.uid({"env": "click", "seed": 7}) == "bench/demo#env=click,seed=7"


def test_a_single_field_uid_is_bare(tmp_path):
    assert load_suite(write(tmp_path)).uid({"task_id": 42}) == "bench/demo#42"


def test_a_uid_refuses_a_partial_instance(tmp_path):
    with pytest.raises(KeyError, match="keyed by"):
        load_suite(write(tmp_path)).uid({})


def test_storage_state_auth_says_what_decides(tmp_path):
    body = mutate(MINIMAL, '[auth]\nkind = "none"', '[auth]\nkind = "storage_state"')
    with pytest.raises(SystemExit, match="decided_by is required"):
        load_suite(write(tmp_path, body))


def test_reset_must_be_an_array(tmp_path):
    body = mutate(MINIMAL, "[[reset]]", "[reset]")
    with pytest.raises(SystemExit, match="reset has a scope"):
        load_suite(write(tmp_path, body))


def test_reset_declares_contamination(tmp_path):
    body = mutate(MINIMAL, "contaminates = false\n", "")
    with pytest.raises(SystemExit, match="separates WebArena's 'none'"):
        load_suite(write(tmp_path, body))


def test_a_recreated_container_cannot_contaminate(tmp_path):
    body = mutate(MINIMAL, 'kind = "none"\ncontaminates = false',
                  'kind = "container_recreate"\ncontaminates = true')
    with pytest.raises(SystemExit, match="definition of not carrying state forward"):
        load_suite(write(tmp_path, body))


def test_something_must_say_what_happens_between_tasks(tmp_path):
    body = mutate(MINIMAL, 'scope = "task"', 'scope = "suite"')
    with pytest.raises(SystemExit, match="no per-task or per-episode entry"):
        load_suite(write(tmp_path, body))


def test_one_scope_has_one_entry(tmp_path):
    body = MINIMAL + '\n[[reset]]\nscope = "task"\nkind = "none"\ncontaminates = false\n'
    with pytest.raises(SystemExit, match="one scope has one answer"):
        load_suite(write(tmp_path, body))


def test_the_scoring_partition_covers_the_suite(tmp_path):
    body = mutate(MINIMAL, "offline_scorable = 10", "offline_scorable = 9")
    with pytest.raises(SystemExit, match="a task nothing plans to score"):
        load_suite(write(tmp_path, body))


def test_runnable_and_blocked_account_for_every_task(tmp_path):
    body = mutate(MINIMAL, "runnable = 10", "runnable = 9")
    with pytest.raises(SystemExit, match="does not account for"):
        load_suite(write(tmp_path, body))


def test_a_gap_is_one_kind_of_gap(tmp_path):
    body = mutate(MINIMAL, 'services = ["bench/demo"]',
                  'services = ["bench/demo"]\nnot_built = ["bench/demo"]\n'
                  'not_deployed = ["bench/demo"]')
    with pytest.raises(SystemExit, match="different problems with different fixes"):
        load_suite(write(tmp_path, body))


BLOCKED = mutate(
    mutate(MINIMAL, "runnable = 10\nblocked = 0",
           'not_built = ["bench/missing"]\nrunnable = 6\nblocked = 4\n\n'
           "[[requires.blocked_by]]\nservice = \"bench/missing\"\ntasks = 4"),
    'services = ["bench/demo"]', 'services = ["bench/demo", "bench/missing"]')


def test_a_blocker_and_a_blocked_task_imply_each_other(tmp_path):
    assert load_suite(write(tmp_path, BLOCKED)).requires.blocked == 4
    body = mutate(BLOCKED, "\n[[requires.blocked_by]]\nservice = \"bench/missing\"\ntasks = 4", "")
    with pytest.raises(SystemExit, match="a blocked task has a reason"):
        load_suite(write(tmp_path, body, slot=1))


def test_a_blocker_must_be_a_declared_gap(tmp_path):
    body = mutate(BLOCKED, 'service = "bench/missing"', 'service = "bench/demo"')
    with pytest.raises(SystemExit, match="nothing says why it is missing"):
        load_suite(write(tmp_path, body))


def test_blocked_by_counts_bound_the_total(tmp_path):
    body = mutate(BLOCKED, "tasks = 4", "tasks = 2")
    with pytest.raises(SystemExit, match="outside the"):
        load_suite(write(tmp_path, body))


def test_listed_ids_must_match_the_count(tmp_path):
    body = mutate(BLOCKED, "tasks = 4", "tasks = 4\ntask_ids = [1, 2]")
    with pytest.raises(SystemExit, match="lists 2 task_ids but claims 4"):
        load_suite(write(tmp_path, body))


DECISION = MINIMAL + """
[[open_decisions]]
id = "bench-judge"
scope = "suite"
kind = "judge_model"
question = "which model?"
affects = "some"

[[open_decisions.options]]
id = "upstream"
text = "the model upstream used"

[[open_decisions.options]]
id = "local"
text = "something local"
"""


def test_a_decision_offers_option_ids(tmp_path):
    suite = load_suite(write(tmp_path, DECISION))
    assert suite.open_decisions[0].option_ids == ("upstream", "local")
    assert suite.decisions_to_answer() == ("bench-judge",)


def test_free_text_options_are_rejected(tmp_path):
    body = mutate(DECISION,
                  '[[open_decisions.options]]\nid = "upstream"\ntext = "the model upstream used"\n\n'
                  '[[open_decisions.options]]\nid = "local"\ntext = "something local"',
                  'options = ["the model upstream used", "something local"]')
    with pytest.raises(SystemExit, match="referenced by nothing"):
        load_suite(write(tmp_path, body))


def test_one_option_is_not_a_decision(tmp_path):
    body = mutate(DECISION, '\n[[open_decisions.options]]\nid = "local"\ntext = "something local"\n', "")
    with pytest.raises(SystemExit, match="a decision with one option"):
        load_suite(write(tmp_path, body))


def test_a_decision_is_namespaced(tmp_path):
    body = mutate(DECISION, 'id = "bench-judge"', 'id = "judge"')
    with pytest.raises(SystemExit, match="namespaced to neither"):
        load_suite(write(tmp_path, body))


def test_a_fleet_decision_is_findable_by_its_id(tmp_path):
    body = mutate(DECISION, 'scope = "suite"', 'scope = "fleet"')
    with pytest.raises(SystemExit, match="one answer could not be found by its id"):
        load_suite(write(tmp_path, body))


def test_affects_tasks_cannot_exceed_the_suite(tmp_path):
    body = mutate(DECISION, 'affects = "some"', 'affects = "some"\naffects_tasks = 11')
    with pytest.raises(SystemExit, match="exceeds the suite's 10 tasks"):
        load_suite(write(tmp_path, body))


def test_a_decision_is_open_or_not_applicable_but_not_both(tmp_path):
    body = DECISION + '\n[decisions_not_applicable]\nbench-judge = "no reason"\n'
    with pytest.raises(SystemExit, match="both an open decision here and declared not"):
        load_suite(write(tmp_path, body))


def test_a_not_applicable_decision_gives_a_reason(tmp_path):
    body = MINIMAL + '\n[decisions_not_applicable]\nfleet-ties = ""\n'
    with pytest.raises(SystemExit, match="an empty reason says nothing"):
        load_suite(write(tmp_path, body))


def test_an_external_pin_is_repo_relative(tmp_path):
    body = MINIMAL + ('\n[[external_pins]]\nwhat = "corpus"\n'
                      'pinned_at = "../images/x/image.toml"\n')
    with pytest.raises(SystemExit, match="must be repo-root-relative"):
        load_suite(write(tmp_path, body))


def test_unclaimed_sections_survive_loading(tmp_path):
    body = MINIMAL + '\n[viewport]\nwindow = [500, 320]\n'
    suite = load_suite(write(tmp_path, body))
    assert suite.extra["viewport"]["window"] == [500, 320], (
        "a manifest may record anything it needs; loading it must not be the thing "
        "that loses it")


def test_discovery_finds_nothing_in_an_empty_tree(tmp_path):
    assert discover_suites(tmp_path) == []


def test_every_real_manifest_loads():
    suites = discover_suites(REPO)
    assert suites, "no tasks/*/*/suite.toml was discovered"
    assert {s.id for s in suites} == {
        p.parent.parent.name + "/" + p.parent.name
        for p in REPO.glob("tasks/*/*/suite.toml")
    }
