# One schema for task definitions and results — design

Pass 1 captured four benchmarks' task sets, scoring code and provenance into `tasks/`
(`2026-08-22-benchmark-test-suites-design.md`). Each manifest describes its benchmark in
that benchmark's own vocabulary, which is what made the capture honest and is exactly what
a driver cannot consume: six ways to say "here is the URL this task starts at" is six code
paths.

Pass 2 does not rewrite that vocabulary. It adds **one vocabulary for the facts a driver
reads**, leaves every benchmark-specific fact where it is, and adds the results half, which
does not exist at all yet.

Three things come out of it:

1. a **normalized core** in `suite.toml` — the sections a driver reads, identical in all six;
2. a **results schema** — what a run emits, and what makes two runs comparable;
3. a **loader** that enforces both, and a CLI that resolves a real task against a real fleet,
   because a schema nothing consumes is a document, not an interface.

## What is accidental variation and what is not

The six manifests were compared key by key. Most differences are real — WebShop has no
placeholder tokens because it has no cross-site URLs — and those stay. What follows are the
places where all six record the same fact in different words, which is the whole of the
standardization work.

| The fact | webarena | vwa | webshop | miniwob |
|---|---|---|---|---|
| how a task becomes a URL | `[sites]` + `tasks.start_url_field` | `[sites]` | `[addressing].path_template` | `[urls].non_flight` / `.flight` |
| what names one runnable instance | `tasks.id_field` | `tasks.id_field` | index, `id_field = ""` | `tasks.instance_key` |
| whether a session is needed | `[login]` | `tasks.require_login` | `requires.accounts = false` | `requires.accounts = false` |
| what happens between tasks | `[reset]` + `reset.suite_*` | `[reset]` | `[reset]` | `[reset]` |
| the score's shape | `scoring.binary_per_task` | `scoring.eval_types` | `scoring.reward_range` | `scoring.reward_range` |

Five sections answer those five questions in every suite after this pass: `[addressing]`,
`[instance]`, `[auth]`, `[[reset]]`, `[scoring]`.

## `[addressing]` — how a task becomes a URL

The one section a driver cannot do without, and today the most scattered. It absorbs
`[sites]`, `[substitution]` and `[urls]`, so the whole "how do I reach this task" story is
in one place.

```toml
[addressing]
kind = "start_url_field"   # the record carries the URL(s)      — webarena, vwa
kind = "path_template"     # the URL is built from the index    — webshop
kind = "record_path"       # the record carries a path          — miniwob
```

Each kind names what it needs and nothing else: `field` and `separator` for
`start_url_field`, `template` for `path_template`, `field` for `record_path`. A
`base_service` names which service a bare path is relative to, and is absent exactly where
the record's own token supplies the origin.

`[addressing.sites]` is pass 1's `[sites]`, unmoved in content. `[addressing.substitution]`
is pass 1's `[substitution]`. The substitution order stays load-bearing and stays verbatim:
`__SHOPPING__` before `__SHOPPING_ADMIN__`, safe only because of the trailing `__`.

## `[instance]` — what names one runnable task

`task_id = 42` is ambiguous across VisualWebArena's three suites and meaningless for
MiniWoB, where a task is a page plus a seed plus a data mode. A results file that records
only an integer cannot be read back.

```toml
[instance]
key = ["env_id", "seed", "data_mode"]   # what together names one instance
from_file = ["env_id"]                  # what the task file supplies
chosen_by_harness = ["seed", "data_mode"]
```

`key` is the concatenation of the other two, in order, and the loader checks that. The
**uid** — `<benchmark>/<suite>#<k>=<v>,…`, shortened to `<benchmark>/<suite>#<v>` for a
single-field key — is computed by the loader from `key`, not restated in six files. A pin
has one source of truth and so does a naming rule.

`chosen_by_harness` is the honest statement of what pass 1 found and had nowhere to put:
MiniWoB's suite is not enumerable. 128 registered envs times however many seeds the harness
draws is the run, and `[[open_decisions]] miniwob-seed-policy` is the question of how many.

## `[auth]` — whether a task needs a session

```toml
[auth]
kind = "storage_state"   # webarena, vwa
kind = "none"            # webshop, miniwob
decided_by = "storage_state"
tasks_with_session = 684
tasks_anonymous = 128
```

`requires.accounts = false` goes away: it restated the absence of `[accounts]`, and its
comment — *why* a benchmark has no accounts — moves here, where the reader is already asking.

## `[[reset]]` — an array, because there is more than one

Pass 1 recorded WebArena's two resets in one table with a `suite_` prefix on half the keys,
and recorded VisualWebArena's shopping and reddit suites as `kind = "container_recreate"`
with no way to say that *nothing at all* happens between tasks. Both are the same shape
problem: reset has a scope, and a suite can declare more than one.

```toml
[[reset]]
scope = "task"           # task | episode | suite
kind = "none"            # none | new_session | page_episode | http_endpoint | container_recreate
cost_s = 0               # absent where unmeasured — an unmeasured number is left out, not guessed
how = "…"
```

