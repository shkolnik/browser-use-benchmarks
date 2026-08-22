# The results schema

One `results.jsonl` per run. The first line is the `run` record, every line after it is a
`task` record appended as that task finishes, and the last is an optional `summary`.

Streaming rather than assembling a document at the end is deliberate: a run that dies
halfway leaves a file that is readable to the point it died, and the writer flushes per
record so that point is where it actually stopped.

`harness/results.py` is the enforcing writer. Validation happens **at write time**, because
a malformed record that reaches disk is a record somebody will read back and believe.

Every record carries `"schema": "browser-use-benchmarks/results@1"` and a `"record"` of
`run`, `task` or `summary`, so one file can hold all three and a reader can dispatch.

## What makes two runs comparable

That question, not "what happened", is what the schema is shaped by. Three fields carry the
weight, and none of them is the score.

**`run.fleet` records image digests, not tags.** Every entry in `deploy/compose.yml` names
`:latest`, which is a moving target by design. Search results with tied relevance permute
across a rebuild — measured, fleet-wide, and still open in `TODO.md` — so two runs against
`:latest` a month apart are two different benchmarks and only the digest says so.

**`run.decisions` answers the open decisions the suites declare.** Several of the 37 change
the metric rather than the implementation: which model scores WebArena's 118 fuzzy-match
tasks, whether MiniWoB ran at its published ten-second deadline, whether an unparseable
judge reply omits a task from the denominator or scores it 0. A mean recorded without them
is a number nobody can interpret later, including the person who produced it.

**`task.status` is not a score.** A task that errored is not a task that scored 0. Upstream
WebArena's runner catches an unparseable judge reply and omits the task, which raises the
reported success rate by shrinking the denominator; scoring it 0 is a different metric.
Recording the distinction is what lets a reader compute either one from the same file
instead of forcing the choice at write time.

## The `run` record

| Field | Meaning |
|---|---|
| `run_id` | joins every other record in the file |
| `started_at` | ISO-8601 UTC |
| `repo_commit` | this repo's revision — the manifests define what was scored |
| `harness` | `{name, version}` of whatever produced the file |
| `agent` | `{name, model, version}` |
| `suites` | `["webarena/test", …]` |
| `fleet` | service name → `{image, digest, base_url}` |
| `decisions` | open-decision id → the **option id** taken |
| `budget` | the agent's step limit, wall clock, retries |
| `scorer` | the versions that move a score without being the agent |
| `notes` | free text |

`budget` is here and not in a suite manifest on purpose. A manifest states what upstream
does; a step limit is this repo's choice. The one budget that *is* upstream's — MiniWoB's
`core.EPISODE_MAX_TIME` — stays in the manifest, and whether a run changed it is an open
decision rather than a field.

`scorer` is for the things that change a number while being invisible in it: the judge model
and its sampling parameters, spaCy's `en_core_web_sm` (not `en_core_web_lg`, which upstream
downloads and never loads), `thefuzz` 0.19.0 on its pure-Python difflib backend, the nltk
punkt data 31 WebArena tasks need. Each is documented in a suite manifest as changing
results.

## The `task` record

| Field | Meaning |
|---|---|
| `uid` | `<benchmark>/<suite>#<key>` — computed by the loader from `[instance].key` |
| `suite`, `instance` | the suite id and the key fields with their values |
| `status` | `scored` \| `blocked` \| `error` \| `skipped` |
| `position`, `attempt` | where in the run, and which try |
| `started_at`, `ended_at`, `duration_s` | |
| `instruction` | the text the agent was given |
| `answer` | the agent's final answer, where the benchmark has one |
| `score` | normalized to `[0, 1]`, comparable within a suite |
| `raw_score` | whatever the benchmark natively produced |
| `score_kind` | which metric produced `score` |
| `success` | |
| `evaluators` | `[{kind, score, detail}]` |
| `blocked_by` | services, when `status` is `blocked` |
| `error` | `{kind, message}`, when `status` is `error` |
| `native` | the benchmark-specific fields, verbatim |
| `artifacts` | paths to a trajectory, screenshots, a HAR |

**`score` versus `raw_score`.** MiniWoB's `env_reward` runs `[-1, 1]` and is time-decayed: a
correct answer at t=9s on a 10s task yields 0.1 while `raw_reward` is 1.0. A file holding
only the native number would make a working agent look broken and could not be compared with
a WebArena 1.0. So `score` is normalized and `raw_score` is kept beside it, and `score_kind`
names the metric — because MiniWoB has four reward processors and the choice is itself an
open decision, so the number does not name itself.

**`native` is the escape hatch, and it is a deliberate one.** The typed fields above are the
ones that mean the same thing across four benchmarks. Everything that does not goes here
verbatim, so a rescore stays possible: MiniWoB's `done` and `reason` — the only field that
separates a deadline expiry from a wrong answer, since both terminate with reward −1 —
WebShop's `w_price` and `r_att` and its server boot id, WebArena's judge replies and the
sha256 of the substituted config file the evaluator actually read.

**`instruction` is mandatory where it cannot be recovered.** WebShop renders its goal at
server boot with an unseeded price draw and MiniWoB regenerates its utterance per episode,
so for those two the text exists nowhere but in the result. The rule is derived from the
manifest's `instruction_source` rather than restated, so it follows the capture.

## The `summary` record

`{run_id, ended_at, counts, aggregate}`. `counts` maps each status to how many task records
carry it and must sum to the number of task records in the file. `aggregate` is whatever the
suite's `scoring.aggregate` calls for.

It is a separate final record rather than fields on the `run` record because the run record
is written first, before any of it is known. Rewriting the first line of a file that is
being appended to is how a crash produces a file that parses and lies.

## The rules with teeth

`harness/results.py` enforces these; each exists because breaking it produces a file that
reads as valid and is wrong.

- `status = "scored"` requires `score` in `[0, 1]`, `success`, and `score_kind`.
- Any other status requires `score` to be **absent**. Writing 0 for a task that did not
  score picks the denominator silently, and that choice is an open decision.
- `blocked` requires `blocked_by`; `error` requires `error.kind`.
- `uid` must equal what the suite's `[instance].key` makes it. VWA's three suites each
  restart `task_id` at 0, so an integer alone names three different tasks.
- `raw_score` must lie inside the suite's `scoring.reward_range`.
- A run must answer **every** open decision each of its suites declares, and each answer must
  be one of that decision's declared option ids.
- A run's `fleet` must name every service its suites require and can actually reach — the
  `not_built` and `not_deployed` ones excepted, since those are why tasks are `blocked`.
