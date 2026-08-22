# MiniWoB++ task capture

```
tasks/miniwob/core/suite.toml        first-party manifest for the 128 registered envs
tasks/miniwob/core/tasks.jsonl       DERIVED task list, one record per registered env
tasks/miniwob/derive-tasks.py        generates it from upstream/ plus the pinned archive; --check diffs
tasks/miniwob/upstream/              vendored upstream metadata, unmodified
tasks/miniwob/LICENSE.upstream       MIT, three copyright holders
```

The site itself is not here. `images/miniwob/server` builds it from a sha256-pinned archive
and `deploy/` serves it at `/miniwob/...`; this directory holds only what the image does not
already contain.

## Two upstream refs, and they are not interchangeable

This is the single most confusing thing about MiniWoB in this repo, so it is stated first.

| | ref | what it is |
|---|---|---|
| the **site** | tag `v1.0` (`553daee55ea0b2cc32b181a474083ab4cad782a1`) | `images/miniwob/server/image.toml` pins the v1.0 tag archive; the image extracts `miniwob/html` out of it, so the bytes an agent drives are v1.0's |
| the **metadata** | `main` @ `33c3b4ddef8c6eb67c57a29663d844b1eda7e614` | everything under `upstream/` is vendored from this commit |

The vendoring ref is main because `registration.py` marks **25** environments
`nondeterministic=True` there and only **4** at v1.0 (`click-pie`, `click-pie-nodelay`,
`stock-market`, `terminal`). The other 21 environments behave identically at both refs, because the pages
themselves are byte-identical — main only records what those pages already do, naming the
causes (jQuery datepicker state persisting across resets, `twbsPagination`
leaking, `scrollTop()` not resetting, `setInterval` animation, `new Date()` in the DOM,
variable RNG consumption). Capturing v1.0's `registration.py` would silently assert that
`book-flight`, `choose-date*`, `social-media*`, `search-engine`, `phone-book`, `order-food`,
`scroll-text*`, `click-dialog*`, `click-scroll-list`, `drag-cube` and the three flight envs
are seed-reproducible.

Mixing the two refs is safe for one measured reason: **all 361 blobs under `miniwob/html/`
are byte-identical at v1.0 and at that main commit**, `core.js` included, compared blob by
blob over the whole subtree of both refs' source archives.

The vendored metadata does not agree quite that closely. The registered id set and every
`entry_point` match, and `miniwob_envs.py`, `flightwob_envs.py`, `constants.py` and the
licence file are byte-identical — but **three** vendored files differ, not two:

| file | v1.0 | vendored (main) | what differs |
|---|---|---|---|
| `registration.py` | 16,428 B | 18,738 B | the `nondeterministic=True` flags: 4 bare ones against 25, each carrying its reason as a trailing comment. Every differing line is one of those flags. |
| `reward.py` | 2,373 B | 2,400 B | one import line, `typing` → `collections.abc`; the four function bodies are identical |
| `selenium_instance.py` | 16,766 B | 17,316 B | a docstring typo (`ChromDriver` → `ChromeDriver`), PEP 604 annotations (`Optional[X]` → `X \| None`, `Dict`/`List`/`Tuple` → the builtins), and `create_driver()` |

`create_driver()` is the one to read before reimplementing anything against this file. At the
vendored commit it honours a `MINIWOB_CHROME_BINARY`/`MINIWOB_CHROMEDRIVER` pair, raising
`ValueError` when exactly one of the two is set, and it no longer passes `window-size` to
Chrome — so nothing sizes the browser window and only the 160x210 crop still reaches the
observation. Both versions read `window.innerWidth`/`innerHeight` back from the page after
load.

Whether the image's pin should move to main is recorded as an open decision, not answered
here. It would change no served byte.

## Why this suite needs a derived task file

The other three benchmarks in this repo ship a task set that can be vendored byte-identical.
MiniWoB ships none. There is no JSON, YAML or TOML manifest anywhere upstream; the task list
exists **only** as 128 `register()` calls inside `upstream/miniwob/registration.py`, and the
per-task metadata exists only as structured docstrings on 128 Python classes.

