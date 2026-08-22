"""What a benchmark run emits, and what makes two runs comparable.

One results.jsonl per run. The first line is the `run` record, every line after
it is a `task` record appended as that task finishes, and the last is an
optional `summary`. Streaming rather than assembling a document at the end is
what makes a run that dies halfway leave a file readable to the point it died.

The design question the schema answers is not "what happened" but "what has to
be recorded for two runs to be comparable". Three fields carry that weight and
none of them is the score:

  run.fleet     — image DIGESTS, not tags. Search results with tied relevance
                  permute across a rebuild, so two runs against :latest a month
                  apart are two different benchmarks and only the digest says so.
  run.decisions — which option was taken for every open decision the suites
                  declare. Several of the 37 change the metric rather than the
                  implementation: which model judges 118 WebArena tasks, whether
                  MiniWoB's deadline was raised, whether an unparseable judge
                  reply omits a task or scores it 0.
  task.status   — a task that errored is not a task that scored 0. Upstream
                  WebArena omits an unparseable reply from the scored set, which
                  raises the reported rate by shrinking the denominator; scoring
                  it 0 is a different metric. Recording the distinction is what
                  lets a reader compute either one from the same file.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "browser-use-benchmarks/results@1"

RECORD_KINDS = frozenset({"run", "task", "summary"})

# scored — the task ran and an evaluator produced a number.
# blocked — the fleet cannot serve it; 1,015 of the 13,937 captured tasks are in
#           this state today, so it is a first-class outcome and not an error.
# error   — something raised: a judge answered off-script, the Magento token
#           endpoint returned non-200, a browser died. Distinct from a 0.
# skipped — deliberately not attempted, e.g. outside the run's selection.
STATUSES = frozenset({"scored", "blocked", "error", "skipped"})

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


class InvalidRecord(ValueError):
    """A record that would be misread later. Raised at write time, not read time."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ServiceRef:
    """Where one service was, and exactly which bytes were serving.

    `image` alone is not enough. Every deploy entry names `:latest`, which is a
    moving target by design, so the digest is the only field that identifies the
    build a score was measured against.
    """
    image: str
    digest: str
    base_url: str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    started_at: str
    # This repo's commit. The manifests are what define the suites, so a result
    # that does not name their revision cannot say what it scored.
    repo_commit: str
    harness: dict            # {"name": …, "version": …}
    agent: dict              # {"name": …, "model": …, "version": …}
    suites: tuple[str, ...]  # "<benchmark>/<suite>"
    fleet: dict              # service name -> ServiceRef (as a dict)
    decisions: dict          # open-decision id -> the option id taken
    # The agent's budget. Deliberately here and not in a manifest: a manifest
    # states what upstream does, and a step limit is this repo's choice. The one
    # budget that IS upstream's — MiniWoB's episode deadline — stays in the
    # manifest, and whether a run changed it is an open decision.
    budget: dict = field(default_factory=dict)
    # The versions that move a score without being part of the agent: the judge
    # model and its sampling parameters, spaCy's model, thefuzz's backend, the
    # nltk punkt data. Each is documented in a suite manifest as changing
    # results, and none of them is visible in the score.
    scorer: dict = field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class EvaluatorResult:
    kind: str
    score: float
    # What upstream computes and throws away: the selected element, the
    # required contents, the resolved func: expression, the judge's raw reply.
    # A 0.0 on a multi-evaluator task is unattributable without it.
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TaskResult:
    run_id: str
    uid: str                 # Suite.uid(instance) — the suite prefix is load-bearing
    suite: str
    instance: dict           # the suite's [instance].key fields and their values
    status: str
    started_at: str
    ended_at: str
    duration_s: float
    # Where in the run this ran. Not cosmetic: WebArena performs no per-task
    # reset, so a task that places an order changes what every later task sees,
    # and two runs in different orders are not comparable at identical
    # everything else.
    position: int = 0
    attempt: int = 1
    # The text the agent was given. Mandatory where the suite's
    # instruction_source is not "literal", because for those it exists nowhere
    # else: WebShop redraws its price clause per boot and MiniWoB regenerates
    # its utterance per episode.
    instruction: str | None = None
    answer: str | None = None
    # Normalized to [0, 1] and comparable within a suite. None unless the task
    # scored: a blocked or errored task has no score, and writing 0 would pick
    # the denominator silently.
    score: float | None = None
    # Whatever the benchmark natively produced. MiniWoB's env_reward runs
    # [-1, 1] and is time-decayed — a correct answer at t=9s on a 10s task is
    # 0.1 — so a file holding only the native number makes a working agent look
    # broken and cannot be compared with a WebArena 1.0.
    raw_score: float | None = None
    # Which metric produced `score`. MiniWoB has four reward processors and the
    # choice is itself an open decision, so the number does not name itself.
    score_kind: str = ""
    success: bool | None = None
    evaluators: tuple[EvaluatorResult, ...] = ()
    blocked_by: tuple[str, ...] = ()
    error: dict | None = None       # {"kind": …, "message": …}
    # The benchmark-native fields, verbatim. The typed fields above are the ones
    # that are comparable across benchmarks; this is everything that is not, kept
    # so a rescore is possible — WebShop's w_price and r_att, MiniWoB's done and
    # reason, WebArena's judge replies and the substituted config file's hash.
    native: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SummaryRecord:
    run_id: str
    ended_at: str
    counts: dict             # status -> how many task records carry it
    aggregate: dict          # e.g. {"n": 812, "mean_score": 0.14, "success_rate": 0.14}


