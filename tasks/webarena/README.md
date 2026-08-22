# tasks/webarena — WebArena's 812-task suite

Everything needed to define, address and score a WebArena task, captured from
`web-arena-x/webarena` at commit `dce04686a56253aefba7b18a4fa0937cf1dc987b`. This directory
holds no driver and no runner: it is the facts a harness reads before it starts one.

```
tasks/webarena/
├── README.md                  ← you are here
├── LICENSE.upstream           ← Apache-2.0, upstream's copy
├── CITATION.cff               ← upstream's BibTeX; the attribution the licence asks for
├── test/
│   ├── suite.toml             ← first-party manifest: what the suite needs and what it scores
│   └── tasks.raw.json         ← upstream config_files/test.raw.json, byte-identical
└── upstream/                  ← upstream's scoring chain, unmodified
    ├── browser_env/{env_config,auto_login}.py
    ├── evaluation_harness/{__init__,evaluators,helper_functions}.py
    └── scripts/generate_test_data.py
```

`suite.toml` is the file to read second. It carries the same facts in a form a loader can
consume, plus the four questions this repo has not answered, as `[[open_decisions]]` entries.

## Why the raw form

Upstream's committed artifact is the *template*: site URLs are placeholder tokens, and a
27-line generator substitutes them from environment variables at run time. The 812
generated per-task JSONs are `.gitignore`d upstream and exist in no checkout.

The template is what is vendored. Substituting at capture time would bake one deployment's
hostnames into git and destroy the relocatability that `deploy/` exists to provide.

## A task record

Task 621, re-wrapped for width and otherwise unchanged — one of the smaller records that
still shows every moving part:

```json
{
  "sites": ["reddit"],
  "task_id": 621,
  "require_login": true,
  "storage_state": "./.auth/reddit_state.json",
  "start_url": "__REDDIT__",
  "geolocation": null,
  "intent_template": "Ask for advice about {{issue}} in a subreddit for relations",
  "instantiation_dict": { "issue": "cheat" },
  "intent": "Ask for advice about cheat in a subreddit for relations",
  "require_reset": false,
  "eval": {
    "eval_types": ["url_match", "program_html"],
    "reference_answers": null,
    "reference_url": "__REDDIT__/f/relationship_advice",
    "program_html": [
      {
        "url": "func:reddit_get_post_url('__last_url__')",
        "locator": "document.querySelector('.submission__inner').outerText",
        "required_contents": { "must_include": ["cheat"] }
      }
    ],
    "url_note": "GOLD in PRED"
  },
  "intent_template_id": 12
}
```

Five fields are live. `intent` is the instruction handed to the agent — already slot-filled,
so `intent_template` and `instantiation_dict` are provenance. `start_url` is where the
browser opens. `storage_state` names a Playwright cookie file and, being non-null, is what
actually decides that this task runs logged in. `geolocation` is null on all 812. `eval` is
the scoring block.

The rest is metadata that no code reads, and two of those fields mislead:

- **`sites` is not a requirement list.** It groups tasks for reporting. 23 tasks are tagged
  `wikipedia` and not one of them references a Wikipedia URL in any field — all 23 start at
  `__MAP__` (17) or `__GITLAB__` (6). Its element order is inconsistent too: both
  `["gitlab", "reddit"]` (10 tasks) and `["reddit", "gitlab"]` (8) occur, as do
  `["wikipedia", "map"]` (16) and `["map", "wikipedia"]` (1). Key it as a set, never as a
  tuple.