Worse, the three obvious ways to enumerate the suite disagree, and the sets are not nested in
either direction:

| | count |
|---|---|
| registered envs (`registration.py`) | **128** |
| `.html` files served under `/miniwob/` | 130 |
| of those, served but deliberately unregistered | 5 |
| registered envs with no file under `/miniwob/` | 3 |
| flight sites served | 4 |
| flight envs registered | 3 |

The 5 unregistered pages are `button-delay`, `chase-circle`, `hover-shape`, `moving-items`
and `simon-says`; upstream's `docs/_scripts/gen_env_list.py` lists them under *Excluded
Tasks* because they require waiting for a timed event and "a 'no-delay' version is impossible
to make". The 3 registered envs with no page there are `flight.AA`, `flight.Alaska` and
`flight.Alaska-auto`, which live at `/flight/<Airline>/wrapper.html`. A fourth flight site,
`/flight/Alaska-auto-medium/wrapper.html`, returns 200 and has no gym env at all.

So enumerating the directory — or nginx's autoindex, which this image has on — yields 130 and
is wrong twice. `tasks.jsonl` exists to be the one list that is right: 128 records,
`125 + 3`, each carrying the URL path that actually resolves.

## What generates `tasks.jsonl`

`derive-tasks.py` builds it — derived, not vendored — from exactly these inputs:

- `upstream/miniwob/registration.py` — `env_id`, `entry_point`, `nondeterministic` and its
  reason, and `section` (from the file's own two comment markers: main 107, test 18,
  flightwob 3)
- `upstream/miniwob/envs/miniwob_envs.py` — `subdomain`, description, utterance fields,
  additional notes (125 classes, 20 with a `**Partial reward:**` note, 5 asking for
  `get_thresholded_reward`)
- `upstream/miniwob/envs/flightwob_envs.py` — the same for the 3 flight envs
- the sha256-pinned v1.0 archive named in `images/miniwob/server/image.toml` — the only
  source of `episode_max_time_ms`, because no vendored `.py` carries a deadline, and of
  `served_by_image`, which is the assertion that every registered id has a page in the tree
  the image ships

Running it needs that archive, and `datasets/` is gitignored, so fetch it first with
`bin/build download miniwob/server --datasets-dir datasets`, which is the fetch the script
names in its own error when the file is absent. It sha256s the archive against the pin in
`images/miniwob/server/image.toml` and refuses anything that does not match, so a deadline
can only ever come from the bytes the image serves. `--check` rebuilds
the records in memory, diffs them against the committed file and exits 1 on any difference,
writing nothing; without it the file is rewritten in place. No workflow runs either mode:
neither `.github/workflows/build.yml` nor `tests.yml` names this script, and `tests.yml`
only invokes pytest over `tests/`. So `--check` runs when a person runs it.

`constants.py` and `selenium_instance.py` are vendored alongside those, for the viewport
geometry and for the reset sequence, URL composition and score readback respectively. They
are there to be read and reimplemented against, not imported — which is what the generator
does with the geometry: it carries `constants.py`'s four pairs as its own `VIEWPORT` and
`FLIGHT_VIEWPORT` literals rather than importing them.

One capture gap is worth naming: upstream's six-way categorisation lives in
`docs/_scripts/gen_env_list.py`, which is **not** vendored — its counts are transcribed into
`suite.toml`'s `[tasks.categories]`, where they sum to 128 and agree with the three-way
`section` split (107 = 77 + 6 + 12 + 12). Nothing in `tasks.jsonl` depends on it.

`suite.toml`'s `[tasks.fields]` names the source of every field one line at a time. A field
with no line there is a field the generator invented. `tests/test_task_suites.py` asserts
that the `env_id` set in `tasks.jsonl` equals the registered id set in the vendored registry;
it deliberately does not check the per-page deadlines, because those live in the pinned
archive rather than in git, and `derive-tasks.py --check` covers them where the archive is
present.

## What a harness must do per episode

A MiniWoB task is not a record. It is a page plus a seed: `genProblem()` builds a fresh
instance every episode, and the instruction is written into `#query` at that moment. The
instance key is `(env_id, seed, data_mode)` and two thirds of it is set by JS after page load.

