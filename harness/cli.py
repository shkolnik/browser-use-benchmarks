"""`python3 -m harness` — read the captured suites through the schema.

Deliberately not `bin/tasks`: build.yml filters pushes on `images/**`,
`builder/**` and `bin/**`, so an entry point there would rebuild the whole fleet
every time this changed.

Nothing here runs a task. `resolve` prints what a driver would be handed, which
is how the schema proves it carries enough to run one.
"""

import argparse
import json
import sys
from pathlib import Path

from harness.results import (InvalidRecord, read, validate_run_against_suites,
                             validate_task_against_suite)
from harness.suite import discover_suites, load_suite

REPO = Path(__file__).resolve().parent.parent


def _suites():
    suites = discover_suites(REPO)
    if not suites:
        raise SystemExit("error: no tasks/*/*/suite.toml was discovered")
    return suites


def _pick(name: str):
    for suite in _suites():
        if suite.id == name:
            return suite
    raise SystemExit(
        f"error: no suite named {name!r}. Captured: "
        + ", ".join(s.id for s in _suites()))


def _coerce(value: str):
    """Match the JSON types in the task files: ids are integers, names are not."""
    return int(value) if value.lstrip("-").isdigit() else value


def cmd_list(args):
    total = 0
    print(f"{'suite':22s} {'tasks':>7s} {'runnable':>9s} {'blocked':>8s}  instruction")
    for suite in _suites():
        total += suite.task_count
        print(f"{suite.id:22s} {suite.task_count:7d} {suite.requires.runnable:9d} "
              f"{suite.requires.blocked:8d}  {suite.tasks.instruction_source}")
    print(f"{'':22s} {total:7d}  captured across {len(_suites())} suites")


def cmd_show(args):
    suite = _pick(args.suite)
    print(f"{suite.id} — {suite.description}")
    print(f"  upstream    {suite.upstream.repo} @ {suite.upstream.commit[:12]} "
          f"({suite.upstream.license}), {len(suite.upstream.files)} vendored files")
    print(f"  tasks       {suite.task_count} in {suite.tasks.file} ({suite.tasks.format}), "
          f"instruction {suite.tasks.instruction_source}")
    print(f"  instance    {list(suite.instance.key)}"
          + (f", harness chooses {list(suite.instance.chosen_by_harness)}"
             if suite.instance.chosen_by_harness else ""))
    print(f"  addressing  {suite.addressing.kind}"
          + (f" via {suite.addressing.field_name!r}" if suite.addressing.field_name else "")
          + (f" template {suite.addressing.template!r}" if suite.addressing.template else ""))
    for token, service in sorted(suite.addressing.sites.items()):
        print(f"                {token} -> {service}")
    print(f"  auth        {suite.auth.kind}"
          + (f", {suite.auth.tasks_with_session} with a session"
             if suite.auth.kind == "storage_state" else "")
          + (f", accounts: {', '.join(a.name for a in suite.accounts)}"
             if suite.accounts else ""))
    for reset in suite.resets:
        cost = "unmeasured" if reset.cost_s is None else f"{reset.cost_s:g}s"
        print(f"  reset       per {reset.scope}: {reset.kind} ({cost})"
              + (", state carries forward" if reset.contaminates else ""))
    print(f"  scoring     {', '.join(suite.scoring.evaluator_kinds)}")
    print(f"              reward {list(suite.scoring.reward_range)}, "
          f"success {suite.scoring.success!r}, aggregate {suite.scoring.aggregate}")
    print(f"              {suite.scoring.offline_scorable} offline-scorable, "
          f"{suite.scoring.needs_live_page} need the live page, "
          f"{suite.scoring.needs_judge} need a judge")
    print(f"  requires    {', '.join(suite.requires.services)}")
    for entry in suite.requires.blocked_by:
        print(f"              blocked: {entry.tasks} tasks on {entry.service}")
    for pin in suite.external_pins:
        print(f"  pinned at   {pin.pinned_at} ({pin.what})")
    print(f"  decisions   {len(suite.open_decisions)} open")