- **`require_reset` is `false` on all 812** and `require_login` is `true` on all 812. Neither
  carries information. `require_reset: false` is not a statement that no reset is needed —
  see [Reset](#reset).

`task_id` runs 0..811 with no gaps and equals the record's index in the array, so the array
can be indexed directly.

### The record shape is not uniform

A strict typed loader over the 812 will reject the file, and the irregularities are data
rather than damage:

| irregularity | where |
|---|---|
| a second `string_note` at the **top level**, outside `eval` | task 723 |
| `eval.annotation_note`, an annotator's scratch note | task 197 |
| `eval.reference_url` holding prose, not a URL | task 247 — `"Valorie doesn't have a email in the system"` |
| `eval.url_note` absent, relying on the evaluator's default | tasks 174–182 |
| `eval.reference_url` null (7) or the empty string (599) | present on all 812 |
| `eval.reference_answers` null | the 477 tasks with no `string_match`, exactly |

## Placeholders become URLs

Five tokens occur in the file. The counts are exact, across every field:

| token | occurrences | service |
|---|---|---|
| `__GITLAB__` | 385 | `webarena/gitlab` |
| `__REDDIT__` | 336 | `webarena/reddit` |
| `__SHOPPING_ADMIN__` | 300 | `webarena/shopping-admin` |
| `__SHOPPING__` | 281 | `webarena/shopping` |
| `__MAP__` | 133 | `webarena/map-frontend` — not built here, see [What cannot run here](#what-cannot-run-here-today) |

`__WIKIPEDIA__` occurs **zero** times even though the generator substitutes it, and
`__HOMEPAGE__` occurs zero times and is not in the generator's replace list at all. Both env
vars are still asserted non-empty at import, so the evaluators will not import without them.

Substitution is a whole-file text replace on the raw bytes, **before** `json.loads`, in this
order:

```
__GITLAB__  __REDDIT__  __SHOPPING__  __SHOPPING_ADMIN__  __WIKIPEDIA__  __MAP__
```

`__SHOPPING__` is replaced before `__SHOPPING_ADMIN__`, and that is safe only because of the
trailing `__`: the literal `__SHOPPING__` is not a prefix of `__SHOPPING_ADMIN__`, since
`__SHOPPING_A` is not `__SHOPPING__`. A prefix-matching regex, or any pattern that treats
the trailing underscores as optional, turns all 300 `__SHOPPING_ADMIN__` occurrences into
`<shopping-url>_ADMIN__` and leaves valid-looking JSON behind. Reproduce the replace; do not
improve on it.

Replacing bytes rather than walking parsed values is also load-bearing: 60 token occurrences
are inside *expected page content* (`eval.program_html[].required_contents.must_include[]`),
not in URL fields.

Two lowercase markers in the same double-underscore shape must survive untouched, because
they are substituted much later, at scoring time: `__last_url__` (65 occurrences, replaced
with `page.url`) and `__page__` (12, replaced with the identifier `page`). The site tokens
being uppercase-only is what keeps a scan for them off these.

### Three separators, all split as bare strings

All three are spelled with surrounding single spaces, and they are not interchangeable.

- `" |AND| "` — `start_url` only. 5 tasks (431–435) join 2 or 3 URLs; upstream opens one tab
  per URL and makes the first active. 812 records hold **818** start URLs.
- `" |OR| "` in `eval.reference_url` — 7 tasks (604, 608, 609, 625, 681–683). Any alternative
  matching scores 1.
- `" |OR| "` inside one `eval.program_html[].required_contents.must_include` element — 5
  elements, across tasks 431, 750, 751, 755, 756.

The trap is the fourth case that does *not* exist: `StringEvaluator.must_include` does **not**
split on `" |OR| "`. Task 386's `reference_answers.must_include` is `["65 |OR| 3"]` and is
matched as that literal substring.

## Logging in

`storage_state` names a Playwright cookie file; 684 tasks name one and 128 do not.

| cookie file | tasks |
|---|---|
| `shopping_state.json` | 187 |
| `gitlab_state.json` | 186 |
| `shopping_admin_state.json` | 182 |
| `reddit_state.json` | 111 |
| `gitlab.reddit_state.json` | 18 |
| (none — anonymous) | 128 |

The site combination is parsed out of the **basename**
(`rsplit("_", 1)[0].split(".")`), so those filenames are an interface, not labels.

`upstream/browser_env/auto_login.py` mints them by driving the real login forms with the
`ACCOUNTS` credentials, which `test/suite.toml` transcribes into an `[accounts]` table. Those
are the benchmark application logins baked into the published images; they are public in the
upstream repository, and the scoring path needs them here — 15 tasks mint a Magento REST
admin token from `shopping_site_admin`.

`auto_login` generates a file for each single site and each 2-combination of
`[gitlab, shopping, shopping_admin, reddit]`, **except** `reddit × shopping` and
`reddit × shopping_admin`, which it skips. Two consequences are visible in the task data and
are the benchmark rather than a capture defect: the 5 tasks tagged `["shopping", "reddit"]`
(671–675) carry `reddit_state.json` only, so the agent is logged out of shopping; and tasks
759 and 760 are tagged `["map", "shopping_admin"]` with a null `storage_state`, so they run
fully anonymous.

## Reset

There is no per-task reset. Upstream recreates the browser context and re-mints cookies
between tasks; server-side state is never rolled back. A task that places an order, opens a
GitLab issue, posts a submission or creates a cart rule changes what every later task sees,
so **task order is part of the run definition** until someone decides otherwise.

The only documented reset is whole-suite: stop, remove and re-run the shopping,
shopping_admin, gitlab and forum containers after all 812 examples, then re-apply the
`base_url` / `external_url` reconfiguration. Map and Wikipedia are read-only and are not in
that list. In this repo the equivalent is a fresh container from the published image, for
those four services and no others:

```
BENCH_HOST=<addr> docker compose -f deploy/compose.yml \
  up -d --force-recreate --wait shopping shopping-admin reddit gitlab
```

`--force-recreate` is what discards the state, and it is enough because no service in
`deploy/compose.yml` declares a volume — everything a task wrote lives in the container's
writable layer. Naming the four matters: `down` would also stop wikipedia, classifieds,
webshop and miniwob, which no task has dirtied.

Budget what this repo has measured for the boot half of that: gitlab pays ~28 s of
`gitlab-ctl reconfigure` on every boot where the address changes — and it always changes,
because the restored dataset carries CMU's own hostname — with a healthy boot at 61 s. Both
numbers are repeated here from where they were measured, the gitlab service comment in
`deploy/compose.yml` and `docs/service-contract.md`. shopping, shopping-admin and reddit have
no measured boot anywhere in this repo, only the `HEALTHCHECK` start-period ceilings in their
Dockerfiles — 900 s, 300 s and 90 s — which are budgets and not measurements.

## Scoring

Three evaluators exist, named by `eval.eval_types`, and `evaluator_router` raises on any
other literal. A task may list two; their scores are **multiplied**, so every listed
evaluator must return exactly 1.0. Success is binary per task and the published metric is the
mean over the 812.

| evaluator | tasks | reads |
|---|---|---|
| `string_match` | 335 | the final answer string |
| `url_match` | 205 | the live `page.url` |
| `program_html` | 411 | the live page, navigating and evaluating JS |

The combinations in use — there is no three-evaluator case:

`["string_match"]` 325 · `["program_html"]` 282 · `["program_html", "url_match"]` 129 ·
`["url_match"]` 66 · `["string_match", "url_match"]` 10

**Only 325 tasks are offline-scorable.** The other 487 need the live browser *after* the
episode ends: `URLEvaluator` reads `page.url`, and `HTMLContentEvaluator` navigates and runs
JS on the page. A stored transcript is not enough.

### `string_match`

`eval.reference_answers` carries one or two approach keys — `exact_match` (45 tasks),
`must_include` (176), `fuzzy_match` (118). Four tasks (34, 35, 267, 268) carry two at once.

`must_include` is a plain substring test on the cleaned answer, except when the reference
list holds exactly one element whose cleaned value is a single *character*: then the
prediction is tokenized with `nltk.word_tokenize`, so an answer of `10` no longer contains
the gold `0`. 31 tasks are on that path, which is why nltk's `punkt` data is a scoring
dependency.

### The LLM judge — 118 tasks

Of the 118 `fuzzy_match` tasks, 82 carry a list and always call the judge, once per element:
**145 calls minimum**. The other 36 carry the literal string `"N/A"` and are the
unachievable-task path — they try `exact_match` against `"N/A"` first, so a bare `N/A` answer
scores 1 with no judge call, and only a longer answer reaches `llm_ua_match`, for up to 36
more calls.

`eval.string_note` is present on all 335 `string_match` tasks and non-empty on exactly those
36, where it is the "actual unachievable reason" fed to the judge. The evaluator reads it
unguarded, so dropping the key from a re-serialisation breaks those 36 with a `KeyError`.

Two things about the judge are part of the metric, not implementation detail. The prompts are
exact strings in `upstream/evaluation_harness/helper_functions.py`. And the reply parsing
**raises**: `llm_fuzzy_match` returns 0.0 when the reply contains `partially correct` or
`incorrect`, and otherwise asserts `correct` is present — an ordering that matters because
`incorrect` contains `correct` — while `llm_ua_match` asserts `same`. An off-script reply
raises `AssertionError`. Upstream's `run.py`, which is not vendored here, catches it and the
task is omitted from the scored set rather than scored 0, which raises the reported success
rate by shrinking the denominator. Which model judges, and what an unparseable reply means,
are both open: see `[[open_decisions]] webarena-judge-model`.

### `program_html` — 411 tasks, 651 targets

Each target names where to look, what to extract and what must be there. Targets are scored
in sequence and multiplied.

Where to look (`url`): `last` (238 targets) means the page as the agent left it, no
navigation. A templated URL (338) or a `func:` expression (75) means `page.goto` followed by a
hard `time.sleep(3)` — **413 navigating targets, 1,239 s of pure sleep** across a full
scoring pass, before any page-load time. Upstream carries its own TODO on that sleep; whether
it is budgeted is open.

What to extract (`locator`): a JS expression (524 targets), `page.evaluate`d with exceptions
swallowed into `""` — a selector that stops matching fails the task silently rather than
erroring; empty (105), meaning the full `page.content()`; or a `func:` expression (22).

What must be there (`required_contents`): `must_include` (463 targets, with `" |OR| "`
alternatives honoured inside each element) or `exact_match` (188). Exactly one key; anything
else raises.

10 targets across tasks 699–703 also carry `prep_actions` — JS run before the locator, inside
a try/except that swallows everything. All are Magento admin accordions that must be clicked
open before the field is readable.

### The `func:` helpers are names, not a spec

79 tasks carry at least one `func:` expression, and upstream resolves it through a bare
Python `eval()` against `evaluators.py`'s module globals. The names *are* the interface:

| helper | targets | what it does |
|---|---|---|
| `reddit_get_post_url` | 65 | `urlparse` string surgery; no API |
| `gitlab_get_project_memeber_role` | 12 | `page.evaluate` DOM scrape; **not** the GitLab API |
| `shopping_get_latest_order_url` | 10 | Magento REST admin API |
| `shopping_get_sku_latest_review_author` | 5 | Magento REST admin API |
| `shopping_get_sku_latest_review_rating` | 5 | Magento REST admin API |

`gitlab_get_project_memeber_role` is spelled with three `e`s. That is upstream's spelling,
the task JSON invokes it by that spelling, and a dispatch table keyed on the corrected
spelling breaks those 9 tasks with a `NameError` instead of scoring them 0.

The only API any evaluator calls is Magento's, from 15 tasks (436–440, 506–510, 585–589),
each minting a fresh token from `[accounts.shopping_site_admin]`. There is no GitLab API
usage anywhere. The shopping helpers `assert response.status_code == 200`, so an API hiccup
raises rather than scoring 0 — the same denominator problem as the judge.

## What cannot run here today

**The 128 map tasks are blocked.** They are exactly the tasks whose `start_url` is
`__MAP__`, and exactly the tasks with a null `storage_state`.

Nothing in this repo builds the OpenStreetMap **web frontend** that `__MAP__` addresses, and
the three map back ends do not substitute for it. The task file says so itself: the reference
URLs are `__MAP__/search?query=...`, and the locators read that frontend's DOM —
`div#content select.routing_engines`, `[name="route_from"]`, `#sidebar_content`. All 63 map
`program_html` targets are `last`, i.e. they score the frontend page as the agent left it.
`suite.toml` lists `webarena/map-frontend` as `[requires].not_built`: a service the tasks
need, and one this repo has no image directory for.

This was two gaps, and the other is closed. `webarena/map-tile`, `webarena/map-osrm` and
`webarena/map-nominatim` are now in `deploy/compose.yml`, behind compose's `map` profile
because they are ~35 GB and nothing can reach them yet; `deploy/Caddyfile` gives each of
their listeners a subdomain. Deploying them moved no count in `suite.toml` — which is the
point worth keeping: the back ends were never what blocked these tasks.

**`__HOMEPAGE__` is not a third gap.** It is not a service at all: the token occurs zero
times in the task file, nothing here serves it, and nothing is meant to. It matters only
because `env_config.py` asserts the `HOMEPAGE` env var is non-empty, so a harness must
export something for it — upstream's own `setup_env.sh` exports the literal string `PASS`.
`suite.toml` declares it in `[substitution].required_env` and deliberately not in
`[requires].services`.

That leaves **684 of 812 tasks** runnable against what `deploy/compose.yml` can bring up, and
they are exactly the 684 that run logged in.

One further caveat has no task-level marking anywhere in this file: search results with tied
relevance permute across a rebuild, fleet-wide, so any task whose expected answer depends on
the relative order of equally-ranked results is unstable. It is measured and recorded in
`TODO.md` and `images/webarena/reddit/README.md`, and how to score it is open — as
`[[open_decisions]] fleet-search-tie-ordering`, the one question here that is fleet-wide and
so carries the same id and the same options in every suite it affects.

## Licence

Apache-2.0. Every file under `upstream/` and `test/tasks.raw.json` is byte-identical to
upstream, which keeps §4(b)'s mark-your-changes obligation trivially satisfied — do not
reformat one in place. `LICENSE.upstream` is upstream's copy; its copyright line is the
unfilled template `Copyright [yyyy] [name of copyright owner]` and there is no NOTICE file
upstream, so attribution travels through `CITATION.cff`: Zhou, Xu, Zhu, Zhou, Lo, Sridhar,
Cheng, Bisk, Fried, Alon et al., *WebArena: A Realistic Web Environment for Building
Autonomous Agents*, arXiv:2307.13854.

The vendored Python is here to be read and reimplemented against, not imported blind.