**The two families of task do not share a base URL.** Against this image the paths are
`/miniwob/<name>.html` for 125 envs and `/flight/<Airline>/wrapper.html` for 3, and
`suite.toml` records them that way because it is unambiguous. Upstream composes them instead
as `urljoin(base_url, subdomain + ".html")` and
`urljoin(base_url, subdomain.replace(".", "/") + "/wrapper.html")` — and feeds those two
calls different roots, `<html>/miniwob/` for the first and `<html>/` for the second. Passing
one base for both yields `/miniwob/flight/Alaska/wrapper.html` or a bare
`/click-button.html`, and both 404 here. Both bases need their trailing slash, because
`urljoin` drops the last segment of a base that lacks one.

**Navigating to the URL does not start the task.** `window.onload` calls
`core.startEpisode()`, which raises a full-page `div#sync-task-cover` reading `START` and
waits. A harness that does `goto()` and starts clicking is driving a page whose problem has
not been generated and whose timer has not started.

The sequence, in this order:

1. `core.endEpisode(0)` — force stop, back to the sync screen
2. *optionally* `driver.get(url)` — the only thing that clears carried-over tab state
3. `Math.seedrandom(<seed>)` — reseeds the page's **global** `Math.random`
4. `core.setDataMode("train"|"test")`
5. `core.startEpisodeReal()`
6. poll `return WOB_TASK_READY;` — up to 20 attempts at 50 ms

Order is load-bearing in three places. Step 1 terminates a still-running episode, and it has
to happen before step 5: `startEpisodeReal()` called over a live episode clears that
episode's timer itself and generates a new problem on top of it, so the previous result is
gone before anything reads it. The seed must land before step 5 too, because `genProblem()`
consumes `Math.random()` when that call runs — there is no `?seed=` parameter anywhere, and
anything else that touches `Math.random()` in between shifts the stream. And the ready poll
matters for the 3 flight envs and only for them: `flight-common/wrapper.js` holds
`WOB_TASK_READY` false until its child iframe loads, and no file under `html/miniwob/` ever
touches the flag.

Read the score with exactly this, before starting the next episode:

```js
return {"done": WOB_DONE_GLOBAL, "env_reward": WOB_REWARD_GLOBAL,
        "raw_reward": WOB_RAW_REWARD_GLOBAL, "reason": WOB_REWARD_REASON,};
```

`core.startEpisodeReal()` resets all four to `0/0/false/null`, and only the first terminal
event of an episode ever scores: `core.endEpisode` returns immediately when `core.EP_TIMER`
is already null, and its last statement re-raises the START cover.

The instruction is read separately, via `core.getUtterance()` — `#query`'s `textContent`,
whitespace-collapsed. `core.getDOMInfo()` skips `#query`, `#reward-display`,
`#sync-task-cover` and `#click-canvas` outright, along with any zero-area element, so a
harness that scrapes the observation tree will not find the instruction in it. The observation
geometry is a 160x210 task area inside a 500x320 window (375x667 inside 600x800 for flight);
a harness at 1280x720 is not observing what the benchmark defines.

## The gap: MiniWoB as published cannot be run by an LLM agent

`[requires].not_built` and `[requires].not_deployed` are both empty and that is not a
mistake — `miniwob/server` is built, published, and wired into `deploy/compose.yml` as the
`miniwob` service, into `deploy/Caddyfile` and into `deploy/README.md`'s port table. There
is no service gap here. The gap is the clock.

`core.startEpisodeReal()` arms `setTimeout(() => core.endEpisode(-1, false, 'timed out'),
core.EPISODE_MAX_TIME)`. Measured across the 130 pages under `/miniwob/`, plus the four
flight wrappers:

| `EPISODE_MAX_TIME` | pages |
|---|---|
| 7000 | 2 |
| 10000 — the `core.js` default, no assignment of their own | 72 |
| 10000 — assigned explicitly | 8 |
| 15000 | 16 |
| 20000 | 18 |
| 30000 | 14 |
| **130 under `/miniwob/`** | |
| 60000 — the four flight wrappers, 3 of them registered envs | 4 |