def cmd_decisions(args):
    suites = [_pick(args.suite)] if args.suite else _suites()
    by_kind = {}
    for suite in suites:
        for decision in suite.open_decisions:
            by_kind.setdefault((decision.kind, decision.id), []).append(suite.id)
    for (kind, ident), asked_by in sorted(by_kind.items()):
        decision = next(d for s in suites for d in s.open_decisions if d.id == ident)
        print(f"[{kind}] {ident}  ({decision.scope}-scoped, asked by "
              f"{len(asked_by)}: {', '.join(asked_by)})")
        print(f"  {decision.question}")
        for option in decision.options:
            print(f"    {option.id:28s} {option.text[:96]}")
    print(f"\n{len(by_kind)} open decisions across {len(suites)} suite(s); "
          f"{len({k for k, _ in by_kind})} distinct kinds")


def cmd_resolve(args):
    from harness.fleet import load_fleet
    from harness.resolve import resolve

    suite = _pick(args.suite)
    if len(args.instance) != len(suite.instance.key):
        raise SystemExit(
            f"error: {suite.id} is keyed by {list(suite.instance.key)}; "
            f"got {len(args.instance)} value(s)")
    instance = dict(zip(suite.instance.key, (_coerce(v) for v in args.instance)))
    case = resolve(suite, instance, load_fleet(REPO), args.host)

    print(f"{case.uid}")
    shown = case.instruction or f"<{case.instruction_source} — read it at run time>"
    print(f"  instruction   {shown}")
    for url in case.start_urls:
        print(f"  start         {url}")
    if case.storage_state:
        print(f"  session       {case.storage_state}")
    print(f"  requires      {', '.join(case.requires)}")
    for service, why in sorted(case.unavailable.items()):
        print(f"  BLOCKED       {service}: {why}")
    for reset in case.resets:
        print(f"  reset         per {reset.scope}: {reset.kind}")
    print(f"  answer first  {', '.join(case.decisions_to_answer) or 'nothing'}")
    if args.json:
        print(json.dumps(case.record, indent=2, sort_keys=True))


def cmd_validate(args):
    suites = _suites()
    print(f"{len(suites)} suite(s) load and satisfy the schema.")
    if not args.results:
        return
    records = read(Path(args.results))
    by_id = {s.id: s for s in suites}
    runs = [r for r in records if r["record"] == "run"]
    if len(runs) != 1:
        raise SystemExit(f"error: {args.results} holds {len(runs)} run records, not 1")
    run = runs[0]
    from harness.results import RunRecord, TaskResult
    validate_run_against_suites(
        RunRecord(**{k: v for k, v in run.items() if k not in ("schema", "record")}),
        suites)
    tasks = [r for r in records if r["record"] == "task"]
    for record in tasks:
        payload = {k: v for k, v in record.items() if k not in ("schema", "record")}
        payload["evaluators"] = tuple(payload.get("evaluators", ()))
        payload["blocked_by"] = tuple(payload.get("blocked_by", ()))
        task = TaskResult(**payload)
        suite = by_id.get(task.suite)
        if suite is None:
            raise InvalidRecord(f"{task.uid} names suite {task.suite!r}, which is not captured")
        validate_task_against_suite(task, suite)
    print(f"{args.results}: 1 run, {len(tasks)} task record(s), all valid.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="every captured suite").set_defaults(fn=cmd_list)

    show = sub.add_parser("show", help="one suite's normalized core")
    show.add_argument("suite")
    show.set_defaults(fn=cmd_show)

    dec = sub.add_parser("decisions", help="the open decisions a run must answer")
    dec.add_argument("--suite", default="")
    dec.set_defaults(fn=cmd_decisions)

    res = sub.add_parser("resolve", help="one runnable task case")
    res.add_argument("suite")
    res.add_argument("instance", nargs="+")
    res.add_argument("--host", default="127.0.0.1",
                     help="the address clients type; BENCH_HOST in deploy/compose.yml")
    res.add_argument("--json", action="store_true", help="also print the upstream record")
    res.set_defaults(fn=cmd_resolve)

    val = sub.add_parser("validate", help="the suites, and optionally a results file")
    val.add_argument("--results", default="")
    val.set_defaults(fn=cmd_validate)

    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except InvalidRecord as exc:
        raise SystemExit(f"error: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
