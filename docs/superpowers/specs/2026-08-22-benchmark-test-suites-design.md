# Benchmark test suites in this repo — design

This repo builds the benchmark *websites*. It records nothing about the *tasks* those
websites exist to serve: an exhaustive search for `task_id`, `intent`, `eval_type`,
`program_html`, `storage_state` or `reward` across every tracked file returns no benchmark
task material at all. `tests/` is the builder's own pytest suite. The only task-shaped
content anywhere is MiniWoB++'s task HTML, and it lives inside the image — the repo names
exactly one of those 130 pages, `click-button.html`, and only as a healthcheck probe.

The goal is to be able to spin up a test case from any service with one driver harness,
with everything needed to run it contained here. That is two passes:

1. **Capture** — get every task set, its scoring rules, and its provenance into the repo.
2. **Standardize** — one schema for task definitions and for results across all four.

This document specifies pass 1 and fixes the conventions pass 2 will tighten.

## What each benchmark actually ships

Verified against upstream at the commits recorded in each suite manifest, not from memory.

| Benchmark | Task set | Count | Form |
|---|---|---|---|
| WebArena | `config_files/test.raw.json` | 812 | enumerated JSON records, URL placeholders unsubstituted |
| VisualWebArena | `config_files/vwa/test_{classifieds,shopping,reddit}.raw.json` | 234 / 466 / 210 | same, plus per-task input images |
| WebShop | `baseline_models/data/human_goals.json` | 12,087 | goal strings; no per-task record exists |
| MiniWoB++ | `miniwob/registration.py` | 128 | env registrations; no task records at all |

The four disagree about what a "task" *is*, and the disagreement is not cosmetic:

- **WebArena and VWA** enumerate records carrying an intent, a start URL and an
  executable evaluator spec.
- **WebShop** generates its goals at server boot from the product corpus. There is no
  per-task file and no per-task URL — a task is `/fixed_<i>`, and `fixed` is matched as a
  *substring*, so `attempt2fixed_0` is a clean session on the same goal.
- **MiniWoB** has no instance records. A task is an HTML page plus a seed; `genProblem()`
  builds a fresh instance per episode.

A schema with a required `instruction` field would therefore be a lie for half the fleet.
Pass 2 resolves this with an `instruction_source` of `literal`, `server_rendered` or
`page_rendered`; pass 1 records which one each suite is.

## Layout

Discovery is by glob and there are no root index files, matching `images/*/*/image.toml`:

```
tasks/<benchmark>/<suite>/suite.toml       # first-party manifest
tasks/<benchmark>/<suite>/<task set>       # vendored upstream, byte-identical
tasks/<benchmark>/upstream/                # vendored scoring code, unmodified
tasks/<benchmark>/LICENSE.upstream
tasks/<benchmark>/README.md
```

Adding a suite is creating a directory; removing one is deleting it. The glob is
`tasks/*/*/suite.toml`.

VWA gets three suite directories rather than one file, because `task_id` restarts at 0 in
each and the directory is what disambiguates them.

## Vendor the raw form, never the generated form

Upstream WebArena `.gitignore`s all 812 generated per-task JSONs (`config_files*/*[0-9].json`).
The artifact it commits is the template, with `__SHOPPING__`-style tokens intact, and a
28-line generator that substitutes them from environment variables.

Vendor the template. Substituting at capture time would bake one deployment's hostnames into
git and destroy the relocatability the whole `deploy/` design exists to provide.

The substitution is a whole-file text replace performed *before* `json.loads`, and its order
is load-bearing: `__SHOPPING__` is replaced before `__SHOPPING_ADMIN__`, and that is only
safe because of the trailing `__`. Any prefix-matching regex corrupts all 300
`__SHOPPING_ADMIN__` occurrences into `<shopping-url>_ADMIN__`. A reimplementation must
reproduce the exact replace, not improve on it.

## The evaluator specs are code, not data

WebArena's `program_html` entries carry `func:`-prefixed strings that upstream passes to a
bare `eval()`, resolved against `evaluation_harness.helper_functions`'s module globals —
75 URL targets and 22 locator targets. VWA uses 14 such helpers, `get_query_text` in 143
places alone.

This is a name-resolution dependency, not a declarative spec. Upstream's helper set includes
the misspelling `gitlab_get_project_memeber_role`, and task JSON invokes it by that spelling,
so a dispatch table that "corrects" anything breaks tasks silently. Reimplementing the
helpers declaratively either preserves every upstream quirk exactly or loses 411 of
WebArena's 812 tasks and 409 of VWA's 910.

So pass 1 vendors `evaluators.py` and `helper_functions.py` byte-identical and unmodified.
Pass 2 may add a parsed `{fn, args}` view *alongside* the verbatim string, guarded by a test
asserting the parse round-trips over every target — but the verbatim string stays the source
of truth.

Two consequences follow, and both are stated rather than absorbed:

- This puts executable third-party Python in the repo for the first time. `SECURITY.md` says
  so, names what is vendored and from where, and states that it is vendored to be read and
  to be reimplemented against, not to be imported blind.
- Apache-2.0 and MIT both permit this with attribution. Every vendored file is unmodified,
  which keeps the "mark your changes" obligation trivially satisfied, and each benchmark
  directory carries the upstream LICENSE.

## Capture facts, not policy

Several questions genuinely have no answer yet, and inventing a default for them would bury
a decision in a data file:

- which judge model scores WebArena's 118 fuzzy-match tasks and VWA's VQA tasks;
- whether MiniWoB runs at its published 10-second deadline, which an LLM agent loses before
  its first click;
- whether the harness resets state mid-run, when only `vwa/classifieds` can be reset in
  place;
- how tied relevance ranks are scored, still open in `TODO.md`.

Each suite manifest records these as `[[open_decisions]]` entries naming the question, the
options and how many tasks the answer moves. A manifest states what upstream does; it does
not invent what this repo will do.

## Manifest skeleton

Every `suite.toml` carries at least these, and each may add what it needs. Every non-obvious
key carries a comment naming the failure mode it prevents, as the eleven `image.toml`
manifests do.

```toml
[suite]      # name, benchmark, task_count, one-line description
[upstream]   # repo, commit, license — the commit is pinned, never a branch
[[upstream.files]]  # per vendored blob: local path, upstream path, sha256, bytes
[tasks]      # which file holds the task set, its format, its id field, instruction_source
[sites]      # placeholder token -> repo service name (absent where a benchmark has none)
[requires]   # services the suite needs; which of them this repo does not build
[reset]      # kind (none | http_endpoint | container_recreate | page_episode) and cost
[scoring]    # evaluator kinds in use, what is offline-scorable, what needs a judge
[[open_decisions]]  # question, options, blast radius
```

## Repo-side changes the capture forces

**`[service].base_path` in `image.toml`.** A port number cannot reconstruct a base URL for
shopping-admin (`/admin`) or wikipedia (`/wikipedia_en_all_maxi_2022-05/A/`), and a task's
`start_url` has to be templated onto one. `load_manifest` reads named keys with `data.get`
and silently ignores unknown ones, so adding the key without a loader change means it is
silently dropped.

**The map services need a deploy path.** `map-tile`, `map-osrm` and `map-nominatim` are
published to GHCR and present in `images/webarena/compose.yml`, but absent from
`deploy/compose.yml`, `deploy/Caddyfile`, `deploy/README.md`'s port table, the root
README's image table and `docs/service-contract.md`'s address table. WebArena has 128
map-tagged tasks. Capturing the suite honestly means either wiring them up or declaring the
dependency unsatisfiable.

**Benchmark application logins are recorded here.** `SECURITY.md` previously excluded them
and pointed at upstream. That position does not survive the capture: 15 WebArena tasks score
by minting a Magento REST admin token from `shopping_site_admin`'s password, and the
evaluators cannot be imported at all without `browser_env/env_config.py`, which contains the
`ACCOUNTS` table verbatim. Vendoring the evaluator chain reverses the policy whether or not
anyone decides to; so it is reversed deliberately, in the same commit that amends
`SECURITY.md`. The credentials are public in the upstream repos and already baked into these
images.

## What is pinned rather than vendored

VWA's 346 per-task input images are 186,294,634 bytes and live in the upstream git tree
rather than a released archive. They are pinned as a `[[datasets]]` entry and fetched into
the existing gitignored `datasets/` directory, exactly as image data is. Two facts travel
with the pin because both are silent traps: the files are named `.png` but are JPEGs, and
their paths are relative to the upstream repo root, so the capture records the rewrite rule.

WebShop's `items_human_ins.json` is **already pinned** at
`images/webshop/server/image.toml`, byte-identical to upstream's copy. The suite manifest
references that pin by filename and sha256 rather than restating it. A pin has exactly one
source of truth; a second copy is a silent-wrong-data hole, not redundancy.

## Testing

`tests/test_task_suites.py`, in the hermetic suite, reading the real files rather than
fixtures — the invariant is about what this repo ships, and a fixture cannot go stale the way
the thing being guarded does. It asserts every vendored blob still matches the sha256 its
manifest records, every `suite.toml` parses and carries the skeleton above, every service in
a `[requires]` list is either an `images/*/*/` directory or declared unrunnable, and every
`[sites]` token appears in the task file it claims to template. With a vacuity guard on each
collection: a vacuous suite is indistinguishable from a passing one.

`tests.yml` runs on push to any branch with no path filter, so this is covered from the
first commit. `build.yml` triggers only on `main` and only under `images/`, `builder/`,
`bin/` and its own path, so a `tasks/` tree builds nothing.

## Out of scope for pass 1

No driver, no runner, no scorer. Pass 1 ends when every fact needed to run a task is in the
repo and tested. Pass 2 takes the four manifests, factors out the common schema, adds the
results schema, and only then is there something for a harness to consume.