The enum gains `new_session` for WebShop. Pass 1 declared it `none` and said in prose that
per-task isolation comes from using a fresh session id rather than from resetting anything.
That is true and it is not `none`: a driver that takes `none` literally reuses `/fixed_0`,
lands in a `user_sessions` entry already flagged done, and drives a finished session. The
enum now has a value for "isolation without server state", so the prose no longer has to
carry it.

## `[scoring]` — behaviour in the section, census below it

`[scoring]` in `tasks/webarena/test/suite.toml` carries 40 keys, of which six are read by a
driver and 34 are counts measured over the task file. Both are worth keeping and they are
not the same kind of thing. The counts move to `[scoring.census]`; named behaviour
sub-tables (`[scoring.judge]`, `[scoring.formula]`, `[scoring.r_type]`) stay where they are.

What is left in `[scoring]` is universal:

```toml
evaluator_kinds = […]
reward_range = [0.0, 1.0]
success = "score == 1.0"     # the predicate, as an expression over `score`
offline_scorable = 325       # these three partition task_count and are
needs_live_page = 487        # the only cross-benchmark scoring comparison
needs_judge = 118            # the fleet has
aggregate = "mean"
```

`reward_range` becomes universal, including for the two binary benchmarks, so that a reader
comparing four suites is comparing four ranges rather than a range and a boolean.

The `" |OR| "` separator moves into `[scoring.alternatives]`, which four manifests spelled
four ways: a `[scoring.separators]` sub-table, a `[scoring].alternative_separator` key, and
twice a leftover `[tasks].multi_value_separators` pairing it with the unrelated `" |AND| "`.
It is a scoring concern and `" |AND| "` is an addressing one, so each goes to the section
that owns its job. `[scoring.alternatives]` also lists the fields upstream honours it in,
because that is not uniform and not inferable from the value: WebArena matches it literally
in `eval.reference_answers.must_include` and VisualWebArena's fork splits that same field.
Two of the four manifests described its reach wrongly before the fields were read off the
vendored evaluators — one named a field carrying zero occurrences and none mentioned the 21
in the field WebArena will not split.

## `[[open_decisions]]` gains `scope` and `kind`

37 decisions were recorded, and the recurrence is not noise: four suites ask which model
judges fuzzy matches, three ask a reset policy, three ask an input-image size, two ask a VQA
model. Pass 1 could only express sharing by repeating an id verbatim, which it did twice
(`fleet-search-tie-ordering`, `vwa-dataset-pin-source`) and could not do for the rest,
because each suite's `affects` is genuinely different.

```toml
[[open_decisions]]
id = "webarena-judge-model"
scope = "suite"          # suite | benchmark | fleet — how far one answer reaches
kind = "judge_model"     # the recurring question class
```

`kind` is what lets a harness answer "which judge model" once and apply it to the four
suites that ask, while each keeps its own blast radius. `scope` says whether an answer is
transferable at all. Neither invents an answer; both make the questions addressable.

## The results schema

Nothing in this repo emits or describes a result today. The schema is new, and it is
designed against one question: **what has to be recorded for two runs to be comparable?**

One `results.jsonl` per run. The first line is the `run` record; every line after it is a
`task` record, appended as the task finishes. Streaming rather than assembling a document at
the end is the same reasoning as the fleet's metrics: a run that dies halfway leaves a file
that is still readable to the point it died.

### The `run` record

```json
{"schema": "browser-use-benchmarks/results@1", "record": "run",
 "run_id": "…", "started_at": "…",
 "repo_commit": "…",
 "harness": {"name": "…", "version": "…"},
 "agent": {"name": "…", "model": "…", "version": "…"},
 "suites": ["webarena/test"],
 "fleet": {"webarena/shopping": {"image": "ghcr.io/…", "digest": "sha256:…", "base_url": "http://…"}},
 "decisions": {"webarena-judge-model": "…", "fleet-search-tie-ordering": "…"}}
```

Two fields carry the weight.

**`fleet` records image digests, not just names.** Search results with tied relevance
permute across a rebuild — measured, fleet-wide, and still open in `TODO.md`. Two runs
against `:latest` a month apart are two different benchmarks, and the digest is the only
thing that says so.

**`decisions` answers the open decisions this run depended on.** Several of the 37 change
the metric rather than the implementation: which model judges 118 WebArena tasks, whether
MiniWoB's episode deadline was raised, whether an unparseable judge reply omits a task from
the denominator or scores it 0. A score recorded without them is a number nobody can
interpret later, including the person who produced it. The loader knows which decisions a
suite declares, so this is checkable rather than aspirational.

### The `task` record

```json
{"schema": "…", "record": "task", "run_id": "…",
 "uid": "webarena/test#42", "suite": "webarena/test", "instance": {"task_id": 42},
 "attempt": 1, "status": "scored",
 "started_at": "…", "ended_at": "…", "duration_s": 12.3,
 "instruction": "…", "answer": "…",
 "score": 1.0, "raw_score": 1.0, "score_kind": "…", "success": true,
 "evaluators": [{"kind": "string_match", "score": 1.0, "detail": {}}],
 "artifacts": {}}
```