def _need(cond, msg):
    if not cond:
        raise InvalidRecord(msg)


def _check_time(value, where):
    _need(isinstance(value, str) and _ISO.fullmatch(value),
          f"{where} must be an ISO-8601 UTC timestamp, got {value!r}")


def validate_run(run: RunRecord):
    _need(run.run_id, "run.run_id is empty; nothing would join the task records to it")
    _check_time(run.started_at, "run.started_at")
    _need(re.fullmatch(r"[0-9a-f]{7,40}", run.repo_commit),
          f"run.repo_commit {run.repo_commit!r} is not a git sha; the suite manifests "
          "define what was scored, so a result must name their revision")
    _need(run.suites, "run.suites is empty")
    _need(run.harness.get("name") and run.harness.get("version"),
          "run.harness must name and version whatever produced this file")
    for name, ref in run.fleet.items():
        ref = ref if isinstance(ref, dict) else asdict(ref)
        _need(ref.get("image"), f"run.fleet[{name!r}] has no image")
        _need(_DIGEST.fullmatch(str(ref.get("digest", ""))),
              f"run.fleet[{name!r}].digest {ref.get('digest')!r} is not a sha256 digest. "
              "Every deploy entry names :latest, so the tag cannot say which build "
              "served this run, and tied search ranks permute across a rebuild")
        _need(ref.get("base_url"), f"run.fleet[{name!r}] has no base_url")


def validate_task(task: TaskResult):
    _need(task.status in STATUSES,
          f"task.status must be one of {sorted(STATUSES)}, got {task.status!r}")
    _need(task.uid and task.suite and task.instance,
          "a task record needs uid, suite and instance; task_id alone names three "
          "different tasks across VisualWebArena's suites")
    _check_time(task.started_at, "task.started_at")
    _check_time(task.ended_at, "task.ended_at")
    _need(task.duration_s >= 0, f"task.duration_s is {task.duration_s}")
    _need(task.attempt >= 1, f"task.attempt is {task.attempt}; attempts count from 1")
    if task.status == "scored":
        _need(task.score is not None and 0.0 <= task.score <= 1.0,
              f"a scored task needs a score in [0, 1], got {task.score!r}")
        _need(task.success is not None, "a scored task needs success set")
        _need(task.score_kind,
              "a scored task needs score_kind: the normalized number does not name "
              "the metric that produced it, and MiniWoB has four")
    else:
        _need(task.score is None,
              f"task.status is {task.status!r} but a score of {task.score!r} is "
              "recorded. A task that did not score has no score — writing 0 picks "
              "the denominator silently, and that choice is an open decision")
    if task.status == "blocked":
        _need(task.blocked_by,
              "a blocked task must name what blocks it, so the day a service is "
              "wired up the affected results are findable")
    if task.status == "error":
        _need(task.error and task.error.get("kind"),
              "an errored task must carry error.kind; 'it failed' is what the "
              "status already said")


