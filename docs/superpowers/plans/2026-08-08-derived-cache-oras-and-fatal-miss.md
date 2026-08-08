# Derived-inputs cache: reliable writes, oras artifacts, fatal misses

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the derived-inputs GHCR cache a dependable, digest-pinned build input: writes retry and fail loudly (#80), entries become oras artifacts, and a miss on a pinned entry fails the build unless explicitly forced (#42).

**Architecture:** Three layers, landing in dependency order. First the write path becomes reliable (a push that fails is no longer stamped as success). Then a shared shell library, `builder/stage-lib/derive-cache.sh`, owns the cache protocol for all seven derive scripts: read prefers an oras artifact, falls back to the legacy one-layer scratch image, and on a legacy hit re-pushes the already-local bytes as oras — so format migration is amortized onto the next rebuild instead of a separate multi-tens-of-GB transfer job. Finally a checked-in digest lock decides whether a miss is fatal.

**Tech Stack:** bash (the derive scripts are shell, run on the self-hosted runner by `builder/docker.py run_prepare`), `oras` CLI (pinned release, checksum-verified), Docker (legacy read path only), pytest for unit + integration tests, a throwaway `registry:2` container for round-trip tests.

## Global Constraints

- **Never touch `datasets/`** — the persistent download cache on the runner.
- **Never `docker system prune`**, never touch containers this plan did not create. The `nice_galois` / `serene_colden` registry containers and `miniwob` / `classifieds_db` are NOT ours.
- Test registries must run on a port this plan chooses and must be removed by the test's own teardown.
- Commits: author `shkolnik-beep <jshkolnik@gmail.com>`, **no `Co-Authored-By`**, no session trailers.
- One branch, one PR: `derived-cache-oras-and-fatal-miss`, branched from `origin/main`. Merge with a merge commit, never squash.
- `COPY --chmod=755` for any script copied into an image; `tail -n 50`, never `tail -50`.
- Prove every guard test's teeth by regressing the **production** script, never by editing the test.
- Do not bump any `RECIPE=` literal. Bumping strands live cache entries and forces a re-derive from the university mirrors — the exact cost this work exists to avoid.
- Do not edit `images/webarena/wikipedia/split-zim.sh`'s push path (Task 3 gives it the dual-format **read** only). Its cache is ~88 GB; opportunistically re-pushing it would add that upload to the next fleet run and risks the 300-minute `timeout-minutes`.

### The seven cache sites

| script | package | RECIPE | live tag |
|---|---|---|---|
| `images/vwa/classifieds/derive-backup.sh` | `vwa-classifieds-derived` | r1 | `a2a794da92f6-r1` |
| `images/webarena/gitlab/derive-backup.sh` | `webarena-gitlab-derived` | r1 | `6269a90527a6-r1` |
| `images/webarena/reddit/derive-backup.sh` | `webarena-reddit-derived` | r2 | `6ff70f73bc80-r2` |
| `images/webarena/shopping/derive-backup.sh` | `webarena-shopping-derived` | r2 | `2052430ee930-r2` |
| `images/webarena/shopping-admin/derive-backup.sh` | `webarena-shopping-admin-derived` | r2 | `ad607557a79f-r2` |
| `images/webshop/server/derive-backup.sh` | `webshop-server-derived` | r1 | `fbb2df1f2a0e-r1` |
| `images/webarena/wikipedia/split-zim.sh` | `webarena-wikipedia-derived` | r1 | (read-only in this plan) |

`derive-backup.sh` × 6 is "the six" throughout. Verified against GHCR 2026-08-08: one live tag per package, plus a stale `webarena-reddit-derived:6ff70f73bc80` (r1-era) and bare `<sha12>` aliases on gitlab/shopping/shopping-admin. Nothing in this plan touches the stale ones.

---

### Task 1: #80 — a failed cache push fails the run

**Why:** `prepare_reuse_check` stamps once the prepare step succeeds, and `run_prepare` then skips the script entirely on every later run whose inputs match. So the one run that derives is the only run that ever pushes; `docker push || echo "warning: …"` leaves the cache permanently empty while every later build silently depends on one runner's disk. This is exactly what PR #16 fixed in `split-zim.sh` (run 31259175714 demonstrated it live).

**Files:**
- Modify: `images/vwa/classifieds/derive-backup.sh`, `images/webarena/gitlab/derive-backup.sh`, `images/webarena/reddit/derive-backup.sh`, `images/webarena/shopping/derive-backup.sh`, `images/webarena/shopping-admin/derive-backup.sh`, `images/webshop/server/derive-backup.sh` — the final `docker push` line in each
- Test: `tests/test_derive_cache_push.py` (create)

**Interfaces:**
- Produces: nothing importable. Task 3 replaces these push blocks with `dcache_push`; the test written here must keep passing against that replacement, so assert on **behaviour present in the file** (a retry loop and a non-zero exit), not on the literal string `docker push`.

- [ ] **Step 1: Write the failing test**

`tests/test_derive_cache_push.py`:

```python
"""A derive script that cannot publish its cache must fail, not warn.

builder/docker.py stamps a successful prepare step and skips the script on
every later run with matching inputs, so the run that derives is the only run
that ever pushes. A warning there leaves the cache empty for good while later
builds believe it is populated — PR #16's finding on wikipedia, applied to the
other six.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SIX = sorted((REPO / "images").glob("*/*/derive-backup.sh"))


def _code(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_all_six_scripts_are_present():
    assert len(SIX) == 6, [str(p) for p in SIX]


@pytest.mark.parametrize("script", SIX, ids=lambda p: p.parent.name)
def test_a_push_failure_is_never_swallowed(script):
    code = _code(script.read_text())
    assert not re.search(r"push[^\n]*\|\|\s*echo", code), (
        f"{script}: a failed cache push is reported as a warning and the run "
        "still succeeds, so prepare_reuse_check stamps it and nothing retries")


@pytest.mark.parametrize("script", SIX, ids=lambda p: p.parent.name)
def test_the_push_is_retried_then_fatal(script):
    code = _code(script.read_text())
    assert "for attempt in 1 2 3" in code, f"{script}: no retry loop"
    assert re.search(r"exit 1", code), f"{script}: no hard failure path"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd ~/workspace/browser-use-benchmarks && python -m pytest tests/test_derive_cache_push.py -v`
Expected: 12 parametrized failures (6 × "warning is swallowed", 6 × "no retry loop"); the presence test passes.

- [ ] **Step 3: Apply the fix to all six**

In each of the six, replace the single push line with the block below. Keep each script's existing trailing message (`webshop/server` says "fetch succeeded", not "derivation succeeded" — preserve that wording where it appears elsewhere in the file). Mirror `split-zim.sh:355-372`:

```bash
# Retry, then FAIL. builder/docker.py stamps a successful prepare and skips
# this script on every later run with matching inputs, so this is the only run
# that will ever push: a warning here leaves the cache empty permanently while
# later builds depend on it. Retries because a transient GHCR error must not
# throw away a finished derivation.
pushed=
for attempt in 1 2 3; do
  if docker push "$CACHE"; then
    pushed=yes
    break
  fi
  echo "cache push attempt $attempt/3 failed" >&2
  [ "$attempt" = 3 ] || sleep 30
done
if [ -z "$pushed" ]; then
  echo "derive: could not publish $CACHE after 3 attempts. Failing rather than" \
       "stamping: prepare_reuse_check would skip this script on the next run," \
       "so nothing would ever retry the push and the cache would stay empty" \
       "for good. The artifacts in $DATASETS_DIR are intact and correct." >&2
  exit 1
fi
```

- [ ] **Step 4: Run the test and the whole suite**

Run: `python -m pytest tests/ -x -q`
Expected: all green, including the new file.

- [ ] **Step 5: Prove the teeth by regressing production**

Restore `docker push "$CACHE" || echo "warning: cache push failed"` in **one** script (`images/webshop/server/derive-backup.sh`), run `python -m pytest tests/test_derive_cache_push.py -q`, and confirm that script's two cases go red. Then restore the fix. Record the observed failure output in the commit message.

- [ ] **Step 6: Commit**

```bash
git add tests/test_derive_cache_push.py images/*/*/derive-backup.sh
git commit -m "derive: a cache push that fails must fail the run (#80)"
```

---

### Task 2: the shared cache library

**Files:**
- Create: `builder/stage-lib/derive-cache.sh`
- Create: `tests/test_derive_cache_lib.py` (pure-bash unit tests, no registry)
- Create: `tests/integration/test_derive_cache_roundtrip.py` (real `registry:2`)

**Interfaces:**
- Produces, all sourced (`. "$REPO_ROOT/builder/stage-lib/derive-cache.sh"`), never executed:
  - `dcache_ensure_oras` — puts a pinned `oras` on PATH; idempotent; fails loudly.
  - `dcache_pull REF DEST_DIR` — returns 0 on a hit with files written into `DEST_DIR`, 1 on a miss. Sets `DCACHE_HIT_FORMAT` to `oras` or `legacy`. A legacy hit extracts through the caller's existing wildcard filter (passed as `$3`, required — see #79) and, on success, re-pushes the extracted files as an oras artifact under the same ref, non-fatally.
  - `dcache_push REF DIR FILE...` — pushes the named files (relative to `DIR`) as an oras artifact, retry-3×-then-`exit 1`, same contract as Task 1.
  - `DCACHE_PUSHED_DIGEST` — set by `dcache_push` to the manifest digest it published.

- [ ] **Step 1: Write the failing unit tests**

`tests/test_derive_cache_lib.py`. Run the real library in bash with a fake `oras` on PATH; assert on the commands it would run.

```python
"""builder/stage-lib/derive-cache.sh, exercised without a registry.

The round trip against a real registry lives in tests/integration/. These
cover the decision table: which format is tried first, what a miss returns,
and that a failed push is fatal exactly as #80 requires.
"""
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "builder" / "stage-lib" / "derive-cache.sh"


def run(body, tmp_path, fake_oras="exit 0", fake_docker="exit 1", env=None):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for name, script in (("oras", fake_oras), ("docker", fake_docker)):
        p = bin_ / name
        p.write_text(f"#!/bin/bash\necho \"{name} $*\" >> {tmp_path}/calls\n{script}\n")
        p.chmod(0o755)
    script = tmp_path / "t.sh"
    script.write_text(f"set -u\n. {LIB}\n" + textwrap.dedent(body))
    e = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}",
         "DCACHE_SKIP_ORAS_INSTALL": "1", **(env or {})}
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=e, cwd=tmp_path)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return r, calls


def test_the_library_exists():
    assert LIB.is_file()


def test_a_pull_prefers_the_oras_artifact(tmp_path):
    r, calls = run('dcache_pull reg/x:t out "*.dat" && echo HIT', tmp_path)
    assert "HIT" in r.stdout, r.stderr
    assert calls[0].startswith("oras pull"), calls
    assert not any(c.startswith("docker pull") for c in calls), calls


def test_a_pull_falls_back_to_the_legacy_image(tmp_path):
    r, calls = run('dcache_pull reg/x:t out "*.dat" && echo HIT',
                   tmp_path, fake_oras="exit 1", fake_docker="exit 0")
    assert "HIT" in r.stdout, r.stderr
    assert any(c.startswith("docker pull") for c in calls), calls


def test_a_legacy_hit_is_re_pushed_as_oras(tmp_path):
    r, calls = run('dcache_pull reg/x:t out "*.dat"', tmp_path,
                   fake_oras="[ \"$1\" = pull ] && exit 1; exit 0", fake_docker="exit 0")
    assert any(c.startswith("oras push") for c in calls), (
        "a legacy hit did not migrate the entry, so it stays legacy forever")


def test_a_re_push_failure_does_not_fail_the_build(tmp_path):
    """Migration is opportunistic: the bytes are already in hand."""
    r, calls = run('dcache_pull reg/x:t out "*.dat" && echo HIT', tmp_path,
                   fake_oras="exit 1", fake_docker="exit 0")
    assert "HIT" in r.stdout and r.returncode == 0, r.stderr


def test_a_miss_returns_one_and_says_so(tmp_path):
    r, calls = run('dcache_pull reg/x:t out "*.dat" || echo MISS', tmp_path,
                   fake_oras="exit 1", fake_docker="exit 1")
    assert "MISS" in r.stdout, r.stderr


def test_a_push_retries_three_times_then_exits_nonzero(tmp_path):
    r, calls = run('DCACHE_RETRY_SLEEP=0 dcache_push reg/x:t . a.dat; echo "rc=$?"',
                   tmp_path, fake_oras="exit 1")
    assert len([c for c in calls if c.startswith("oras push")]) == 3, calls
    assert r.returncode != 0, "a failed push must be fatal (#80)"


def test_an_extract_filter_is_required(tmp_path):
    """#79: an unfiltered docker export drops /dev,/etc,/proc,/sys into datasets."""
    r, _ = run('dcache_pull reg/x:t out; echo "rc=$?"', tmp_path)
    assert r.returncode != 0 or "rc=0" not in r.stdout
```

- [ ] **Step 2: Run them and watch every one fail**

Run: `python -m pytest tests/test_derive_cache_lib.py -v`
Expected: all fail — the library does not exist.

- [ ] **Step 3: Write `builder/stage-lib/derive-cache.sh`**

Requirements the tests above pin, plus:
- `dcache_ensure_oras` downloads a **pinned** oras release to `${DCACHE_TOOL_DIR:-$HOME/.cache/derive-cache}` and verifies a hardcoded sha256 before use; skipped entirely when `DCACHE_SKIP_ORAS_INSTALL=1` or `oras` is already on PATH. Pick the current stable release, record the version and checksum in a comment, and state in the commit message how the checksum was obtained.
- `DCACHE_RETRY_SLEEP` defaults to 30, overridable so tests do not sleep 90s.
- The legacy read path keeps the existing idiom exactly — `docker create` / `docker export … | tar -x -C "$DEST" --wildcards "$FILTER"` / `docker rm` / `docker rmi` — because #79 and the ~2× local-storage cost of holding a cache image both apply unchanged.
- Every function usable under the callers' `set -euo pipefail`.

- [ ] **Step 4: Run the unit tests to green**

Run: `python -m pytest tests/test_derive_cache_lib.py -v`

- [ ] **Step 5: Write the integration round trip**

`tests/integration/test_derive_cache_roundtrip.py`: start `registry:2` on **port 5555** with a container name unique to this test (`dcache-test-registry`), `--rm`, removed in a fixture teardown that runs even on failure. Then:
1. `dcache_push` three files including one ≥1 MiB; assert `dcache_pull` into a fresh dir returns them byte-identical (compare sha256).
2. Build a **legacy** scratch image with the same files, `docker push` it, and assert `dcache_pull` hits via the fallback, writes the files correctly, and that a subsequent `dcache_pull` takes the **oras** path — i.e. the migration actually happened in the registry, not just in a log line.
3. Assert the datasets dir contains no `etc/`, `dev/`, `proc/`, `sys/` or `.dockerenv` after a legacy hit (#79 regression guard).

Mark it with whatever marker `tests/integration/test_run_services_pid1.py` uses for docker-requiring tests; follow that file's fixture style.

- [ ] **Step 6: Run it**

Run: `python -m pytest tests/integration/test_derive_cache_roundtrip.py -v`
Then confirm no stray container: `docker ps -a --filter name=dcache-test-registry` must be empty.

- [ ] **Step 7: Prove the migration assertion has teeth**

In the library, make the legacy hit path skip its re-push. Round-trip case 2 must go red at the "subsequent pull takes the oras path" assertion. Restore.

- [ ] **Step 8: Commit**

```bash
git add builder/stage-lib/derive-cache.sh tests/test_derive_cache_lib.py tests/integration/test_derive_cache_roundtrip.py
git commit -m "derive: a shared cache library that reads both formats and writes oras"
```

---

### Task 3: convert the seven scripts

**Files:**
- Modify: the six `derive-backup.sh` (read + write path)
- Modify: `images/webarena/wikipedia/split-zim.sh` (**read path only** — see Global Constraints)
- Modify: `tests/test_derive_cache_push.py` if the Task 1 assertions no longer fit the `dcache_push` shape — but only by making them assert the same *behaviour*, never by weakening them
- Test: `tests/test_derive_cache_sites.py` (create)

**Interfaces:**
- Consumes: `dcache_pull`, `dcache_push`, `DCACHE_PUSHED_DIGEST` from Task 2.

- [ ] **Step 1: Write the failing static guard**

`tests/test_derive_cache_sites.py`: for each of the seven scripts assert it sources `derive-cache.sh`; for the six assert no bare `docker pull "$CACHE"` and no `docker build -t "$CACHE"` remain; for all seven assert every `docker export` still passes through a wildcard filter (delegate to the existing helper in `tests/test_derive_export_filtered.py` rather than re-implementing the regex).

- [ ] **Step 2: Run it, watch it fail**

- [ ] **Step 3: Convert the six**

Each script's hit block becomes a `dcache_pull` call carrying that script's existing wildcard (e.g. `'*gitlab*'`, `'classifieds_*'`, `'reddit_*'`, `'shopping_*'`, `'shopping_admin_*'`, `'items_*'`) and keeping any post-extract reassembly (gitlab/classifieds/reddit/shopping `cat … .part-* > …`). The push block becomes `dcache_push` over the same file list the Dockerfile used to `COPY`, keeping the 8G `split` — GHCR's ~10 GB ceiling applies to oras blobs too.

- [ ] **Step 4: Give wikipedia the dual-format read only**

Replace its `docker pull` hit block with `dcache_pull … "*.zim*"`, keep the `$SIZES` manifest check and every existing invariant, and **leave its push path building and pushing the legacy image**. Add a comment saying why (88 GB; converts when next re-derived, not opportunistically).

- [ ] **Step 5: Full suite + teeth**

Run `python -m pytest tests/ -q`. Then delete the `dcache_pull` line from one script and confirm the new guard goes red.

- [ ] **Step 6: Commit**

```bash
git commit -am "derive: all seven caches read oras; the six write it (#42)"
```

---

### Task 4: the digest lock, fatal misses, and the escape hatch

**Files:**
- Create: `builder/derived-cache.lock`
- Modify: `builder/stage-lib/derive-cache.sh` (miss policy)
- Modify: `.github/workflows/build.yml` (`workflow_dispatch` input + env plumb)
- Test: `tests/test_derive_cache_lock.py` (create)

**Interfaces:**
- Consumes: Task 2's `dcache_pull` / `DCACHE_PUSHED_DIGEST`.
- Produces: `dcache_require REF` — called by each script right after `dcache_pull` misses. Lock format, one entry per line, `#` comments allowed:
  `ghcr.io/shkolnik/webarena-gitlab-derived:6269a90527a6-r1 sha256:<64hex>`

- [ ] **Step 1: Write the failing tests**

`tests/test_derive_cache_lock.py`:
- a miss on a ref **present** in the lock exits non-zero, and the message names both the ref and `ALLOW_DERIVE_CACHE_MISS`
- a miss on a ref **absent** from the lock returns cleanly so the script derives (new image / deliberate `RECIPE` bump)
- `ALLOW_DERIVE_CACHE_MISS=1` downgrades the pinned-miss failure to a warning on stderr
- the lock file parses: every non-comment line is `<ref> sha256:<64 hex>`, refs are unique, and each ref's package is one of the seven
- after a successful `dcache_push`, the script prints the exact lock line to add, including the real digest

- [ ] **Step 2: Run, watch fail**

- [ ] **Step 3: Implement `dcache_require` + populate the lock**

Seed `builder/derived-cache.lock` with the six live refs from the table above, resolving each real manifest digest against GHCR (`oras manifest fetch --descriptor`, or the registry API with a token minted from `gh auth token`). Do **not** invent digests — a wrong pin turns every build red. Leave wikipedia out of the lock for now and say why in a comment: its entry stays legacy until it is next re-derived, and pinning a digest we are about to replace would be a trap.

- [ ] **Step 4: Wire the escape hatch through CI**

Add a `workflow_dispatch` input `allow_derive_cache_miss` (boolean, default false) to `.github/workflows/build.yml`, exported as `ALLOW_DERIVE_CACHE_MISS` into the build step's env. Document it in one line in the workflow's header comment: it exists for a GHCR outage.

- [ ] **Step 5: Verify the digests are real, not plausible**

For each of the six lock lines, fetch the manifest from GHCR and confirm the digest matches. Paste the six ref→digest pairs into the commit message. **This is the one step that cannot be taken on trust** — report the exact command used.

- [ ] **Step 6: Prove the teeth**

Point one lock line at a wrong-but-well-formed digest and confirm a simulated miss on that ref fails with the right message; restore. Then confirm `ALLOW_DERIVE_CACHE_MISS=1` lets it through.

- [ ] **Step 7: Full suite, then commit**

```bash
git commit -am "derive: a pinned cache entry that is missing fails the build (#42)"
```

---

## Self-review notes

- **Spec coverage:** #80 → Task 1. oras → Tasks 2-3. Fatal-on-miss + escape hatch + digest pin → Task 4. Amortized migration → Task 2 Step 3 / Task 3 Step 3. Wikipedia's exclusion from the write-side migration is a stated, justified narrowing, not a gap.
- **Sequencing:** Task 1 must land before Task 4 — a fatal miss on top of unreliable writes is a trap, which is why #80 is not folded in.
- **Naming:** `dcache_pull` / `dcache_push` / `dcache_require` / `dcache_ensure_oras` / `DCACHE_HIT_FORMAT` / `DCACHE_PUSHED_DIGEST` / `DCACHE_RETRY_SLEEP` / `DCACHE_SKIP_ORAS_INSTALL` / `DCACHE_TOOL_DIR` — used identically in every task.
- **Not verified yet, for the implementer to confirm:** that GHCR's ~10 GB layer ceiling applies to oras blobs the same way it applies to image layers. Task 2's integration test does not prove this (it is a local registry). Keep the 8 G split regardless; if it turns out oras chunks large blobs itself, that is a follow-up, not a change of plan.
