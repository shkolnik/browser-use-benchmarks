# The suite manifest schema

Every `tasks/<benchmark>/<suite>/suite.toml` describes one captured benchmark suite.
Discovery is by glob — `tasks/*/*/suite.toml`, no index files — so adding a suite is
creating a directory, the same rule `images/*/*/image.toml` follows.

A manifest has two halves. The **core**, specified here, is what a driver reads and is
identical in every suite. Everything else is whatever that benchmark needs, and the loader
carries all of it through untouched, because loading a manifest must not be the thing that
loses what it records. A section the core does not claim lands in `Suite.extra`; a key the
core does not claim inside a section it does lands in that section's own `.extra`, so
`suite.scoring.extra["dead_fields"]` is still there after normalization. `CLAIMED_KEYS` in
`harness/suite.py` declares what each section models, and a test asserts that every key of
every manifest is either modelled or in an `extra`.

`harness/suite.py` is the enforcing reader. A manifest that fails a rule below does not
half-load: it exits naming the file, the key, and the failure the rule prevents.

## Two path bases, one rule each

| Key | Relative to |
|---|---|
| `tasks.file` | the **suite** directory |
| `upstream.files[].path` | the **benchmark** directory |
| `external_pins[].pinned_at` | the **repo root** |

`upstream.files[].path` may not start with `../` or `/`. Scoring code is shared by every
suite of a benchmark and lives one level up from the suite, so a `../` spelling resolves
against whichever root a reader guesses.

## `[suite]`

```toml
name = "test"            # must equal the directory name — discovery is by glob
benchmark = "webarena"   # must equal the parent directory name
task_count = 812         # measured over the task file; 0 is rejected, an empty suite passes everything
description = "…"        # one line
```

## `[upstream]` and `[[upstream.files]]`

```toml
repo = "https://github.com/web-arena-x/webarena"
commit = "dce04686a56253aefba7b18a4fa0937cf1dc987b"   # a 40-char sha, never a branch or tag
license = "Apache-2.0"

[[upstream.files]]
path = "test/tasks.raw.json"              # benchmark-relative
upstream_path = "config_files/test.raw.json"
sha256 = "…"                              # of the bytes in THIS repo
bytes = 880776
```

The hashes are of the vendored bytes, so re-hashing them checks the vendoring rather than
restating upstream. At least one entry is required: a capture with no vendored blob has no
provenance to check.

## `[[external_pins]]`

Data a suite needs whose pin has one source of truth somewhere else.

```toml
[[external_pins]]
what = "items_human_ins.json"
pinned_at = "images/webshop/server/image.toml"    # repo-root-relative
sha256 = "…"                                       # optional; omit for a multi-file pin
```

Restating a sha256 in a second file is the silent-wrong-data hole `docs/design.md`
describes, not redundancy. This points at the pin instead of copying it, and a test checks
that the named file exists and still carries the hash.

## `[tasks]`

```toml
file = "tasks.raw.json"       # suite-relative
format = "json_array"         # json_array | json_array_of_strings | jsonl
instruction_source = "literal" | "server_rendered" | "page_rendered"
instruction_field = "intent"  # required iff instruction_source = "literal"
```

`instruction_source` is the field that decides whether a result record has to carry the
instruction text. `literal` means it is in the file and can be looked up later.
`server_rendered` (WebShop draws the price clause unseeded at boot) and `page_rendered`
(MiniWoB regenerates the utterance per episode) mean it exists nowhere but in the result.

Naming an `instruction_field` under the other two sources is rejected: the text is not in
the file, so naming a field claims otherwise.

## `[addressing]`

How a task record becomes the URL an agent starts at. Absorbs what pass 1 spelled as
`[sites]`, `[substitution]` and `[urls]`.

```toml
kind = "start_url_field"   # the record carries the URL(s)        — webarena, vwa
kind = "path_template"     # the URL is built from the instance   — webshop
kind = "record_path"       # the record carries a path            — miniwob
```

| Key | Applies to | Meaning |
|---|---|---|
| `field` | `start_url_field`, `record_path` | the record field holding the address; required |
| `template` | `path_template` | e.g. `/fixed_{index}`; must interpolate something |
| `separator` | `start_url_field` | in-band splitter, e.g. `" \|AND\| "`, one tab per part |
| `base_service` | any | which service a bare path is relative to |

`[addressing.sites]` maps a placeholder token to a service, named the way `images/*/*/`
names it. A token maps to a **service**, never to a URL: the base URL is composed at run
time from `deploy/compose.yml`'s published port and the service's own
`image.toml` `base_path`, and a pin has one source of truth.

`[addressing.substitution]` records how the tokens are replaced, verbatim from upstream —
`kind`, `order`, and whatever else that benchmark's substitution needs. Declaring
`[addressing.sites]` without it is rejected: the order is load-bearing wherever one token
is a prefix of another, and `__SHOPPING__` before `__SHOPPING_ADMIN__` is only safe because
of the trailing `__`.

## `[instance]`

What names one runnable task.

```toml
key = ["env_id", "seed", "data_mode"]
from_file = ["env_id"]
chosen_by_harness = ["seed", "data_mode"]
```

`from_file + chosen_by_harness` must equal `key`, in order: every part of the key comes from
exactly one of the two. A non-empty `chosen_by_harness` is the honest statement that the
suite is not enumerable — MiniWoB is 128 envs times however many seeds a run draws.

The **uid** is computed by the loader, not restated here: `<benchmark>/<suite>#<value>` for
a single-field key and `<benchmark>/<suite>#<k>=<v>,<k>=<v>` for a compound one. VWA's three
suites each restart `task_id` at 0, so the suite prefix is what stops three different tasks
sharing a name.

