"""The results records, and the two places they meet the suite manifests.

Validation runs at write time, so these tests are about what is REFUSED. A
malformed record that reaches disk is a record somebody reads back and believes,
and the two cross-schema rules — a run answers its suites' open decisions, a
result carries an instruction that exists nowhere else — are the ones that stop
a file being a number without a meaning.
"""

import json
from pathlib import Path

import pytest

from harness.results import (SCHEMA, EvaluatorResult, InvalidRecord, ResultsWriter,
                             RunRecord, SummaryRecord, TaskResult, now, read,
                             validate_run, validate_run_against_suites, validate_task,
                             validate_task_against_suite)
from harness.suite import discover_suites, load_suite

REPO = Path(__file__).resolve().parent.parent
DIGEST = "sha256:" + "0" * 64
T0 = "2026-08-22T05:00:00Z"
T1 = "2026-08-22T05:00:12Z"


def run_record(**over) -> RunRecord:
    base = dict(
        run_id="r1",
        started_at=T0,
        repo_commit="c8bcac3",
        harness={"name": "harness", "version": "0"},
        agent={"name": "a", "model": "m", "version": "0"},
        suites=("bench/demo",),
        fleet={"bench/demo": {"image": "ghcr.io/x/bench-demo:latest",
                              "digest": DIGEST, "base_url": "http://h:1"}},
        decisions={},
    )
    base.update(over)
    return RunRecord(**base)


def task_record(**over) -> TaskResult:
    base = dict(
        run_id="r1", uid="bench/demo#1", suite="bench/demo", instance={"task_id": 1},
        status="scored", started_at=T0, ended_at=T1, duration_s=12.0,
        score=1.0, raw_score=1.0, score_kind="binary", success=True,
    )
    base.update(over)
    return TaskResult(**base)


def test_a_well_formed_pair_validates():
    # Vacuity guard: if the fixtures stopped validating, every rejection below
    # would pass for the wrong reason.
    validate_run(run_record())
    validate_task(task_record())


def test_a_tag_is_not_a_build():
    bad = run_record(fleet={"bench/demo": {"image": "ghcr.io/x/bench-demo:latest",
                                           "digest": "latest", "base_url": "http://h:1"}})
    with pytest.raises(InvalidRecord, match="tied search ranks permute"):
        validate_run(bad)


def test_a_run_names_the_revision_that_defined_the_suites():
    with pytest.raises(InvalidRecord, match="must name their revision"):
        validate_run(run_record(repo_commit=""))


def test_a_scored_task_carries_a_score():
    with pytest.raises(InvalidRecord, match=r"needs a score in \[0, 1\]"):
        validate_task(task_record(score=None))


def test_a_score_outside_the_unit_interval_is_refused():
    with pytest.raises(InvalidRecord, match=r"needs a score in \[0, 1\]"):
        validate_task(task_record(score=1.5))


def test_a_scored_task_names_the_metric():
    with pytest.raises(InvalidRecord, match="does not name the metric"):
        validate_task(task_record(score_kind=""))


def test_a_task_that_did_not_score_has_no_score():
    # The whole denominator argument in one assertion: writing 0 here would
    # silently pick one of the two metrics the judge decision leaves open.
    with pytest.raises(InvalidRecord, match="picks the denominator silently"):
        validate_task(task_record(status="error", error={"kind": "judge_unparseable"}))


def test_a_blocked_task_names_what_blocks_it():
    with pytest.raises(InvalidRecord, match="must name what blocks it"):
        validate_task(task_record(status="blocked", score=None, success=None))
    validate_task(task_record(status="blocked", score=None, success=None,
                              blocked_by=("webarena/map-frontend",)))


def test_an_errored_task_says_how():
    with pytest.raises(InvalidRecord, match="carry error.kind"):
        validate_task(task_record(status="error", score=None, success=None, error={}))


def test_an_unknown_status_is_refused():
    with pytest.raises(InvalidRecord, match="task.status must be one of"):
        validate_task(task_record(status="passed"))


def test_a_task_record_is_named_by_more_than_an_integer():
    with pytest.raises(InvalidRecord, match="names three different tasks"):
        validate_task(task_record(instance={}))


def test_timestamps_are_iso_utc():
    with pytest.raises(InvalidRecord, match="ISO-8601"):
        validate_task(task_record(started_at="yesterday"))


# ---- the two cross-schema rules -------------------------------------------

SUITES = discover_suites(REPO)


def test_the_repo_has_suites_to_check_against():
    assert SUITES, "no tasks/*/*/suite.toml was discovered"


def test_a_run_answers_every_open_decision_its_suites_declare():
    suite = next(s for s in SUITES if s.open_decisions)
    run = run_record(suites=(suite.id,), fleet={
        name: {"image": f"ghcr.io/x/{name.replace('/', '-')}:latest",
               "digest": DIGEST, "base_url": "http://h:1"}
        for name in suite.requires.services
        if name not in set(suite.requires.not_built) | set(suite.requires.not_deployed)
    })
    with pytest.raises(InvalidRecord, match="does not answer it"):
        validate_run_against_suites(run, SUITES)

    answered = run_record(
        suites=run.suites, fleet=run.fleet,
        decisions={d.id: d.options[0].id for d in suite.open_decisions})
    validate_run_against_suites(answered, SUITES)


