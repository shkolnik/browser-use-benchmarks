# VisualWebArena task suites

910 tasks in three suites — `classifieds/` 234, `shopping/` 466, `reddit/` 210 — captured
from `web-arena-x/visualwebarena` at commit
`89f5af29305c3d1e9f97ce4421462060a70c9a03`, MIT. Every vendored file here is
byte-identical to that tree; the sha256 and byte count of each one is recorded in the
`suite.toml` that ships beside it.

```
tasks/vwa/<suite>/suite.toml        first-party manifest
tasks/vwa/<suite>/tasks.raw.json    upstream config_files/vwa/test_<suite>.raw.json
tasks/vwa/upstream/                 the scoring code, unmodified
tasks/vwa/LICENSE.upstream          upstream LICENSE
```

Three suite directories rather than one, because **`task_id` restarts at 0 in each file**
(classifieds 0-233, shopping 0-465, reddit 0-209). Every id in 0-209 names three different
tasks, so the directory is the disambiguator and a capture keyed on `task_id` alone merges
the three silently.

## What VWA adds over WebArena

VWA's harness is a fork of WebArena's — the record shape carries over and VWA adds the
visual half. The two `evaluators.py` this repo vendors are *not* the same file. Measured
against `../webarena/test/tasks.raw.json`:

| | WebArena | VisualWebArena |
|---|---|---|
| tasks | 812 | 910 (234 + 466 + 210) |
| records with an `image` field | 0 | 601 (321 of them carrying 346 real references) |
| `page_image_query` evaluator | absent | 42 tasks |
| difficulty labels | none | `reasoning_`, `visual_` and `overall_difficulty` on all 910 |
| `viewport_size` overrides | none | 69 tasks |
| `string_note` | top-level | inside `eval`, where `StringEvaluator` reads it |

`evaluator_router` routes exactly four `eval_types`: `string_match` to `StringEvaluator`,
`url_match` to `URLExactEvaluator`, `program_html` to `HTMLContentExactEvaluator`,
`page_image_query` to `PageImageEvaluator`. Anything else raises. A fifth class,
`StringSoftEvaluator`, exists and no VWA task reaches it — but it is why `evaluators.py`
imports the `evaluate` package at module top. `EvaluatorComb` **multiplies** the scores it
routes to, so a task listing two `eval_types` needs both to return 1.0; they are never
alternatives.

## The three suites

| | classifieds | shopping | reddit |
|---|---|---|---|
| tasks | 234 | 466 | 210 |
| tasks with input images / references | 68 / 68 | 169 / 189 | 84 / 89 |
| image bytes | 15,418,114 | 91,359,300 | 79,372,113 |
| `program_html` / `string_match` / `url_match` / `page_image_query` | 31 / 78 / 131 / 2 | 293 / 132 / 42 / 12 | 85 / 70 / 51 / 28 |
| needs a vision model (`eval_vqa`) | 0 | 12 | 10 |
| needs image SSIM (`eval_fuzzy_image_match`) | 2 | 0 | 18 |
| needs an LLM judge | 10 | 31 | 8 |
| scorable from the final answer alone | 62 | 98 | 59 |
| scorable with no live page (`offline_scorable`) | 72 | 129 | 64 |
| requires a login | 234 | 346 | 81 |
| declares `require_reset` | 22 | 19 | 21 |
| runnable in this repo today | 221 | 0 | 0 |

## Dependencies come from the tokens, not from `sites`

Start URLs and reference URLs carry `__CLASSIFIEDS__`, `__SHOPPING__`, `__REDDIT__` and
`__WIKIPEDIA__` placeholders that upstream substitutes with a whole-file `str.replace` over
the file text before `json.loads`. `__HOMEPAGE__` is in the replace map and occurs **0
times** in all three files.

The environment is a wider requirement than the tokens, and every manifest records it as
`[requires].required_env`. `upstream/browser_env/env_config.py` needs **seven** variables
before the evaluator chain can be imported at all, and they fail in two different ways:

- `DATASET` is read as `os.environ["DATASET"]` at module top, before anything else, so an
  unset value is a `KeyError` rather than the `AssertionError` the other six raise; a value
  that is neither `webarena` nor `visualwebarena` is a `ValueError`, and only
  `visualwebarena` takes the branch that defines `CLASSIFIEDS` at all.
- `REDDIT`, `SHOPPING`, `WIKIPEDIA`, `HOMEPAGE`, `CLASSIFIEDS` and `CLASSIFIEDS_RESET_TOKEN`
  are read with `os.environ.get(name, "")` and asserted non-empty in one `and` chain, so any
  one of them missing is an `AssertionError` that names all six.

A single-site deployment satisfies all seven or imports nothing: a shopping-only run still
exports `CLASSIFIEDS` and `CLASSIFIEDS_RESET_TOKEN`, a classifieds-only run still exports
`REDDIT`, `SHOPPING` and `WIKIPEDIA`, and all three export `HOMEPAGE`, which no task
references. `REDDIT_RESET_URL` is read the same way and is **not** asserted, so it silently
stays empty.

