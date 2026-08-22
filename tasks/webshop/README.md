# WebShop task capture

WebShop is the one benchmark in this repo whose task set has no task file. There is no
per-task JSON record, no per-task URL, no start state to template and nothing to
substitute. A task is the pair **(goal index, running server)** — and the goal itself is
computed at server boot out of the product corpus.

So `human/suite.toml` is not an index over a task set. It **is** the capture: everything
a harness needs to run and score one instance, that exists in no other form.

## Layout

```
tasks/webshop/
  README.md                                     this file
  LICENSE.upstream                              upstream LICENSE.md, MIT
  human/suite.toml                              the manifest
  human/human_goals.json                        12,087 goal texts, in the server's order
  upstream/web_agent_site/engine/goal.py        the reward, byte-identical
  upstream/web_agent_site/engine/normalize.py   normalize_color, byte-identical
  upstream/tests/test_goal.py                   the reward oracle, byte-identical
```

One suite directory, `human/`, holding the full 12,087-goal human set. The synthetic
goal set is a different set reachable from the same code and is not captured — see
[Open decisions](#open-decisions).

## Why there is no task file

Three properties of the server, in the order they bite:

**The goal list is generated, not enumerated.** `get_human_goals` walks the ~1.18M
product corpus, takes each product's crowd-sourced instructions, drops the ones with an
empty attribute list, and emits a goal record carrying the target `asin`, the
`attributes` and `goal_options` checklists, a `price_upper` ceiling and the corpus's
`query`, `name`, `category` and `product_category` fields. None of that is written to
disk. It lives in the server process.

**The instruction text is not stable across boots.** The rendered text is
`product['instruction'].strip('.')` plus `', and price lower than {price_upper:.2f}
dollars'`, and `price_upper` comes from `random.sample(price_range, 2)`. There is no
`random.seed` call anywhere in `goal.py`; the seed the app does set fixes only the
shuffle that decides *which* goal an index names. `/fixed_42` therefore returns the same
product and the same base sentence on every boot of the same image, and a different price
clause. That is why the manifest records `instruction_source = "server_rendered"`, and
why a capture that snapshotted 500 instruction strings would report spurious mismatches
against a live server forever after.

**A task is addressed by index, in the session id.** `app.py` selects a fixed goal with:

```python
if session_id not in user_sessions and 'fixed' in session_id:
    goal_dix = int(session_id.split('_')[-1])
    goal = goals[goal_dix]
```

Goal *i* is `GET /fixed_<i>`. Any id *not* containing `fixed` draws a random
weight-sampled goal instead — which is what the image's healthcheck path `/abc` does.

## What `human_goals.json` is, and is not

It is the 12,087 goal texts in the server's post-shuffle order — the list upstream's own
split code indexes into to decide which split a goal belongs to.

Use it to **verify** that a live `/fixed_<i>` is the goal you think it is. Never to
**reconstruct** a goal offline. Measured against the vendored bytes:

| measurement | value |
|---|---|
| entries | 12,087 |
| distinct entries | 11,563 |
| texts appearing more than once | 517, covering 1,041 entries |
| of the first 500 (the test split), entries whose text repeats elsewhere | 41 |
| entries containing a price clause | 0 |
| entries containing an ASCII quote (`'` or `"`) | 0 |
| entries containing a typographic quote | 59 — U+2019 in 44, U+201C in 6, U+201D in 16 |
| entries containing a bare `&` | 199 |
| entries with any uppercase | 0 |
| entries still ending in a period | 11 |

Text → `(asin, attributes, goal_options)` is many-to-one and cannot be inverted, and the
file carries none of the fields the reward actually reads. What makes it a *stable*
identity is precisely what it leaves out: the price clause, the half of the instruction
that moves between boots.

Compare normalised, not for equality — lowercase, strip the
`", and price lower than N dollars"` suffix and strip trailing periods on both sides. The
11 entries that still end in a period do not survive a naive `==` against a server that
runs its base text through `.strip('.')`.

Punctuation needs the same symmetry. There is no ASCII quote in this file to strip, 59
entries carry a typographic one and 199 carry a bare `&` — so strip or unescape those on
both sides, or on neither. Doing it to one side only breaks exactly the entries that
carry them.

## Running and scoring one instance

Everything below is one container and one HTTP client. There is no login, no cookie jar,
no storage state and no reset endpoint.

**1. Bring the service up.**

```
BENCH_HOST=<addr> docker compose -f deploy/compose.yml up -d --wait webshop
```

It is published at `http://<BENCH_HOST>:${WEBSHOP_PORT:-3000}/`.

**2. Warm it.** The goal list and the search index are loaded lazily inside the first
request. Issue one request and let the 180 s health-check start period elapse before
dispatching agents in parallel.

**3. Open the task.** `GET /fixed_<i>`, with *i* the zero-based index into
`human_goals.json`. The rendered instruction is the goal.

**4. Verify, if you want the check.** Normalise the rendered instruction as above and
compare it to `human_goals.json[i]`. A mismatch means the server is serving a different
goal list — the 1000-product sample instead of the full corpus, or the synthetic set
instead of the human set — not that the price drifted.

**5. Drive the agent.** Search, open an item, choose options, click **Buy Now**. That
lands on `/done/<session_id>/<asin>/<options>`, which is where the server computes the
reward.

**6. Read the score off the done page.** `#reward pre` holds the number; `#reward_info
pre` holds the sub-score dict. **Read the DOM, not the rendered text** — `reward_info`
sits inside an `<h4 hidden>` and every goal field sits inside a `style="display:none"`
div, so a visible-text observation loses all of it. Log the goal block too: it is the
only record of what the server actually asked for on this boot.

Success is `reward == 1.0`.

**7. Retry on a fresh session, never on the same id.** `fixed` is matched as a
*substring*, and `user_sessions` is keyed by the raw session id — revisiting an id
already in it reuses the stored goal rather than re-rolling, so a second pass at
`/fixed_0` is driving a session that is already flagged done. `attempt2fixed_0` is a
clean session on the same goal, and it is the only way to retry without restarting the
container.

Restarting is not an equivalent reset. It rebuilds the goal list, which re-runs the
unseeded price draws, so every goal's price clause and every `r_price` outcome changes.
Only the shuffle survives.

## How the reward works

The server returns a continuous reward in `[0.0, 1.0]`, from `goal.py`:

```
total_reward = (num_attr_matches + num_option_matches + r_price)
             / (len(attributes) + len(goal_options) + 1)
total_reward *= r_type
```

The numerator is raw **match counts**. The normalised `r_att` and `r_option` in the
verbose info dict are separate quantities and cannot be recombined into the total.
`r_price` is the boolean `price <= price_upper`, summed as 1 or 0. `r_type` is a
multiplier over the whole fraction taking only `{1.0, 0.5, 0.1, 0.0}`.

`suite.toml` carries each component in full — the `> 85` `token_set_ratio` threshold, the
containment fallback into Title/BulletPoints/Description, the U+203A breadcrumb split,
the spaCy noun-overlap `title_score` and the exact cascade that turns it into `r_type` —
along with the four `get_reward` cases from the vendored `test_goal.py` restated as
arithmetic, so a reimplementation can be checked against the manifest alone.

Those four cases carry a caveat the manifest states beside them: each builds
`goal['goal_options']` as a **dict**, which is the *synthetic* goal shape, so they run
`get_reward`'s `.items()` branch rather than the list branch every goal in this suite
takes. Reproducing all four leaves the list branch untested.

### The oracle is vendored to be read, not imported

`goal.py`, `normalize.py` and `test_goal.py` are in `upstream/` byte-identical, and they
are there to be **read and reimplemented against**. Importing `goal.py` pulls in spaCy
with `en_core_web_sm` and thefuzz at module scope; *calling* `get_reward` pulls in the
5.28 GiB product corpus on top, because the reward reads the **purchased** product's
`Attributes`, `Title`, `BulletPoints`, `Description`, `query`, `product_category` and
`name` — plus the server-side randomised price, which exists only inside the process that
drew it.

Scraping the done page costs none of that, and the running image already computes the
same number. That is the recommended path; offline scoring is the expensive one.

## Nothing here is unrunnable

`[requires]` names one service, `webshop/server`. `images/webshop/server/` builds and
publishes it and `deploy/compose.yml` brings it up, so `not_built` and `not_deployed` are
both empty, and the suite needs no second site, no map backend, no auth service and no
network at run time.

There is also no `[accounts]` table, because WebShop has no logins: session state is an
in-process dict keyed by the session-id path segment, with no persistence layer and
nothing to authenticate as.

## What is pinned rather than vendored

The goals the server serves are built from `items_human_ins.json`, which is **already
pinned** at `images/webshop/server/image.toml`. `[goal_source]` in the manifest names it
by filename and sha256 so the two can be checked against each other, and deliberately
does not restate the mirror URLs: a pin has exactly one source of truth, and a second
copy is a silent-wrong-data hole rather than redundancy.

The 5.1 GiB `items_shuffle.json` and 178 MiB `items_ins_v2.json` are pinned the same way
and are needed only to build the image or to score offline.

## Not covered by a checksum

The manifest states several facts that come from upstream files at the pinned commit
whose bytes are **not** in this repo — the `/fixed_<i>` addressing, the seeded shuffle,
the unseeded price generation, the done-page selectors, and the two split definitions.
`[upstream].not_vendored` lists them explicitly, so a reader can tell which claims a
sha256 covers and which it does not.

One consequence is flagged in place. Because `done_page.html` is not vendored, no byte
here says which of its elements carry `hidden` or `display:none` — including whether
`#reward` is itself inside a hidden wrapper. Reading the DOM rather than the visible text
is correct whether or not it is, and it is the only rule that is; checking further means
reading that template at the pinned commit.

## Open decisions

`suite.toml` records four, and invents a default for none of them.

| id | the question | blast radius |
|---|---|---|
| `webshop-run-scope` | the published 500-goal test split, a stable in-house subset, or all 12,087 — and on which scale results are reported | 12,087 goals; the published-numbers option scores 500 |
| `webshop-price-nondeterminism` | accept and report the per-boot variance, or seed the two draws in the fork this repo already carries | every goal's instruction text and one of three numerator terms in every reward |
| `webshop-goal-set` | human goals only, or capture the synthetic set as a second suite | the meaning of every index, and the option-scoring path |
| `webshop-train-split` | train starts at 1500 (`env.py`) or at 500 (`train_search_il.py`) | 1,000 goals; test is unaffected either way |

The train-split contradiction is recorded as a contradiction. Both statements are
upstream at the pinned commit, and picking one would bury a decision in a data file.

The fleet-wide search-tie question is deliberately not among them. It asks how to score a
task whose expected answer depends on the order of equally-ranked results; `get_reward`
takes the purchased product, the goal record, the price and the purchased options, and no
ranking among them, so a permuted tie can change which product an agent buys but not what
a bought product scores.

## Upstream

[princeton-nlp/WebShop](https://github.com/princeton-nlp/WebShop) at
`64fa2a5c15c7daa698b9ac93f5bb5437b634c9bd`, MIT. The image is built from this repo's
fork, `shkolnik/WebShop` — see `images/webshop/server/README.md` — which changes the
corpus loading path and leaves the goal generation, the shuffle seed, the `fixed_<i>`
addressing and the reward untouched.

Yao, Chen, Yang, Narasimhan, *WebShop: Towards Scalable Real-World Web Interaction with
Grounded Language Agents*, arXiv:2207.01206.

⚠️ The upstream README's licence badge reads "License: Princeton" and links
copyright.princeton.edu, while `LICENSE.md` in the same commit is verbatim MIT. The file
governs. Separately, the MIT grant covers the code; the scraped Amazon catalogue is
distributed outside the repo and carries no licence statement of its own, which is why
this repo pins that corpus rather than re-hosting it.