**`score` is normalized to `[0, 1]`; `raw_score` is whatever the benchmark natively
produces.** MiniWoB's `env_reward` runs `[-1, 1]` and is time-decayed — a correct answer at
t=9s on a 10s task yields 0.1 — so a file that recorded only the native number would make a
working agent look broken and would not be comparable with a WebArena 1.0. `score_kind`
names the processor that produced the normalized value, because for MiniWoB that choice is
itself an open decision with four options.

**`status` is not a score.** `scored | blocked | error | skipped`. It exists because the
denominator is contested: upstream WebArena's runner catches an unparseable judge reply and
omits the task, which raises the reported success rate by shrinking the denominator, while
scoring it 0 is a different metric. Recording `error` distinctly from `score = 0` is what
makes both computable from one file instead of forcing the choice at write time. `blocked`
is a first-class status because 1,015 of the captured 13,937 tasks cannot run against this
fleet at all, and `blocked_by` names the services.

**`instruction` is required where it cannot be recovered.** WebShop renders its goal text at
server boot with an unseeded price draw; MiniWoB regenerates its utterance per episode from
the seed. For those two, the instruction the agent saw exists nowhere but in the result. The
manifest already says which case a suite is in — `instruction_source` — so the requirement
is derived from the schema rather than restated.

## The loader

`harness/`, a new top-level package. Not `builder/`: `discover.py` takes its
"shared build inputs changed: building all" branch on any change under `builder/`, and
`build.yml`'s push filter names `images/**`, `builder/**`, `bin/**`. A schema tweak must not
rebuild the fleet, which is also why the CLI is `python3 -m harness` and not `bin/tasks`.

- `harness/suite.py` — dataclasses, `load_suite`, `discover_suites`. Stdlib only, and it
  fails the way `builder/manifest.py` fails: loudly, naming the file, the key, and the
  failure the check prevents. Normalizing is not allowed to lose anything: each core section
  keeps what the core does not model in its own `.extra`, `CLAIMED_KEYS` declares what is
  modelled, and a test asserts that every key of every manifest is one or the other. Without
  it, moving `[login]` into `[auth]` would silently drop `sessions` and `never_generated`,
  which the file would still record and nothing reading it would see.
- `harness/results.py` — the two records, a JSONL writer and reader, and validation. Stdlib.
- `harness/fleet.py` — service name to base URL. Reads `deploy/compose.yml` for the
  published port and `images/*/*/image.toml` for `base_path`; the `images/<b>/<s>` name is
  derived from each compose entry's image reference rather than from a hand-kept table,
  which is the same no-index-files rule the rest of the repo follows.
- `harness/resolve.py` — a suite plus an instance key plus a fleet gives a `TaskCase`: the
  instruction or how to obtain it, the start URLs with tokens substituted, the services it
  needs and whether they are up, the session file, the viewport, the evaluator spec.

`python3 -m harness resolve webarena/test 42 --host bench.example` printing a runnable case
is the acceptance test for the whole pass: if a fact is missing from the schema, that command
cannot print it.

Whether a case is runnable is decided per task, not per suite. A `TaskCase` is blocked by the
services *it* addresses: by `[[requires.blocked_by]].task_ids` where a manifest enumerates
them, otherwise by whether the missing service's placeholder token survives substitution in
that record, and for a suite with no tokens by the suite's gaps. A suite-level answer would
block all 812 WebArena tasks over a map front end 684 of them never touch. Recounting each
suite that way and comparing against its `[requires].blocked` reproduces all six declared
numbers, so the rule a driver applies is the rule the counts were made with.

## Not in this pass

No browser, no agent, no scorer, no execution of any kind. Pass 2 ends with a schema, a
loader that enforces it, and a resolver that proves it is sufficient. What consumes it is
pass 3, and it will be the first thing in this repo that needs a running fleet.

## Testing

`tests/test_task_schema.py` unit-tests the loader against synthetic manifests, including
every fail-loud path — the house style is that a bad manifest dies with a message naming the
failure it prevents, and an untested error message is prose.

`tests/test_results_schema.py` does the same for the results records, and holds the two
cross-schema rules: a run must answer every open decision its suites declare, and a task
record must carry `instruction` where the suite's `instruction_source` says it is not
recoverable.

`tests/test_task_suites.py` gains the normalized core over the real six files, keeping pass
1's posture of reading what the repo ships rather than a fixture. Vacuity guards on every
collection, for the reason pass 1 gave: a vacuous suite is indistinguishable from a passing
one.

The restructuring itself is verified mechanically rather than trusted. Every key/value pair
and every comment line in the six manifests is extracted before and after and diffed against
a declared rename map, so a comment lost in a move is a failure and not a discovery six
months later.