The `sites` field on each record is not a reliable statement of what a task needs. Measured
disagreements between `sites` and the tokens actually present:

- **classifieds** — 13 tasks reference a foreign site and 11 of them declare
  `sites = ["classifieds"]` alone (8 shopping, 3 reddit).
- **shopping** — task 319 declares `["shopping", "wikipedia"]` and contains no
  `__WIKIPEDIA__` token at all.
- **reddit** — task 64 declares `["reddit"]` while carrying `__WIKIPEDIA__`.

Compute dependencies from the tokens in the whole record. One caveat in the other
direction: reddit task 64's only `__WIKIPEDIA__` reference is inside `comments`, an
annotator note no evaluator reads, which is why 33 reddit tasks contain the token and only
32 actually need the service.

## What runs here today, and what does not

`images/vwa/` contains exactly one service directory: `classifieds`. **`vwa/shopping` and
`vwa/reddit` are not built** — no Dockerfile, no dataset pin, no compose entry.

| | tasks | runnable | blocked on a service not built here | also needs a service that is built here |
|---|---|---|---|---|
| classifieds | 234 | 221 | `vwa/shopping` (10 tasks), `vwa/reddit` (3 tasks) | — |
| shopping | 466 | 0 | `vwa/shopping` (all 466) | `webarena/wikipedia` (2 tasks) |
| reddit | 210 | 0 | `vwa/reddit` (all 210), `vwa/shopping` (11 tasks) | `webarena/wikipedia` (32 tasks) |

**221 of 910 tasks, 24%.** The 13 blocked classifieds tasks all open the foreign site in
`start_url`, so they cannot even begin; tasks 36 and 37 additionally hold a `__SHOPPING__`
URL in their reference answer, so they cannot be scored either.

`__WIKIPEDIA__` is the one foreign token that resolves to a service this repo does build.
The task URLs address the ZIM by name — `/wikipedia_en_all_maxi_2022-05/A/<Title>` — which
is what `images/webarena/wikipedia` serves, on published port 9888 rather than upstream's
8888.

Whether `webarena/shopping` and `webarena/reddit` can stand in for the missing VWA services
is left open in each manifest rather than assumed. The evidence for it is real: this repo
builds them from `shopping_final_0712.tar` and `postmill-populated-exposed-withimg.tar`,
the same artifacts VWA names, and the reddit image's media archive does carry a
`submission_images` tree of the kind reddit's SSIM references address. Nothing here
verifies that the specific item pages and images these tasks name exist in them.

## Scoring is not self-contained

Only the `string_match`-only tasks — 265 of 910, of which 219 need no judge — can be
re-scored from a saved run. The other 645 call into a live Playwright page: `url_match`
reads `page.url`, `program_html` navigates and queries the DOM, `page_image_query`
downloads what the page renders. 117 `program_html` entries across 107 tasks go further and
mint a Magento admin REST token from `shopping_site_admin`'s password before they can name
a page.

Four things scoring reaches for beyond the site under test:

1. **9 live `images.pexels.com` URLs** are fetched as SSIM references for reddit tasks 27,
   29, 111, 112 and 113, and a tenth for classifieds task 8. Scoring needs the public
   internet, and link rot or a silent re-encode upstream changes results with no signal.
2. **`coco_images/000000515982.jpg`**, a repo-relative file, is reddit task 28's reference.
   It is one of 45 files in upstream's `coco_images/`; the other 44 are referenced by
   nothing. Its licence is MS-COCO's, not upstream's MIT.
3. **13 `__SHOPPING__` product-image URLs** are SSIM references on 9 reddit tasks, so
   reddit cannot be scored with only a reddit container.
4. **Two models.** `llm_fuzzy_match` and `llm_ua_match` pin `gpt-4-1106-preview` at
   temperature 0; `image_utils.get_captioning_fn` raises `NotImplementedError` for any
   model name without `blip2` in it, so the 22 `eval_vqa` tasks are pinned to
   `Salesforce/blip2-flan-t5-xl`.

Each of the four is an `[[open_decisions]]` entry, in the manifest that carries the tasks it
moves, with options and no default:

1. `vwa-reddit-fuzzy-image-reference` and `vwa-classifieds-fuzzy-image-reference` — fetch
   live as upstream does, or snapshot and pin.
2. `vwa-reddit-coco-reference` — take it out of the pinned tarball, snapshot that one file,
   or leave the reference unresolved and the task unpassable.
3. `vwa-reddit-service-unavailable`, and `vwa-cross-site-substitution` in the classifieds
   manifest — build `vwa/shopping`, point `__SHOPPING__` at the `webarena/shopping` this
   repo already builds, or leave the tasks unrunnable.