## `[auth]`

```toml
kind = "storage_state" | "none"
decided_by = "storage_state"   # required for storage_state: the record field that decides
tasks_with_session = 684
tasks_anonymous = 128
```

`decided_by` exists because the obvious field is the wrong one: WebArena's `require_login`
is `true` on all 812 records and carries no information, while `storage_state` being
non-null is what actually decides.

`[accounts.<name>]` carries `username` and `password` for the seeded benchmark logins,
transcribed verbatim from upstream. Its absence is what says a suite has none.

## `[[reset]]`

An array, because reset has a scope and a suite can declare more than one.

```toml
[[reset]]
scope = "task"          # task | episode | suite
kind = "none"           # none | new_session | page_episode | http_endpoint | container_recreate
contaminates = true     # does what a task changed survive into the next one?
cost_s = 0              # omit where unmeasured — an unmeasured number is left out, not guessed
how = "…"
```

At least one `task`- or `episode`-scoped entry is required, so something always says what a
driver does between two tasks. One scope has one entry.

`contaminates` is the field that stops one token meaning two opposite things. WebArena and
WebShop both reset nothing per task, but WebArena's tasks place orders and open issues
against a server that keeps them — so task order is part of the run definition — while
WebShop's server writes nothing at all. `kind = "none"` alone cannot tell those apart.
A `container_recreate` entry that also claims to contaminate is rejected.

## `[scoring]`

```toml
evaluator_kinds = ["string_match", "url_match", "program_html"]
reward_range = [0.0, 1.0]
success = "score == 1.0"     # a predicate over `score`, as an expression
offline_scorable = 325       # these two partition task_count
needs_live_page = 487
needs_judge = 118            # crosses the partition; <= task_count
aggregate = "mean"
```

`offline_scorable + needs_live_page` must equal `task_count`. A gap is a task nothing plans
to score.

`reward_range` is required even for the binary benchmarks, so a reader comparing four suites
compares four ranges rather than a range and a boolean.

`[scoring.census]` holds the counts measured over the task file — 34 of them for WebArena.
They are worth keeping and a driver reads none of them, so they sit below the six keys it
does read. Named behaviour sub-tables (`[scoring.judge]`, `[scoring.formula]`,
`[scoring.r_type]`, …) stay beside `[scoring.census]` and describe how scoring works rather
than how much of it there is.

### `[scoring.alternatives]`

```toml
separator = " |OR| "
split_in = ["eval.reference_url", "eval.program_html[].required_contents.must_include[]"]
matched_literally_in = ["eval.reference_answers.must_include[]"]
```

The in-band separator that offers a scorer a choice, as against `[addressing].separator`,
which opens tabs. Both are bare-string splits on the exact spaced token — splitting on
`|OR|` without its spaces leaves the alternatives with stray whitespace — and the two are
not interchangeable, which is why they live in the sections that own their jobs.

The fields are listed because upstream does not honour the separator uniformly and the value
does not say whether it will be. WebArena splits it in two fields and matches it literally in
`eval.reference_answers.must_include`; VisualWebArena's fork splits that same field. A
splitter applied to every string scores WebArena's task 386 wrong, and one applied to too few
scores VWA wrong the other way. `split_in` and `matched_literally_in` are what a
reimplementation follows instead of guessing, and a suite where a `func:` helper does its own
splitting says so in `split_by_the_named_helper` — splitting such a locator before `eval()`
corrupts the call.

## `[requires]`

```toml
services = ["webarena/shopping", …]   # named the way images/*/*/ names them
not_built = ["webarena/map-frontend"] # subsets of services, and disjoint from each other
not_deployed = ["webarena/map-tile", …]
required_env = ["REDDIT", …]
runnable = 684
blocked = 128

[[requires.blocked_by]]
service = "webarena/map-frontend"
tasks = 128
task_ids = []      # optional; must have length `tasks` when present
```

`not_built` and `not_deployed` are different problems with different fixes — nothing to
build one from, versus built and published but absent from `deploy/compose.yml` — so a name
in both is rejected.

`runnable + blocked` must equal `task_count`. `blocked` and `[[requires.blocked_by]]` imply
each other, and every blocking service must be `not_built` or `not_deployed`. Because a task
can be blocked by two missing services at once, the entries bound `blocked` from both sides
rather than summing to it: `max(tasks) <= blocked <= sum(tasks)`.

## `[[open_decisions]]`

Questions this repo has not answered. A default invented in a data file is a policy decision
nobody can find later.

```toml
[[open_decisions]]
id = "webarena-judge-model"     # slug, namespaced to the benchmark or to `fleet-`
scope = "suite"                 # suite | benchmark | fleet — how far one answer reaches
kind = "judge_model"            # the recurring question class
question = "…"
affects = "…"
affects_tasks = 118             # omit where it genuinely cannot be counted

[[open_decisions.options]]
id = "upstream-gpt4"
text = "…"
```

Options are tables with slug ids, not strings. A free-text option can be read by a person
and referenced by nothing, so a result record could not say which one a run took — and
`decisions` in the results schema is exactly that reference.

`kind` groups the questions that recur: four suites ask which model judges fuzzy matches,
three ask a reset policy, three ask an input-image size. `scope` says how far one answer
reaches, and a `fleet`-scoped id must carry the `fleet-` namespace so one answer is findable
by its id.

`[decisions_not_applicable]` maps a decision id to the reason it does not apply here. A
fleet-scoped question that a suite simply omits is indistinguishable from one nobody
considered; WebShop's search-tie exemption is a real answer and is recorded as one.