def validate_task_against_suite(task: TaskResult, suite):
    """The rules that need the manifest — the two halves of the schema meeting."""
    _need(task.suite == suite.id,
          f"task.suite is {task.suite!r} but the suite is {suite.id!r}")
    expected = suite.uid(task.instance)
    _need(task.uid == expected,
          f"task.uid is {task.uid!r}; this suite's [instance].key makes it {expected!r}")
    if task.status == "scored" and not suite.tasks.instruction_is_recoverable:
        _need(task.instruction,
              f"{suite.id} renders its instruction at run time "
              f"(instruction_source = {suite.tasks.instruction_source!r}), so the text "
              "the agent saw exists nowhere but in this record")
    if task.raw_score is not None:
        low, high = suite.scoring.reward_range
        _need(low <= task.raw_score <= high,
              f"task.raw_score {task.raw_score} is outside {suite.id}'s reward_range "
              f"[{low}, {high}]")


def validate_run_against_suites(run: RunRecord, suites):
    """A run must answer every open decision its suites declare.

    Not a formality. `webarena-judge-model` decides whether 118 tasks are scored
    by gpt-4-1106-preview or by something else, and `miniwob-episode-deadline`
    decides whether an agent had ten seconds or longer. A mean score recorded
    without them is a number nobody can interpret later.
    """
    by_id = {s.id: s for s in suites}
    for name in run.suites:
        _need(name in by_id, f"run.suites names {name!r}, which is not a captured suite")
    for name in run.suites:
        suite = by_id[name]
        for decision in suite.open_decisions:
            _need(decision.id in run.decisions,
                  f"{name} declares open decision {decision.id!r} and run.decisions does "
                  f"not answer it: {decision.question}")
            taken = run.decisions[decision.id]
            _need(taken in decision.option_ids,
                  f"run.decisions[{decision.id!r}] is {taken!r}, which is not one of "
                  f"{list(decision.option_ids)}")
        unavailable = set(suite.requires.not_built) | set(suite.requires.not_deployed)
        for service in suite.requires.services:
            if service in unavailable:
                continue
            _need(service in run.fleet,
                  f"{name} requires {service!r} and run.fleet does not say where it was "
                  "or which build served it")


def validate_summary(summary: SummaryRecord, task_count: int):
    _check_time(summary.ended_at, "summary.ended_at")
    for status in summary.counts:
        _need(status in STATUSES, f"summary.counts has unknown status {status!r}")
    total = sum(summary.counts.values())
    _need(total == task_count,
          f"summary.counts sums to {total} but the file holds {task_count} task records")


def _encode(record, kind: str) -> dict:
    out = {"schema": SCHEMA, "record": kind}
    out.update(asdict(record))
    return out


class ResultsWriter:
    """Append records to one results.jsonl, validating each before it lands.

    Validation happens at write time on purpose. A malformed record that reaches
    disk is a record somebody will read back and believe.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None
        self._tasks = 0
        self._run_id = ""

    def __enter__(self):
        self._fh = self.path.open("a", encoding="utf-8")
        return self

    def __exit__(self, *exc):
        self._fh.close()
        self._fh = None
        return False

    def _write(self, payload: dict):
        self._fh.write(json.dumps(payload, sort_keys=True) + "\n")
        # Flushed per record: a run that dies is exactly when the file matters.
        self._fh.flush()

    def run(self, record: RunRecord):
        validate_run(record)
        self._run_id = record.run_id
        self._write(_encode(record, "run"))

    def task(self, record: TaskResult):
        validate_task(record)
        if self._run_id and record.run_id != self._run_id:
            raise InvalidRecord(
                f"task.run_id {record.run_id!r} does not match the file's run "
                f"{self._run_id!r}")
        self._tasks += 1
        self._write(_encode(record, "task"))

    def summary(self, record: SummaryRecord):
        validate_summary(record, self._tasks)
        self._write(_encode(record, "summary"))


def read(path: Path) -> list[dict]:
    """Every record in a results file, in order, as plain dicts.

    A truncated last line is dropped rather than fatal: the writer flushes per
    record, so a partial line means the run died mid-write, which is the case
    the streaming layout exists to survive.
    """
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("schema") != SCHEMA:
            raise InvalidRecord(
                f"{path}: record declares schema {record.get('schema')!r}, not {SCHEMA!r}")
        if record.get("record") not in RECORD_KINDS:
            raise InvalidRecord(f"{path}: unknown record kind {record.get('record')!r}")
        out.append(record)
    return out