def test_an_answer_must_be_one_of_the_declared_options():
    suite = next(s for s in SUITES if s.open_decisions)
    run = run_record(
        suites=(suite.id,),
        fleet={name: {"image": "ghcr.io/x/y:latest", "digest": DIGEST,
                      "base_url": "http://h:1"}
               for name in suite.requires.services
               if name not in set(suite.requires.not_built) | set(suite.requires.not_deployed)},
        decisions={d.id: "whatever-we-felt-like" for d in suite.open_decisions})
    with pytest.raises(InvalidRecord, match="which is not one of"):
        validate_run_against_suites(run, SUITES)


def test_a_run_says_where_every_reachable_service_was():
    suite = next(s for s in SUITES if s.open_decisions)
    run = run_record(suites=(suite.id,), fleet={},
                     decisions={d.id: d.options[0].id for d in suite.open_decisions})
    with pytest.raises(InvalidRecord, match="does not say where it was"):
        validate_run_against_suites(run, SUITES)


def test_a_uid_must_match_the_suites_instance_key():
    suite = SUITES[0]
    instance = {k: 1 for k in suite.instance.key}
    # An instruction on every record, whatever the suite: one that renders its
    # text at run time requires the record to carry it, and that is a different
    # rule from the one under test here.
    def record(uid):
        return task_record(suite=suite.id, instance=instance, uid=uid,
                           instruction="do the thing")
    validate_task_against_suite(record(suite.uid(instance)), suite)
    with pytest.raises(InvalidRecord, match=r"\[instance\].key makes it"):
        validate_task_against_suite(record("whatever"), suite)


def test_a_rendered_instruction_must_be_recorded():
    rendered = [s for s in SUITES if not s.tasks.instruction_is_recoverable]
    assert rendered, (
        "no captured suite renders its instruction at run time; this rule guards "
        "WebShop and MiniWoB and would be vacuous without them")
    for suite in rendered:
        instance = {k: 1 for k in suite.instance.key}
        base = dict(suite=suite.id, instance=instance, uid=suite.uid(instance),
                    raw_score=None)
        with pytest.raises(InvalidRecord, match="exists nowhere but in this record"):
            validate_task_against_suite(task_record(**base), suite)
        validate_task_against_suite(
            task_record(instruction="what the agent saw", **base), suite)


def test_a_literal_instruction_need_not_be_repeated():
    for suite in (s for s in SUITES if s.tasks.instruction_is_recoverable):
        instance = {k: 1 for k in suite.instance.key}
        validate_task_against_suite(
            task_record(suite=suite.id, instance=instance, uid=suite.uid(instance),
                        raw_score=None), suite)


def test_a_native_score_must_lie_in_the_suites_range():
    suite = next(s for s in SUITES if s.scoring.reward_range == (0.0, 1.0))
    instance = {k: 1 for k in suite.instance.key}
    with pytest.raises(InvalidRecord, match="outside"):
        validate_task_against_suite(
            task_record(suite=suite.id, instance=instance, uid=suite.uid(instance),
                        instruction="x", raw_score=-1.0), suite)


# ---- the file ---------------------------------------------------------------

def test_records_round_trip_through_jsonl(tmp_path):
    path = tmp_path / "results.jsonl"
    with ResultsWriter(path) as out:
        out.run(run_record())
        out.task(task_record(evaluators=(EvaluatorResult("string_match", 1.0, {}),)))
        out.summary(SummaryRecord(run_id="r1", ended_at=T1,
                                  counts={"scored": 1},
                                  aggregate={"n": 1, "mean_score": 1.0}))
    records = read(path)
    assert [r["record"] for r in records] == ["run", "task", "summary"]
    assert all(r["schema"] == SCHEMA for r in records)
    assert records[1]["evaluators"][0]["kind"] == "string_match"


def test_a_task_from_another_run_is_refused(tmp_path):
    with ResultsWriter(tmp_path / "r.jsonl") as out:
        out.run(run_record())
        with pytest.raises(InvalidRecord, match="does not match the file's run"):
            out.task(task_record(run_id="r2"))


def test_a_summary_must_account_for_the_file(tmp_path):
    with ResultsWriter(tmp_path / "r.jsonl") as out:
        out.run(run_record())
        out.task(task_record())
        with pytest.raises(InvalidRecord, match="sums to 2 but the file holds 1"):
            out.summary(SummaryRecord(run_id="r1", ended_at=T1,
                                      counts={"scored": 1, "error": 1}, aggregate={}))


def test_a_half_written_last_line_is_survivable(tmp_path):
    # The writer flushes per record, so a partial line means the run died
    # mid-write — which is the case the streaming layout exists to survive.
    path = tmp_path / "r.jsonl"
    with ResultsWriter(path) as out:
        out.run(run_record())
        out.task(task_record())
    path.write_text(path.read_text() + '{"schema": "browser-use')
    assert [r["record"] for r in read(path)] == ["run", "task"]


def test_a_foreign_schema_is_not_silently_read(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps({"schema": "someone-elses@3", "record": "run"}) + "\n")
    with pytest.raises(InvalidRecord, match="declares schema"):
        read(path)


def test_now_is_iso_utc():
    validate_run(run_record(started_at=now()))