4. `vwa-shopping-vqa-model`, `vwa-reddit-vqa-model` and the three `*-judge-model` entries.

Only the fourth is the reproduce-the-published-numbers versus stable-in-house pair; the
other three trade a live third-party dependency against a frozen copy of it.

## The input images

346 files, 186,149,527 bytes, fetched into the gitignored `datasets/` directory rather than
committed. The pinnable object is the whole upstream repository tarball, because the images
live in the git tree and not in a release asset. Fetching that URL repeatedly returns the
identical sha256 and the identical 206,490,478 bytes; GitHub does not contract to make
on-demand commit tarballs byte-reproducible, so a regenerated archive would fail the
checksum loudly rather than substitute a different tree.

The pin lives in **`datasets.toml`, once**, and each `suite.toml` points at it with
`[tasks.images].datasets_file` and adds only what differs per site: its `member_prefix`
inside the archive, its file count, its byte total and its format mix. Three copies of one
sha256 would be three answers to one question with nothing comparing them — the
silent-wrong-data hole `docs/design.md` describes.

**`tasks/vwa/fetch-images.py` is what fetches it, not `bin/build`.** `builder/discover.py`
globs `images/*/*/image.toml`, so a dataset pinned under `tasks/` is structurally invisible
to the build; the script beside the pin verifies the sha256 and unpacks what the tasks
address. It takes two whole trees rather than a file list — the 346 input images and all 45
files of upstream's `coco_images/`, 391 files and 193,716,928 bytes on disk — because
reddit task 28's SSIM reference lives in the second tree, outside every site's
`member_prefix`, and it is the only one of those 45 files any task names.

Two traps travel with the pin:

- **The paths are relative to the upstream repo root**
  (`environment_docker/webarena-homepage/static/input_images/<site>/task_<id>/input_<n>.png`)
  and upstream opens them with a bare `Image.open()`. Run from any other directory and 321
  tasks silently become text-only; they still score, at 0, with nothing naming the image.
- **The extension is not the format.** All 346 files are named `.png`. 301 are JPEG, 23 are
  PNG, 20 are WebP and 2 are GIF. Dispatching on the extension, or declaring `image/png` on
  an API upload, sends the wrong thing for 323 of them.

They are also large for prompting: up to 27,551,037 bytes (reddit task 33, one of the
GIFs) and up to 6960x4640 pixels (shopping task 178).

## Per-task reset

`vwa/classifieds` is the only site in the fleet that resets in place: POST
`index.php?page=reset` with the `RESET_TOKEN`, and the endpoint deletes the rows with
id >= 84143 — the 12 seeded items in the range a task run mutates — and re-inserts them,
photos included. Upstream's environment honours `require_reset` for
classifieds and nothing else — for any other site it prints a warning and continues against
dirty state, which is why the published shopping and reddit runs recreated their containers
between batches. 62 of the 910 tasks declare `require_reset`.

## Accounts

The seeded benchmark logins are recorded in each `suite.toml`, transcribed verbatim from
`upstream/browser_env/env_config.py`. They are here because the evaluator chain cannot be
imported without that module and because scoring uses them directly — 107 tasks mint a
Magento admin token from `shopping_site_admin`'s password. They are public in the upstream
repository and already baked into these images.

## Known upstream defects

- **reddit task 184 raises rather than scores.** Its second `eval_fuzzy_image_match`
  alternative is a bare `media/catalog/...` path with no `__SHOPPING__` prefix, and
  `PageImageEvaluator`'s `Image.open` on a non-`http` reference is not inside a
  `try`/`except`.
- **`PageImageEvaluator` keeps only the last query's score.** `score = 1.0` is
  re-initialised inside the per-query loop and returned after it, so on reddit tasks 111,
  112 and 113 — the only tasks with more than one query object — the earlier checks cannot
  fail the task.
- **20 shopping records carry a vestigial top-level `reference_url`**, always the empty
  string. `URLExactEvaluator` reads `eval.reference_url`; a reader that takes the top-level
  one scores those tasks against an empty gold URL.
- **One shopping record's `reasoning_difficulty` is the typo `hrad`.** Nothing scores on
  it, but a strict enum parse dies on it.

## What is vendored and what is not

`upstream/` holds only what scores a task: `evaluation_harness/evaluators.py`,
`helper_functions.py`, `image_utils.py`, `browser_env/env_config.py` and
`scripts/generate_test_data.py`. They are unmodified, because the `func:` strings in
`program_html` are passed to a bare `eval()` and resolved against `helper_functions`'
module globals — a name-resolution dependency, not a declarative spec.

`browser_env/envs.py`, `run.py`, `auto_login.py` and `llms/providers/openai_utils.py` are
**not** vendored. Facts that live only in them — the `viewport_size` merge, the
classifieds-only `require_reset` handling, the storage_state minting — are recorded in the
manifests as prose rather than as code to import.