Eighty of the 130 pages give an agent exactly ten seconds, 82 give ten or fewer, and 116
expire before thirty — so an agent that spends half a minute deciding has scored -1 on 116 of
them before its first click. On expiry the page looks entirely normal:
`WOB_DONE_GLOBAL` flips true, the reward is -1, and there is no exception, no console warning
and no signal of any kind that the deadline is what happened.

The reward is time-decayed on top of that. `core.js` computes
`reward * Math.max(0, 1 - dt/EPISODE_MAX_TIME)` whenever `time_proportional` is truthy. Of
the 295 `endEpisode` call sites in the 130 pages, 94 pass a literal `true` (88 of them with
full credit) and 92 more pass a success-conditional expression like `r > 0`, which is true on
exactly the successful terminations; `reward.py`'s own docstring states the consequence — "In
all environments, any positive reward is scaled by the remaining time." A correct answer at
t=9s on a 10s task yields `env_reward` 0.1 and `raw_reward` 1.0, so **`env_reward` is partly a
measurement of the agent's latency** and reading it by mistake makes a working agent look
broken. The 3 flight envs are the exception: their wrapper passes `time_proportional = false`.

Both obvious fixes change the numbers. Raising `core.EPISODE_MAX_TIME` before
`startEpisodeReal()` also raises the time-decay denominator; clearing `core.EP_TIMER` removes
the timeout but leaves the decay running against a deadline that no longer terminates
anything. Which one this repo does, and whether `env_reward` is reportable at all, are
`[[open_decisions]]` in `core/suite.toml` — along with the reward processor, the seed policy,
the treatment of the 25 nondeterministic envs, the data mode, the viewport, the dataset pin
ref, and the six unregistered pages.

## Scoring needs no judge, and there is no answer key to score against

Every one of the 128 envs scores itself in the browser: the task HTML calls
`core.endEpisode(reward, time_proportional, reason)` from its own handlers and `core.js`
turns that into the four globals above. Nothing external is consulted and no model is in the
loop, which is `suite.toml`'s `needs_judge = 0`. The same fact from the other side is
`offline_scorable = 0` and `needs_live_page = 128`: the score exists only in the tab the
episode ran in, there is no answer file anywhere to compare a recorded answer against, and
reading it is a JS call into that live page — after the episode terminates and before the
next `startEpisodeReal()` zeroes the globals. The one genuinely different scorer is
FlightWoB's: `flight-common/wrapper.js` overrides `core.endEpisode` and scores via
`core.validateForm` — `-1` if a required field is unfilled or the agent navigates the inner
frame away, otherwise the fraction of requested key-value pairs satisfied.

What remains a policy choice is the binarisation, which is why `upstream/miniwob/reward.py`
is vendored: `get_original_reward`, `get_raw_reward`, `get_binary_reward` and
`get_thresholded_reward`. Published MiniWoB numbers overwhelmingly use the binary one,
`raw_reward == 1.0`.

## No accounts, no reset cost, no server state

There is no login, no cookie, no `storage_state` and no seeded database — the server is
static nginx and the entire benchmark state lives in one browser tab. That is why this
benchmark's manifest has no `[accounts]` table where WebArena's has one, and why `[reset]`
costs effectively nothing. The corollary is that a reset also clears nothing outside the tab:
between episodes the tab keeps jQuery-UI widget state, scroll positions, focus and
`setInterval` timers, which is exactly what the 25 `nondeterministic=True` flags document.
Only a full `driver.get()` clears them, and it must be followed by re-seeding.

## Relative asset paths pin the serving layout

Every task page loads `../core/core.js` and `../common/ui_utils.js` by relative path, so a
page must be served at `<root>/miniwob/<name>.html` with `core/` and `common/` as siblings of
`miniwob/`. Served flat, or at a different depth, the page renders and has no `core` object:
`genProblem` never runs and nothing ever scores. The image's Dockerfile is built precisely
around this (`--strip-components=3` over the archive's `miniwob/html` subtree), and the
healthcheck probes a real task page rather than `/` for the same reason.
