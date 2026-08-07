# Fleet Service-Contract Standardization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every runnable image in the fleet the same two-part service contract — an in-container `HEALTHCHECK` that owns *readiness*, and required `HTTP_HOST`/`HTTP_PORT` that own the *published address* — then make the build driver depend on the healthcheck instead of re-implementing readiness itself.

**Architecture:** Readiness moves into the image, where the app's own timing is known; the driver keeps only a thin host-side reachability check, because in-container health and host-published reachability are different facts and #66 was a failure only the latter could see. Both halves of the contract are about *addresses*, and they differ (reddit listens on 80, publishes on 9999), so each image states both explicitly rather than deriving one from the other.

**Tech Stack:** Docker / BuildKit, docker compose v2 (v5.3.1 on this host), Python 3.11 stdlib (`urllib`, `subprocess`, `tomllib`), pytest.

## Why this is ONE plan and not two (#69 + #71 merged)

Recorded because the merge is the load-bearing decision, and a future reader will otherwise
re-split them:

1. **CI cost.** `.github/workflows/build.yml`'s `discover` job takes the
   `shared build inputs changed: building all` branch whenever a push touches `builder/`. Both
   standardizations need driver changes, so as two plans they would force **two full-fleet
   rebuilds** — serialized at `max-parallel: 1`, with classifieds measured at 85 min and
   shopping's derive longer still. One branch, one merge commit, one full-fleet run.
2. **One contract, two sides.** `HEALTHCHECK` runs *inside* the container and must name the
   in-container listen port; `[service].healthcheck` and `HTTP_HOST`/`HTTP_PORT` name the
   *published* side. Reddit's `9999:80` is the case that proves these are distinct values, and
   that distinction is exactly what #66 was.
3. **Ordering coupling.** `HTTP_HOST` changes what the app serves at the probed address. Landing
   healthchecks first and host/port second would let the second pass move a gate the first pass
   just tuned.

Cost accepted: a red per-image task is ambiguous between the two changes. Mitigated by **two
separate commits inside each per-image task**, so `git bisect` stays per-change; only the rebuild
is shared.

## Global Constraints

- **Commits:** author `shkolnik-beep` / `jshkolnik@gmail.com`. **No `Co-Authored-By` trailer.**
- **Branch + PR:** one branch `fleet-service-contract` for this whole plan, per-task commits on
  it, self-merge once CI is green, **merge commit — never squash**.
- **Never touch `datasets/`** or `$HOME/benchmark-datasets` — the persistent download cache.
- **Never** `docker system prune`, `pkill chrome`, `killall`, or `docker rm` a container this plan
  did not create. `nice_galois`, `serene_colden`, `miniwob`, `classifieds_db` are NOT ours.
- **Never cancel a CI run you did not start.**
- **Verify by running.** A task is not done until its behavior was observed live. Never weaken an
  assertion to get green.
- **Prove a guard test's teeth by regressing the PRODUCTION code**, not by editing the test.
- `HTTP_HOST` and `HTTP_PORT`: **two variables, no defaults, hard fail at entrypoint**, port
  **always** written out, **no scheme** (nothing in the fleet serves TLS). They describe the
  **PUBLISHED** side of the port mapping, never the in-container listen port.
- `COPY --chmod=755` for every script copied into an image (needs
  `# syntax=docker/dockerfile:1` at the top of the Dockerfile). Never rely on the checkout's mode.
- `tail -n 50`, never `tail -50` — the obsolete form dies on a multi-file glob and prints nothing.
- Compose keeps the webarena-standard published ports (7770 / 7780 / 9999 / 8023) and **one
  mapping per service**. Do not add extra port mappings.

## Verified fact base

Every line below was confirmed on this host on 2026-08-07, not recalled. Re-derive rather than
trust if more than a few weeks have passed.

**`docker compose up --wait` is bounded and terminal on unhealthy.** A synthetic image with
`HEALTHCHECK --start-period=5s --interval=2s --timeout=2s --retries=2 CMD false` produced:

```
 Container hcprobe-bad-1 Waiting
container hcprobe-bad-1 is unhealthy
EXIT=1 ELAPSED=8s
```

So worst-case wait ≈ `start-period + retries × (timeout + interval)`, and the driver never needs
its own readiness budget on top.

**Inspect shapes.** For an image *with* a healthcheck:

```
$ docker image inspect --format '{{json .Config.Healthcheck}}' hcprobe-bad:test
{"Test":["CMD-SHELL","false"],"Interval":2000000000,"Timeout":2000000000,"StartPeriod":5000000000,"Retries":2}
```

For an image *without* one, both `.Config.Healthcheck` and the container's `.State.Health` print
the literal `null`. ⚠️ A dotted template on a healthcheck-less container **errors**:

```
$ docker inspect --format '{{.State.Health.Status}}' <cid>
template parsing error: ... map has no entry for key "Health"
```

**So always read `{{json .State.Health}}` and branch on `null` in Python — never
`{{.State.Health.Status}}`.**

**`.State.Health.Log`** carries per-probe `Start`/`End`/`ExitCode`/`Output` (last 5 kept). This is
the evidence #65 (gitlab unhealthy after 1107s) has never had, and Task 1 exposes it.

**Probe binaries, measured by running each image** (`command -v`):

| image | curl | wget | notes |
|---|---|---|---|
| miniwob-server | ✅ `/usr/bin/curl` | busybox | nginx:alpine ships curl |
| webarena-reddit | ✅ `/usr/bin/curl` | busybox | |
| webarena-shopping-admin | ✅ `/usr/bin/curl` | **MISSING** | |
| webshop-server | ✅ `/usr/bin/curl` | GNU wget | |

**`curl` is the one universal probe — use it everywhere.** `wget` is not available fleet-wide.
(classifieds and shopping were not testable locally but install/inherit curl in their runtime
stages; shopping shares shopping-admin's base image by digest. Confirm during their tasks.)

**`docker compose -f <file> config --format json`** resolves each service's image, verified:

```
{'gitlab': 'ghcr.io/shkolnik/webarena-gitlab:latest', 'reddit': 'ghcr.io/shkolnik/webarena-reddit:latest', ...}
```

**`curl -f` does not fail on 3xx**, only ≥400 — `-L` is load-bearing wherever the probed path
redirects.

**`--start-period` failures do not count toward `--retries`**, but a success during it marks the
container healthy immediately.

**Docker never overlaps healthcheck probes**, so a long `--timeout` is safe; `--interval` only
controls the gap after a probe finishes.

## The address table — the spine of this plan

| image | in-container listen | published | HEALTHCHECK target (in-container) | `[service].healthcheck` (published) | HTTP_HOST/PORT |
|---|---|---|---|---|---|
| miniwob/server | 8399 | 8399 | `http://localhost:8399/miniwob/click-button.html` | `http://localhost:8399/miniwob/click-button.html` | **to add** |
| vwa/classifieds | 9980 | 9980 | `http://localhost:9980/` | `http://localhost:9980/` | **to add** |
| webarena/reddit | **80** | **9999** | `http://localhost:80/forums` | `http://localhost:9999/forums` | **to add** |
| webarena/shopping | 7770 | 7770 | `http://localhost:7770/` | `http://127.0.0.1:7770/` | shipped |
| webarena/shopping-admin | 7780 | 7780 | `http://localhost:7780/admin` | `http://127.0.0.1:7780/admin` | shipped |
| webarena/gitlab | 8023 | 8023 | `gitlab-healthcheck` (shipped) | `http://localhost:8023/explore` | **to add — see Task 8** |
| webshop/server | 3000 | 3000 | `http://localhost:3000/` (shipped) | `http://localhost:3000/` | **to add** |

**Reddit is the row that proves the two columns are different.** Never copy `[service].healthcheck`
into a `HEALTHCHECK` line.

## File Structure

**Modified — driver (all in one place, so the fleet rebuild is paid once):**
- `builder/docker.py` — add `image_healthcheck`, `container_health`, `compose_service_image`,
  `dump_health_log`; enforce a declared healthcheck in `run_smoke`; thin `poll_health` to a
  reachability check.
- `builder/manifest.py` — retire `healthcheck_timeout_s`, add `reachability_timeout_s`, fail loud
  on the retired key.
- `tests/test_docker.py`, `tests/test_manifest.py` — guards for all of the above.

**Modified — per image (each self-contained; no two tasks touch the same file):**
- `images/miniwob/server/{Dockerfile,image.toml,entrypoint.sh}`
- `images/vwa/classifieds/{Dockerfile,image.toml,entrypoint.sh}` + `images/vwa/compose.yml`
- `images/webarena/reddit/{Dockerfile,image.toml,entrypoint.sh}` + `images/webarena/compose.yml`
- `images/webarena/shopping/{Dockerfile,image.toml}`
- `images/webarena/shopping-admin/{Dockerfile,image.toml}`
- `images/webarena/gitlab/{Dockerfile,image.toml,entrypoint.sh}`
- `images/webshop/server/{Dockerfile,image.toml,entrypoint.sh}`

⚠️ **`images/webarena/compose.yml` is touched by both the reddit task (Task 4) and the gitlab task
(Task 8).** Those two must not run concurrently — see the execution grouping.

**Created:**
- `docs/service-contract.md` — the fleet contract, one page.

## Execution grouping

```
Group A (alone, first):      Task 1  — health-log diagnostics
Group B (parallel, 5 ways):  Task 2 miniwob | Task 3 classifieds | Task 4 reddit
                             Task 5 shopping | Task 6 shopping-admin
Group C (parallel, 2 ways):  Task 7 webshop | Task 8 gitlab      (Task 8 after Task 4: compose.yml)
Group D (alone, last):       Task 9  — driver enforcement + poll_health thinning
Group E (alone):             Task 10 — docs
```

Task 1 lands first because it is a pure addition that makes every later task's failures legible —
including #65's. Task 9 lands **last** because it fails any image that has no `HEALTHCHECK`, so it
must not precede Groups B and C.

---

### Task 1: Expose the container's healthcheck log in smoke diagnostics

**Why:** when `up --wait` reports `container X is unhealthy`, the probe's own exit code and output
are the direct evidence and are currently discarded. #65 (gitlab unhealthy after 1107s, service
tree present) has been unexplained for exactly this reason.

**Files:**
- Modify: `builder/docker.py` (add helpers; call from `dump_service_diagnostics`)
- Test: `tests/test_docker.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `container_health(compose: Path, service: str, check_output=subprocess.check_output) -> dict | None`
    — parsed `.State.Health`, or `None` when the container declares no healthcheck / cannot be found.
  - `dump_health_log(compose: Path, service: str, check_output=subprocess.check_output, log=print) -> None`
    — best-effort printer, never raises.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_docker.py`:

```python
def test_container_health_parses_the_health_object(tmp_path):
    calls = []
    def fake(cmd, text=True):
        calls.append(cmd)
        if "ps" in cmd:
            return "abc123\n"
        return ('{"Status":"unhealthy","FailingStreak":3,'
                '"Log":[{"Start":"t0","End":"t1","ExitCode":7,"Output":"boom\\n"}]}')
    h = docker_mod.container_health(tmp_path / "compose.yml", "reddit", check_output=fake)
    assert h["Status"] == "unhealthy"
    assert h["Log"][0]["ExitCode"] == 7

def test_container_health_is_None_when_no_healthcheck_is_declared(tmp_path):
    # `docker inspect` prints the literal "null", and the dotted-template form
    # ERRORS on such a container — verified live. Reading it as JSON is the
    # only shape that survives both cases.
    def fake(cmd, text=True):
        return "abc123\n" if "ps" in cmd else "null"
    assert docker_mod.container_health(tmp_path / "compose.yml", "x",
                                       check_output=fake) is None

def test_dump_health_log_prints_exit_code_and_output(tmp_path):
    def fake(cmd, text=True):
        if "ps" in cmd:
            return "abc123\n"
        return ('{"Status":"unhealthy","Log":['
                '{"Start":"t0","End":"t1","ExitCode":7,"Output":"connection refused"}]}')
    lines = []
    docker_mod.dump_health_log(tmp_path / "compose.yml", "reddit",
                               check_output=fake, log=lines.append)
    blob = "\n".join(lines)
    assert "unhealthy" in blob
    assert "exit=7" in blob
    assert "connection refused" in blob

def test_dump_health_log_never_raises_on_the_failure_path(tmp_path):
    # This runs when something has ALREADY failed; it must not replace the real
    # error with one of its own.
    def boom(cmd, text=True):
        raise OSError("docker daemon gone")
    lines = []
    docker_mod.dump_health_log(tmp_path / "compose.yml", "x",
                               check_output=boom, log=lines.append)
    assert any("could not collect" in ln for ln in lines)

def test_diagnostics_include_the_health_log(tmp_path):
    seen = []
    monkey = docker_mod.dump_health_log
    try:
        docker_mod.dump_health_log = lambda *a, **kw: seen.append("called")
        docker_mod.dump_service_diagnostics(tmp_path / "compose.yml", "reddit",
                                           runner=lambda cmd: None)
    finally:
        docker_mod.dump_health_log = monkey
    assert seen == ["called"]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd ~/workspace/browser-use-benchmarks && python3 -m pytest tests/test_docker.py -k "health_log or container_health" -v`

Expected: FAIL — `AttributeError: module 'builder.docker' has no attribute 'container_health'`.

- [ ] **Step 3: Implement**

In `builder/docker.py`, directly after `dump_service_logs`:

```python
def container_health(compose: Path, service: str,
                     check_output=subprocess.check_output) -> dict | None:
    """The container's parsed .State.Health, or None if it declares no healthcheck.

    Read as JSON rather than through `--format '{{.State.Health.Status}}'`: on a
    container with no healthcheck that template does not print an empty string,
    it FAILS with `map has no entry for key "Health"` (verified 2026-08-07). The
    json form prints the literal `null` in the same case, which parses.
    """
    ids = check_output(["docker", "compose", "-f", str(compose), "ps", "-aq", service],
                       text=True).strip().splitlines()
    if not ids:
        return None
    out = check_output(["docker", "inspect", "--format", "{{json .State.Health}}",
                        ids[0].strip()], text=True).strip()
    if out in ("", "null"):
        return None
    return json.loads(out)

def dump_health_log(compose: Path, service: str,
                    check_output=subprocess.check_output, log=print) -> None:
    """The probe's own exit codes and output — the evidence `up --wait` throws away.

    When --wait says `container X is unhealthy` it reports no reason at all, and
    the reason is sitting in .State.Health.Log (docker keeps the last 5 probes).
    gitlab went unhealthy after 1107s twice with its service tree present and
    nothing in this pipeline could say why.

    Best-effort, like the other dumps: it runs on an already-failing path.
    """
    log(f"=== healthcheck probe log for {service} ===")
    try:
        health = container_health(compose, service, check_output=check_output)
    except Exception as e:
        log(f"(could not collect health log: {e})")
        return
    if health is None:
        log("(container declares no healthcheck — nothing to report)")
        return
    log(f"status={health.get('Status')} failing_streak={health.get('FailingStreak')}")
    for entry in health.get("Log", [])[-5:]:
        log(f"  exit={entry.get('ExitCode')} start={entry.get('Start')} end={entry.get('End')}")
        output = (entry.get("Output") or "").strip()
        if output:
            for line in output[:2000].splitlines():
                log(f"    {line}")
```

Then add the call inside `dump_service_diagnostics`, as its **first** probe, before the existing
`probes` loop:

```python
def dump_service_diagnostics(compose: Path, service: str,
                             runner=subprocess.run) -> None:
    # ... existing docstring comment stays ...
    dump_health_log(compose, service)
    probes = [
        # ... unchanged ...
    ]
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `python3 -m pytest tests/test_docker.py -v`
Expected: PASS, and no previously-passing test regresses.

- [ ] **Step 5: Prove it against a REAL unhealthy container**

This is the verify-by-running gate — the unit tests all use fakes, so they prove the parsing, not
the shape of what docker actually emits.

```bash
cd "$(mktemp -d /tmp/hcplan-t1.XXXX)"
cat > Dockerfile <<'EOF'
FROM busybox
HEALTHCHECK --start-period=2s --interval=2s --timeout=2s --retries=2 \
  CMD echo "probe says no" && false
CMD sleep 300
EOF
printf 'services:\n  bad:\n    build: .\n    image: hcplan-t1:test\n' > compose.yml
docker compose up -d --wait bad || true
python3 - <<'PY'
import sys; sys.path.insert(0, "/home/agent/workspace/browser-use-benchmarks")
from pathlib import Path
from builder.docker import dump_health_log
dump_health_log(Path("compose.yml"), "bad")
PY
docker compose down -v; docker image rm -f hcplan-t1:test
```

Expected: `status=unhealthy`, at least one `exit=1` line, and the literal `probe says no`.
**If `Output` is empty, stop and investigate before continuing** — an empty Output would make this
whole task cosmetic.

- [ ] **Step 6: Commit**

```bash
git add builder/docker.py tests/test_docker.py
git commit -m "smoke: report the healthcheck probe's own exit code and output

\`up --wait\` says only \`container X is unhealthy\`; the reason is in
.State.Health.Log and was being discarded. gitlab has gone unhealthy after
~1100s twice with its service tree present and nothing could say why.

Read as {{json .State.Health}}, not {{.State.Health.Status}}: on a container
with no healthcheck the dotted template fails outright with \`map has no entry
for key \"Health\"\`, while the json form prints \`null\` and parses."
```

---

### Task 2: miniwob/server — HEALTHCHECK + the HTTP_HOST/HTTP_PORT contract

**Why:** static nginx over the miniwob task HTML. All asset paths in the task pages are relative
(`../core/...`, `../common/...`), so nothing host-dependent is *served* — but the contract is
fleet-wide and uniform on purpose, and a validating entrypoint is what makes "this image has no
default hostname" true rather than aspirational.

**Files:**
- Modify: `images/miniwob/server/Dockerfile`
- Create: `images/miniwob/server/entrypoint.sh`
- Modify: `images/miniwob/server/image.toml`
- Modify: `images/miniwob/compose.yml`

**Interfaces:**
- Consumes: nothing.
- Produces: the `entrypoint.sh` validation block reused verbatim by Tasks 3, 4, 7, 8.

- [ ] **Step 1: Write the entrypoint**

Create `images/miniwob/server/entrypoint.sh`:

```bash
#!/bin/sh
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container. They
# are NOT the address the server binds — nginx's listen port is fixed inside the
# image — so publishing on a different port (-p 8080:8399 with HTTP_PORT=8080)
# is a supported thing to do.
#
# miniwob serves static task HTML whose asset paths are all relative, so nothing
# here is rewritten from these values. They are still REQUIRED: the contract is
# uniform across the fleet so that an operator never has to remember which
# images care, and a silently-ignored variable is how a wrong hostname reaches
# an agent unnoticed.
#
# /bin/sh, not bash: this is nginx:alpine and there is no bash.
set -eu

missing=
[ -n "${HTTP_HOST:-}" ] || missing="$missing HTTP_HOST"
[ -n "${HTTP_PORT:-}" ] || missing="$missing HTTP_PORT"
if [ -n "$missing" ]; then
  cat >&2 <<EOF
error:$missing not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container: the PUBLISHED
  side of the port mapping, not the port the server listens on inside.

  Run it like:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=8399 -p 8399:8399 <image>

  Publishing on another port is fine — match HTTP_PORT to the PUBLISHED side:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=8080 -p 8080:8399 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

echo "serving http://${HTTP_HOST}:${HTTP_PORT}/ (listening on 8399 in-container)"
exec nginx -g 'daemon off;'
```

- [ ] **Step 2: Wire it into the Dockerfile**

In `images/miniwob/server/Dockerfile`, after the `COPY nginx.conf` line and before the `LABEL`:

```dockerfile
EXPOSE 8399
# --chmod so the mode is a property of the BUILD, not of whoever checked the
# repo out: a COPY that inherited a 644 script died with "Permission denied"
# before any of its own validation could run.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
# Not nginx directly: HTTP_HOST/HTTP_PORT are validated before anything serves.
ENTRYPOINT ["/entrypoint.sh"]

# localhost:8399 is the IN-CONTAINER address — the healthcheck runs inside the
# container, so it uses the listen port, never the published one. curl is
# present in nginx:alpine (verified by running the image); wget is busybox's and
# is not available fleet-wide, so curl is the fleet's one probe.
#
# A real task page, not `/`: nginx answers `/` with a directory listing or a 404
# depending on config, and the failure this gate exists to catch is a tree that
# extracted to the wrong depth — which only a real content path can see.
HEALTHCHECK --start-period=10s --interval=10s --timeout=5s --retries=3 \
  CMD curl -fsS --max-time 4 http://localhost:8399/miniwob/click-button.html >/dev/null || exit 1
```

⚠️ The Dockerfile already begins with `# syntax=docker/dockerfile:1`, which `--chmod` requires.
Confirm it is still line 1.

- [ ] **Step 3: Add the published-side variables to compose**

Rewrite `images/miniwob/compose.yml`:

```yaml
# miniwob task HTML over static nginx; upstream-standard port 8399.
# Healthcheck URLs live in each service's image.toml ([service].healthcheck).
services:
  server:
    image: ghcr.io/shkolnik/miniwob-server:latest
    # Required, no defaults — see docs/service-contract.md. These are the
    # PUBLISHED side of the mapping below, not the in-container listen port.
    environment:
      HTTP_HOST: 127.0.0.1
      HTTP_PORT: "8399"
    ports:
      - "8399:8399"
```

- [ ] **Step 4: Retire the driver-side timeout from the manifest**

In `images/miniwob/server/image.toml`, the `[service]` block becomes:

```toml
[service]
# The PUBLISHED address the smoke gate reaches this service at — deliberately
# different from the in-container URL the Dockerfile's HEALTHCHECK probes.
# Readiness is the HEALTHCHECK's job now; this is a reachability check only.
healthcheck = "http://localhost:8399/miniwob/click-button.html"
```

(No `healthcheck_timeout_s` was set here, so nothing to remove. Task 9 makes the key an error.)

- [ ] **Step 5: Build and prove BOTH halves live**

```bash
cd ~/workspace/browser-use-benchmarks
bin/build build miniwob/server --datasets-dir "$HOME/benchmark-datasets"

# (a) the contract fails loud when the variables are absent
out=$(docker run --rm ghcr.io/shkolnik/miniwob-server:latest 2>&1) || ec=$?
echo "$out"; echo "exit=$ec"
# Expected: exit=1 and "HTTP_HOST HTTP_PORT not set". NOT exit=126 (that is the
# chmod footgun) and NOT an nginx banner.

# (b) it boots and becomes healthy when they are present
cid=$(docker run -d -e HTTP_HOST=127.0.0.1 -e HTTP_PORT=8399 -p 8399:8399 \
        ghcr.io/shkolnik/miniwob-server:latest)
for i in $(seq 1 30); do
  st=$(docker inspect --format '{{json .State.Health}}' "$cid" | python3 -c \
       'import json,sys; print((json.load(sys.stdin) or {}).get("Status","none"))')
  echo "health=$st"; [ "$st" = healthy ] && break; sleep 2
done
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8399/miniwob/click-button.html
docker rm -f "$cid"
```

Expected: `(a)` exits 1 with our message; `(b)` reaches `health=healthy` and the published-side
curl prints `200`.

- [ ] **Step 6: Prove the healthcheck has TEETH by regressing the image**

A healthcheck that passes on a broken tree is worse than none. Regress the **production**
Dockerfile, not the test:

```bash
cd ~/workspace/browser-use-benchmarks
cp images/miniwob/server/Dockerfile /tmp/hcplan-miniwob.bak
# break the extraction depth — the exact defect this gate exists to catch
sed -i 's|--strip-components=3|--strip-components=4|' images/miniwob/server/Dockerfile
bin/build build miniwob/server --datasets-dir "$HOME/benchmark-datasets"
cid=$(docker run -d -e HTTP_HOST=127.0.0.1 -e HTTP_PORT=8399 ghcr.io/shkolnik/miniwob-server:latest)
sleep 30
docker inspect --format '{{json .State.Health}}' "$cid" | head -c 300; echo
docker rm -f "$cid"
cp /tmp/hcplan-miniwob.bak images/miniwob/server/Dockerfile
bin/build build miniwob/server --datasets-dir "$HOME/benchmark-datasets"
```

Expected: the broken build reports `"Status":"unhealthy"`. **If it reports healthy, the probed
path is wrong — fix it before committing.**

- [ ] **Step 7: Commit — two commits, one per change**

```bash
git add images/miniwob/server/Dockerfile images/miniwob/server/entrypoint.sh images/miniwob/compose.yml
git commit -m "miniwob: require HTTP_HOST/HTTP_PORT rather than guess a hostname"

git add images/miniwob/server/Dockerfile images/miniwob/server/image.toml
git commit -m "miniwob: declare a HEALTHCHECK so \`up --wait\` gates on readiness

Probes a real task page on the IN-CONTAINER port: a tree extracted to the
wrong depth still answers on /, and that is the defect this gate exists for.
Proven by regressing --strip-components in the production Dockerfile."
```

---

### Task 3: vwa/classifieds — HEALTHCHECK + HTTP_HOST/HTTP_PORT

**Why:** classifieds is the one non-Magento app that already carries an absolute base URL —
`CLASSIFIEDS=http://127.0.0.1:9980/`, read at runtime by `config.php` to resolve `WEB_PATH`. That
env var is exactly `HTTP_HOST`/`HTTP_PORT` under a different name, hardcoded in two places
(the image's `ENV` and `images/vwa/compose.yml`). This task makes it derived.

**Files:**
- Modify: `images/vwa/classifieds/Dockerfile` (final stage, lines ~186-192)
- Create: `images/vwa/classifieds/entrypoint.sh`
- Modify: `images/vwa/classifieds/image.toml`
- Modify: `images/vwa/compose.yml`

**Interfaces:**
- Consumes: the validation block from Task 2's `entrypoint.sh` (repeated below in full — do not
  go read the other file, and do not factor it into `builder/`: a shared file makes every push
  rebuild the whole fleet, which is why these are deliberately per-image copies).
- Produces: `CLASSIFIEDS` derived from `HTTP_HOST`/`HTTP_PORT` at runtime.

- [ ] **Step 1: Confirm curl really is in the final image**

The table in this plan lists classifieds as inferred, not measured. Settle it first:

```bash
docker run --rm --entrypoint sh ghcr.io/shkolnik/vwa-classifieds:latest \
  -c 'command -v curl || echo MISSING'
```

If `MISSING`, add `curl` to the runtime stage's `apt-get install` list in the same commit and note
it in the commit message. (The Dockerfile installs curl at lines ~42 and ~74, so it is expected
present — but verify, do not assume.)

- [ ] **Step 2: Write the entrypoint**

Create `images/vwa/classifieds/entrypoint.sh`:

```bash
#!/bin/bash
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container. They
# are NOT the address the server binds — nginx's listen port is fixed inside the
# image — so publishing on a different port (-p 8080:9980 with HTTP_PORT=8080)
# is a supported thing to do.
#
# Osclass needs this: config.php resolves WEB_PATH from the CLASSIFIEDS
# variable, and WEB_PATH is what the app emits into links and asset URLs. That
# value used to be hardcoded to http://127.0.0.1:9980/ in BOTH the image's ENV
# and the compose file, which is a wrong hostname waiting to happen the first
# time anything is published anywhere else.
set -euo pipefail

missing=()
[ -n "${HTTP_HOST:-}" ] || missing+=(HTTP_HOST)
[ -n "${HTTP_PORT:-}" ] || missing+=(HTTP_PORT)
if [ "${#missing[@]}" -gt 0 ]; then
  cat >&2 <<EOF
error: ${missing[*]} not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container: the PUBLISHED
  side of the port mapping, not the port the server listens on inside. Osclass
  bakes them into every link and asset URL it serves.

  Run it like:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=9980 -p 9980:9980 <image>

  Publishing on another port is fine — match HTTP_PORT to the PUBLISHED side:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=8080 -p 8080:9980 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

# config.php reads this with getenv() on every request, so exporting it here is
# the whole configuration step — there is no cached copy to invalidate.
export CLASSIFIEDS="http://${HTTP_HOST}:${HTTP_PORT}/"
echo "serving ${CLASSIFIEDS} (listening on 9980 in-container)"
exec supervisord -c /etc/supervisor/supervisord.conf -n
```

- [ ] **Step 3: Rework the Dockerfile's final stage**

Replace the `ENV CLASSIFIEDS=... RESET_TOKEN=...` / `EXPOSE` / `CMD` block at the end of
`images/vwa/classifieds/Dockerfile` with:

```dockerfile
# RESET_TOKEN is read by the reset controller and is genuinely a constant, so it
# stays an ENV. CLASSIFIEDS is NOT: it is this fleet's HTTP_HOST/HTTP_PORT under
# an app-specific name, and the entrypoint derives it. Leaving a default here
# would mean a silently-ignored HTTP_HOST still serves the old links.
ENV RESET_TOKEN=4b61655535e7ed388f0d40a93600254c

EXPOSE 9980
# --chmod so the mode is a property of the BUILD, not of the checkout.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
# Not supervisord directly: HTTP_HOST/HTTP_PORT are validated, and CLASSIFIEDS
# derived, before anything serves a page.
CMD ["/entrypoint.sh"]

# localhost:9980 is the IN-CONTAINER address. Osclass boots MySQL and PHP-FPM
# under supervisord, so give it a real start period; the per-probe --timeout is
# the parameter that decides whether a slow first page reads as unhealthy.
HEALTHCHECK --start-period=90s --interval=10s --timeout=15s --retries=3 \
  CMD curl -fsS -L --max-time 14 http://localhost:9980/ >/dev/null || exit 1
```

⚠️ Add `# syntax=docker/dockerfile:1` as line 1 of this Dockerfile if it is not already there —
`COPY --chmod` silently fails without it on older frontends.

- [ ] **Step 4: Move the variables into compose**

In `images/vwa/compose.yml`, replace the `environment:` block:

```yaml
services:
  classifieds:
    image: ghcr.io/shkolnik/vwa-classifieds:latest
    # Required, no defaults — see docs/service-contract.md. The entrypoint
    # derives CLASSIFIEDS (Osclass's WEB_PATH) from these, so they are the ONLY
    # place the served hostname is written down. 127.0.0.1 rather than localhost
    # is deliberate: the image is built naming localhost, so a silently-ignored
    # HTTP_HOST fails the gate instead of passing on the build-time value.
    environment:
      HTTP_HOST: 127.0.0.1
      HTTP_PORT: "9980"
      RESET_TOKEN: 4b61655535e7ed388f0d40a93600254c
    ports:
      - "9980:9980"
```

- [ ] **Step 5: Build and prove all three behaviors**

```bash
cd ~/workspace/browser-use-benchmarks
bin/build build vwa/classifieds --datasets-dir "$HOME/benchmark-datasets"

# (a) fails loud with no variables
docker run --rm ghcr.io/shkolnik/vwa-classifieds:latest 2>&1 | head -n 5

# (b) becomes healthy and serves the configured host
cid=$(docker run -d -e HTTP_HOST=127.0.0.1 -e HTTP_PORT=9980 -p 9980:9980 \
        ghcr.io/shkolnik/vwa-classifieds:latest)
for i in $(seq 1 60); do
  st=$(docker inspect --format '{{json .State.Health}}' "$cid" | python3 -c \
       'import json,sys; print((json.load(sys.stdin) or {}).get("Status","none"))')
  echo "health=$st"; [ "$st" = healthy ] && break; sleep 5
done

# (c) THE ONE THAT MATTERS: the emitted links carry the configured host, not a
# baked-in one. This is the check that would have caught #68 on the Magento
# images months earlier.
curl -fsS http://127.0.0.1:9980/ | grep -oE 'https?://[^"/]+' | sort -u | head
docker rm -f "$cid"
```

Expected for (c): every absolute URL names `127.0.0.1:9980`. **Any occurrence of a different host
means `CLASSIFIEDS` did not take effect — stop and fix.**

- [ ] **Step 6: Prove the variable is really load-bearing**

```bash
cid=$(docker run -d -e HTTP_HOST=example.invalid -e HTTP_PORT=9980 -p 9980:9980 \
        ghcr.io/shkolnik/vwa-classifieds:latest)
sleep 90
curl -fsS http://127.0.0.1:9980/ | grep -oE 'https?://[^"/]+' | sort -u | head
docker rm -f "$cid"
```

Expected: the links now name `example.invalid:9980`. If they still say `127.0.0.1`, the value is
being ignored and the gate is decorative.

- [ ] **Step 7: Commit — two commits**

```bash
git add images/vwa/classifieds/entrypoint.sh images/vwa/classifieds/Dockerfile images/vwa/compose.yml
git commit -m "classifieds: derive CLASSIFIEDS from HTTP_HOST/HTTP_PORT

WEB_PATH came from a value hardcoded in two places (the image ENV and the
compose file). Osclass emits it into every link and asset URL, so a deployment
that moved would have served links pointing at the old address."

git add images/vwa/classifieds/Dockerfile images/vwa/classifieds/image.toml
git commit -m "classifieds: declare a HEALTHCHECK so \`up --wait\` gates on readiness"
```

---

### Task 4: webarena/reddit — HEALTHCHECK + HTTP_HOST/HTTP_PORT

**Why:** reddit is the row where the in-container port (80) and the published port (9999)
genuinely differ, so it is the one image where copying `[service].healthcheck` into a `HEALTHCHECK`
line would silently probe a port nothing listens on.

⚠️ **This task and Task 8 both edit `images/webarena/compose.yml`. Run them sequentially, Task 4
first.**

**Files:**
- Modify: `images/webarena/reddit/Dockerfile` (final stage, lines ~204-232)
- Create: `images/webarena/reddit/entrypoint.sh`
- Modify: `images/webarena/reddit/image.toml`
- Modify: `images/webarena/compose.yml` (the `reddit` service only — leave the others alone)

**Interfaces:**
- Consumes: the validation block pattern from Task 2 (repeated below in full).
- Produces: nothing other tasks read.

- [ ] **Step 1: Find out what Postmill actually does with the host — do not guess**

Symfony apps usually emit *relative* URLs but can be configured with a hard `router.request_context`
or `trusted_hosts`. Establish the truth before writing any config:

```bash
cid=$(docker run -d -p 9999:80 ghcr.io/shkolnik/webarena-reddit:latest)
sleep 45
# Absolute URLs in the served HTML, if any:
curl -fsS http://127.0.0.1:9999/forums | grep -oE 'https?://[^"/]+' | sort -u | head
# Does a different Host header change what it serves?
curl -fsS -H 'Host: example.invalid' http://127.0.0.1:9999/forums \
  | grep -oE 'https?://[^"/]+' | sort -u | head
docker exec "$cid" sh -c 'grep -rn "request_context\|trusted_hosts\|router.default_uri" /app/config 2>/dev/null | head'
docker rm -f "$cid"
```

**Record the output in the commit message.** Two possible outcomes, and they lead to different
Step 3s:
- **Relative-only (expected):** the entrypoint validates and reports, and changes no app config —
  exactly like miniwob. This is the lean.
- **Absolute URLs found:** the entrypoint must additionally set the router context. Write that
  config only if this step proves it is needed; do not add it speculatively.

- [ ] **Step 2: Write the entrypoint**

Create `images/webarena/reddit/entrypoint.sh`. Note `/bin/sh` — this is an alpine image:

```bash
#!/bin/sh
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container. They
# are NOT the address the server binds — nginx listens on 80 inside this image,
# and compose publishes that on 9999. Those are different numbers on purpose,
# and this is the image where confusing them is easiest.
set -eu

missing=
[ -n "${HTTP_HOST:-}" ] || missing="$missing HTTP_HOST"
[ -n "${HTTP_PORT:-}" ] || missing="$missing HTTP_PORT"
if [ -n "$missing" ]; then
  cat >&2 <<EOF
error:$missing not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container: the PUBLISHED
  side of the port mapping. This image listens on 80 INSIDE the container, so
  the published port is normally a different number.

  Run it like:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=9999 -p 9999:80 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

echo "serving http://${HTTP_HOST}:${HTTP_PORT}/ (listening on 80 in-container)"
exec supervisord -c /etc/supervisord.conf -n
```

- [ ] **Step 3: Rework the Dockerfile's final stage**

Replace the trailing `EXPOSE 80` / `CMD [...]` in `images/webarena/reddit/Dockerfile` with:

```dockerfile
EXPOSE 80
# --chmod so the mode is a property of the BUILD, not of the checkout.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
CMD ["/entrypoint.sh"]

# ⚠️ localhost:80, NOT 9999. The healthcheck runs INSIDE the container, so it
# uses the listen port; 9999 is the published side and nothing is bound to it in
# here. This is the only image in the fleet where the two differ, which makes it
# the easiest one to get wrong by copying image.toml's URL.
#
# /forums, not /: a document root nested one level too deep still answers / and
# 404s every real page — the exact defect that shipped once already.
# Postgres + php-fpm + nginx come up under supervisord, hence the start period.
HEALTHCHECK --start-period=90s --interval=10s --timeout=15s --retries=3 \
  CMD curl -fsS -L --max-time 14 http://localhost:80/forums >/dev/null || exit 1
```

Add `# syntax=docker/dockerfile:1` as line 1 if absent.

- [ ] **Step 4: Add the variables to compose**

In `images/webarena/compose.yml`, the `reddit` service becomes — **touch no other service**:

```yaml
  reddit:
    image: ghcr.io/shkolnik/webarena-reddit:latest
    # Required, no defaults — see docs/service-contract.md. Note the asymmetry:
    # HTTP_PORT is the PUBLISHED port (9999); the container listens on 80.
    environment:
      HTTP_HOST: 127.0.0.1
      HTTP_PORT: "9999"
    ports:
      - "9999:80"
```

- [ ] **Step 5: Build and prove it**

```bash
cd ~/workspace/browser-use-benchmarks
bin/build build webarena/reddit --datasets-dir "$HOME/benchmark-datasets"

docker run --rm ghcr.io/shkolnik/webarena-reddit:latest 2>&1 | head -n 5   # must fail loud

cid=$(docker run -d -e HTTP_HOST=127.0.0.1 -e HTTP_PORT=9999 -p 9999:80 \
        ghcr.io/shkolnik/webarena-reddit:latest)
for i in $(seq 1 40); do
  st=$(docker inspect --format '{{json .State.Health}}' "$cid" | python3 -c \
       'import json,sys; print((json.load(sys.stdin) or {}).get("Status","none"))')
  echo "health=$st"; [ "$st" = healthy ] && break; sleep 5
done
curl -fsS -o /dev/null -w 'published /forums -> %{http_code}\n' http://127.0.0.1:9999/forums
docker rm -f "$cid"
```

Expected: fails loud without vars; reaches `healthy`; published `/forums` returns `200`.

- [ ] **Step 6: Prove the healthcheck has teeth**

The defect this must catch is the document root nested one level too deep — the bug that actually
shipped. Regress **production**:

```bash
cd ~/workspace/browser-use-benchmarks
cp images/webarena/reddit/nginx.conf /tmp/hcplan-reddit-nginx.bak
sed -i 's|root /app/public;|root /app/public/public;|' images/webarena/reddit/nginx.conf
bin/build build webarena/reddit --datasets-dir "$HOME/benchmark-datasets"
cid=$(docker run -d -e HTTP_HOST=127.0.0.1 -e HTTP_PORT=9999 ghcr.io/shkolnik/webarena-reddit:latest)
sleep 120
docker inspect --format '{{json .State.Health}}' "$cid" | head -c 300; echo
docker rm -f "$cid"
cp /tmp/hcplan-reddit-nginx.bak images/webarena/reddit/nginx.conf
bin/build build webarena/reddit --datasets-dir "$HOME/benchmark-datasets"
```

Expected: `"Status":"unhealthy"`. Confirm the exact `root` string first with
`grep -n 'root ' images/webarena/reddit/nginx.conf` and adjust the `sed` to match.

- [ ] **Step 7: Commit — two commits**

```bash
git add images/webarena/reddit/entrypoint.sh images/webarena/reddit/Dockerfile images/webarena/compose.yml
git commit -m "reddit: require HTTP_HOST/HTTP_PORT rather than guess a hostname

Postmill's URL behavior recorded in Step 1: <PASTE THE MEASURED OUTPUT HERE>."

git add images/webarena/reddit/Dockerfile images/webarena/reddit/image.toml
git commit -m "reddit: declare a HEALTHCHECK so \`up --wait\` gates on readiness

Probes localhost:80 — the IN-CONTAINER listen port — not the published 9999.
Reddit is the only image where those differ, so it is the one where copying
image.toml's URL into the Dockerfile would probe a dead port."
```

---

### Task 5: webarena/shopping — HEALTHCHECK only

**Why:** shopping already ships the HTTP_HOST/HTTP_PORT contract (PR #6). It needs only the
readiness half — and it is the image whose readiness is least like a socket opening: the build
deliberately never runs `setup:di:compile`, so Magento compiles generated classes on the **first
request**, which is minutes of PHP. That budget currently lives in `healthcheck_timeout_s = 900`
and must move into `--start-period`, because Task 9 deletes the manifest key.

**Files:**
- Modify: `images/webarena/shopping/Dockerfile`
- Modify: `images/webarena/shopping/image.toml`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Add the HEALTHCHECK**

In `images/webarena/shopping/Dockerfile`, immediately after the `CMD ["/entrypoint.sh"]` line in
the runtime stage:

```dockerfile
# The 900s start period is not padding. This build deliberately never runs
# setup:di:compile (the dataset has no core setup_module rows), so the
# generated/ classes do not exist in the image and Magento compiles them on the
# FIRST request — minutes of PHP. Boot itself is quick; mariadb and
# elasticsearch answered in 10s during the build.
#
# --timeout is the decisive parameter, not the total budget: a probe that gives
# up after 10s against a first request that takes minutes can never succeed at
# ANY number of retries. Docker never overlaps probes, so a long timeout is
# safe. Failures during the start period do not count toward --retries, and a
# success during it marks the container healthy immediately.
#
# -L is load-bearing: `curl -f` fails only on >=400, so an unfollowed 302 would
# pass against a container that rendered nothing.
HEALTHCHECK --start-period=900s --interval=15s --timeout=120s --retries=3 \
  CMD curl -fsS -L --max-time 115 http://localhost:7770/ >/dev/null || exit 1
```

- [ ] **Step 2: Move the budget out of the manifest**

`images/webarena/shopping/image.toml`'s `[service]` block becomes:

```toml
[service]
# The PUBLISHED address the smoke gate reaches this service at. Readiness is the
# Dockerfile HEALTHCHECK's job (900s start period, for Magento's first-request
# DI compile); this is a reachability check only, and its budget is the
# fleet-wide default.
healthcheck = "http://127.0.0.1:7770/"
```

Delete the `healthcheck_timeout_s = 900` line and the comment above it — the reasoning it carried
now lives beside the `HEALTHCHECK` in the Dockerfile, where the number actually takes effect.

- [ ] **Step 3: Verify**

⚠️ **Known constraint:** shopping could not be built on this host —
`datasets/shopping_media.tar` is absent. If that is still true, do **not** fake a verification.
Instead:

```bash
ls -l "$HOME/benchmark-datasets"/shopping_*.{tar,gz,php} 2>&1
```

If the inputs are absent, record in the commit message that this image's healthcheck is
**unverified locally and first exercised by CI**, exactly as its `healthcheck_timeout_s = 900` was.
Do not claim otherwise. If the inputs ARE present, build and verify as in Task 3 Step 5.

- [ ] **Step 4: Commit**

```bash
git add images/webarena/shopping/Dockerfile images/webarena/shopping/image.toml
git commit -m "shopping: declare a HEALTHCHECK and retire the driver-side timeout

The 900s budget moves from image.toml into --start-period, where readiness now
lives. --timeout=120s is the parameter that matters: Magento compiles its
generated/ classes on the first request, and a 10s probe against a
multi-minute first request fails at any total budget.

<STATE HERE whether this was verified locally or is first exercised by CI.>"
```

---

### Task 6: webarena/shopping-admin — HEALTHCHECK only

**Why:** same as Task 5, with one extra wrinkle — the probed path is `/admin`, and Magento's
adminhtml is **host-pinned** to the configured base_url (#70): other Hosts fall through to the
frontend area and render its 404. So this image's healthcheck is also, incidentally, a check that
the entrypoint's base_url write took effect.

**Files:**
- Modify: `images/webarena/shopping-admin/Dockerfile`
- Modify: `images/webarena/shopping-admin/image.toml`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Establish what `/admin` actually returns in-container**

`/admin` redirects to a login page, and the in-container probe uses `localhost` while the
entrypoint configures base_url from `HTTP_HOST` — which compose sets to `127.0.0.1`. Given #70,
`localhost` and `127.0.0.1` may not be interchangeable here. **Measure before choosing the URL:**

```bash
cid=$(docker run -d -e HTTP_HOST=127.0.0.1 -e HTTP_PORT=7780 -p 7780:7780 \
        ghcr.io/shkolnik/webarena-shopping-admin:latest)
sleep 300   # first-request DI compile
for h in localhost 127.0.0.1; do
  echo "--- in-container, Host=$h ---"
  docker exec "$cid" curl -fsS -L -o /dev/null -w '%{http_code} %{url_effective}\n' \
    "http://$h:7780/admin" || echo "FAILED"
done
docker rm -f "$cid"
```

**Use whichever host answers 200 in the HEALTHCHECK below.** If `localhost` 404s and `127.0.0.1`
works, that is #70 showing up in-container, and the healthcheck must name `127.0.0.1` — write a
comment saying so.

- [ ] **Step 2: Add the HEALTHCHECK**

In `images/webarena/shopping-admin/Dockerfile`, after `CMD ["/entrypoint.sh"]` — substituting the
host Step 1 proved:

```dockerfile
# Same first-request DI compile as ../shopping: this build never runs
# setup:di:compile, so Magento generates its classes on the first request and
# --timeout, not the total budget, is what decides whether that reads as
# unhealthy. Docker never overlaps probes, so a long timeout is safe.
#
# ⚠️ The host below is not arbitrary. Magento's adminhtml is pinned to the
# configured base_url (unlike the storefront, which serves any Host after
# redirect_to_base=0) — other hosts fall through to the frontend area and render
# ITS 404, with a 200-shaped path that -f would not catch. Measured in-container
# before this was written.
HEALTHCHECK --start-period=900s --interval=15s --timeout=120s --retries=3 \
  CMD curl -fsS -L --max-time 115 http://127.0.0.1:7780/admin >/dev/null || exit 1
```

- [ ] **Step 3: Move the budget out of the manifest**

`images/webarena/shopping-admin/image.toml`'s `[service]` block becomes:

```toml
[service]
# The PUBLISHED address the smoke gate reaches this service at. Readiness is the
# Dockerfile HEALTHCHECK's job; this is a reachability check only.
healthcheck = "http://127.0.0.1:7780/admin"
```

Delete `healthcheck_timeout_s = 900` and its comment.

- [ ] **Step 4: Verify the container reaches healthy**

```bash
cid=$(docker run -d -e HTTP_HOST=127.0.0.1 -e HTTP_PORT=7780 -p 7780:7780 \
        ghcr.io/shkolnik/webarena-shopping-admin:latest)
for i in $(seq 1 80); do
  st=$(docker inspect --format '{{json .State.Health}}' "$cid" | python3 -c \
       'import json,sys; print((json.load(sys.stdin) or {}).get("Status","none"))')
  echo "$((i*15))s health=$st"; [ "$st" = healthy ] && break; sleep 15
done
docker inspect --format '{{json .State.Health}}' "$cid" | head -c 400; echo
docker rm -f "$cid"
```

Expected: `healthy`. **Record the observed time-to-healthy in the commit message** — it is the
first real measurement of this image's readiness and it replaces a guessed 900.

- [ ] **Step 5: Commit**

```bash
git add images/webarena/shopping-admin/Dockerfile images/webarena/shopping-admin/image.toml
git commit -m "shopping-admin: declare a HEALTHCHECK and retire the driver-side timeout

Probes /admin at the host the base_url names: adminhtml is host-pinned (#70),
so a different host renders the frontend 404 rather than failing outright.
Measured time-to-healthy: <FILL IN>s."
```

---

### Task 7: webshop/server — HTTP_HOST/HTTP_PORT (its HEALTHCHECK already ships)

**Why:** webshop already has a tuned HEALTHCHECK (merged `1a2fde5`). It needs the published-address
half. WebShop's Flask app is the fleet's one *exclusively relative* app — no `_external`,
`url_root`, or `host_url` anywhere — so nothing is rewritten, and the variables are required for
uniformity and fail-loud, exactly as with miniwob.

**Files:**
- Modify: `images/webshop/server/Dockerfile`
- Create: `images/webshop/server/entrypoint.sh`
- Modify: `images/webshop/server/image.toml`
- Modify: `images/webshop/compose.yml`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Re-confirm the relative-URL claim before relying on it**

```bash
docker run --rm --entrypoint sh ghcr.io/shkolnik/webshop-server:latest -c \
  'grep -rn "_external\|url_root\|host_url\|SERVER_NAME" /webshop/web_agent_site | head'
```

Expected: no matches. **If there are matches, the entrypoint must configure them** — extend this
task rather than proceeding as if it were validation-only.

- [ ] **Step 2: Write the entrypoint**

Create `images/webshop/server/entrypoint.sh`:

```bash
#!/bin/bash
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container. They
# are NOT the address the server binds — Flask listens on 3000 inside the image.
#
# WebShop emits exclusively RELATIVE URLs (no _external / url_root / host_url
# anywhere in web_agent_site), so nothing here is rewritten from these values.
# They are still REQUIRED: the contract is uniform across the fleet so an
# operator never has to remember which images care, and the failure this
# prevents — an image quietly serving somebody else's hostname — is one this
# fleet has already shipped once.
set -euo pipefail

missing=()
[ -n "${HTTP_HOST:-}" ] || missing+=(HTTP_HOST)
[ -n "${HTTP_PORT:-}" ] || missing+=(HTTP_PORT)
if [ "${#missing[@]}" -gt 0 ]; then
  cat >&2 <<EOF
error: ${missing[*]} not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container: the PUBLISHED
  side of the port mapping, not the port the server listens on inside.

  Run it like:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=3000 -p 3000:3000 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

echo "serving http://${HTTP_HOST}:${HTTP_PORT}/ (listening on 3000 in-container)"
exec python -m web_agent_site.app
```

- [ ] **Step 3: Wire it in**

In `images/webshop/server/Dockerfile`, replace `CMD ["python", "-m", "web_agent_site.app"]` with:

```dockerfile
# --chmod so the mode is a property of the BUILD, not of the checkout.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
CMD ["/entrypoint.sh"]
```

Leave the existing `HEALTHCHECK` block and its comment exactly as they are — that comment records
the measured 57s/14.3G first load and is the reference explanation for the whole fleet's
`--timeout` reasoning.

Add `# syntax=docker/dockerfile:1` as line 1 if absent.

- [ ] **Step 4: Add the variables to compose**

```yaml
# WebShop upstream-standard port 3000 (web_agent_site/app.py); unique across benchmarks
# (miniwob 8399; webarena 7770/7780/9999/8023).
services:
  server:
    image: ghcr.io/shkolnik/webshop-server:latest
    # Required, no defaults — see docs/service-contract.md.
    environment:
      HTTP_HOST: 127.0.0.1
      HTTP_PORT: "3000"
    ports:
      - "3000:3000"
```

- [ ] **Step 5: Drop the now-dead manifest key**

`images/webshop/server/image.toml`'s `[service]` block becomes:

```toml
[service]
# The PUBLISHED address the smoke gate reaches this service at. Readiness is the
# Dockerfile HEALTHCHECK's job — see the long comment beside it for the measured
# 57s / 14.3G first-request load that sets its --timeout.
healthcheck = "http://localhost:3000/"
```

Delete `healthcheck_timeout_s = 900` and the paragraph above it. ⚠️ That paragraph currently says
the number is PROVISIONAL and unmeasured; the Dockerfile's comment carries the measurement now, so
nothing is lost — but read both before deleting, and keep any fact that exists in only one of them.

- [ ] **Step 6: Verify**

⚠️ **This image is 17.3GB and its first request costs ~14 GiB RSS.** The host has 39GB and
**no swap** — nothing else heavy may run concurrently. Check for the LMDB spike or a fleet build
first (`docker ps`), and serialize.

```bash
cd ~/workspace/browser-use-benchmarks
bin/build build webshop/server --datasets-dir "$HOME/benchmark-datasets"
docker run --rm ghcr.io/shkolnik/webshop-server:latest 2>&1 | head -n 5   # must fail loud

cid=$(docker run -d -e HTTP_HOST=127.0.0.1 -e HTTP_PORT=3000 -p 3000:3000 \
        ghcr.io/shkolnik/webshop-server:latest)
for i in $(seq 1 40); do
  st=$(docker inspect --format '{{json .State.Health}}' "$cid" | python3 -c \
       'import json,sys; print((json.load(sys.stdin) or {}).get("Status","none"))')
  echo "$((i*10))s health=$st"; [ "$st" = healthy ] && break; sleep 10
done
docker rm -f "$cid"
```

Expected: fails loud without vars; reaches `healthy`. Record time-to-healthy.

- [ ] **Step 7: Commit**

```bash
git add images/webshop/server/entrypoint.sh images/webshop/server/Dockerfile images/webshop/compose.yml
git commit -m "webshop: require HTTP_HOST/HTTP_PORT rather than guess a hostname

WebShop emits exclusively relative URLs, so nothing is rewritten from these —
they are required so the fleet contract is uniform and a missing hostname
fails loud instead of being assumed."

git add images/webshop/server/image.toml
git commit -m "webshop: retire the driver-side healthcheck timeout

Readiness is the Dockerfile HEALTHCHECK's job; the measured 57s/14.3G first
load is documented beside it, where the number takes effect."
```

---

### Task 8: webarena/gitlab — HTTP_HOST/HTTP_PORT (its HEALTHCHECK already ships)

**Why:** gitlab is the hard one, and the only image where the contract may not be cheaply
satisfiable. Its `external_url` is baked into `/etc/gitlab/gitlab.rb` at port 8023 by the restore,
and GitLab emits absolute URLs from it. Changing it requires `gitlab-ctl reconfigure`, which is
minutes of chef and is precisely what this image avoids at boot (it runs `runsvdir-start` directly,
which is what keeps boot fast — see the Dockerfile's `/opt/gitlab` comment).

⚠️ **Run after Task 4 — both edit `images/webarena/compose.yml`.**

**⚠️ DECISION REQUIRED — do not decide this alone.** Two options; my lean is (A):

- **(A) Validate-and-refuse (lean).** The entrypoint requires both variables and **hard-fails if
  they do not match the baked `external_url`**. Cost: gitlab cannot be republished on a different
  address without a rebuild. Benefit: it is honest — the alternative to refusing is serving links
  that point somewhere else, which is #68 verbatim. It also keeps boot fast, which #65 (unhealthy
  after 1107s) says we cannot afford to spend.
- **(B) Reconfigure-on-mismatch.** The entrypoint rewrites `gitlab.rb` and runs
  `gitlab-ctl reconfigure` when the requested address differs. Cost: minutes added to boot on the
  mismatch path, against an image already fighting a 900s start period and an unexplained
  unhealthy-at-1107s failure. Benefit: the contract holds fully.

**Escalate this choice to James before implementing.** If unanswered, implement (A) — it is
strictly safer and (B) remains available later.

**Files:**
- Modify: `images/webarena/gitlab/Dockerfile`
- Create: `images/webarena/gitlab/entrypoint.sh`
- Modify: `images/webarena/compose.yml` (the `gitlab` service only)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Read the baked address out of the image — do not assume it is 8023**

```bash
docker run --rm --entrypoint sh ghcr.io/shkolnik/webarena-gitlab:latest \
  -c "grep -n '^external_url' /etc/gitlab/gitlab.rb"
```

Record the exact value. Everything below compares against it.

- [ ] **Step 2: Write the entrypoint (option A)**

Create `images/webarena/gitlab/entrypoint.sh`:

```bash
#!/bin/bash
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container.
#
# gitlab is the one image that cannot simply ADOPT them. external_url is baked
# into /etc/gitlab/gitlab.rb by the restore, GitLab emits absolute URLs from it,
# and changing it means `gitlab-ctl reconfigure` — minutes of chef on an image
# that deliberately runs runsvdir-start directly to keep boot fast.
#
# So this refuses rather than lies. Serving links that point at an address the
# operator did not ask for is exactly the failure this fleet already shipped
# once (the dataset arrived naming metis.lti.cs.cmu.edu and silently 302'd every
# request off-host). A loud refusal at startup is strictly better than that.
set -euo pipefail

missing=()
[ -n "${HTTP_HOST:-}" ] || missing+=(HTTP_HOST)
[ -n "${HTTP_PORT:-}" ] || missing+=(HTTP_PORT)
if [ "${#missing[@]}" -gt 0 ]; then
  cat >&2 <<EOF
error: ${missing[*]} not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container.

  Run it like:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=8023 -p 8023:8023 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

WANT="http://${HTTP_HOST}:${HTTP_PORT}"
BAKED=$(sed -n "s/^external_url *['\"]\\(.*\\)['\"].*/\\1/p" /etc/gitlab/gitlab.rb | head -n1)
BAKED=${BAKED%/}
if [ "$WANT" != "$BAKED" ]; then
  cat >&2 <<EOF
error: this image is built to serve ${BAKED}, but HTTP_HOST/HTTP_PORT ask for ${WANT}.

  GitLab bakes external_url into /etc/gitlab/gitlab.rb and emits absolute URLs
  from it. Serving under a different address would hand agents links pointing at
  ${BAKED} — so this refuses instead of misleading them.

  Either publish it at ${BAKED}, or rebuild the image with a different
  external_url. Reconfiguring at boot is possible but costs minutes of
  \`gitlab-ctl reconfigure\` on every start; see
  docs/superpowers/plans/2026-08-07-fleet-service-contract-standardization.md Task 8.
EOF
  exit 1
fi

echo "serving ${WANT}"
exec /opt/gitlab/embedded/bin/runsvdir-start
```

- [ ] **Step 3: Wire it in**

Append to `images/webarena/gitlab/Dockerfile`, after the existing `HEALTHCHECK` block:

```dockerfile
# --chmod so the mode is a property of the BUILD, not of the checkout.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
# Replaces the `command:` compose used to pass. runsvdir-start is still what
# actually runs — the entrypoint validates the address contract and execs it.
CMD ["/entrypoint.sh"]
```

Add `# syntax=docker/dockerfile:1` as line 1 if absent.

- [ ] **Step 4: Update compose — and drop the now-redundant `command:`**

```yaml
  gitlab:
    image: ghcr.io/shkolnik/webarena-gitlab:latest
    # Required, no defaults — see docs/service-contract.md. gitlab's
    # external_url is baked in, so these must MATCH it; the entrypoint refuses
    # rather than serve links pointing somewhere else.
    environment:
      HTTP_HOST: localhost
      HTTP_PORT: "8023"
    ports:
      - "8023:8023"
```

⚠️ `HTTP_HOST: localhost`, **not** `127.0.0.1` — it must equal the baked `external_url` host from
Step 1, or option (A) refuses to start. This is the one service that cannot use the
deliberately-different host the other webarena services use. Note that in the commit message.

⚠️ The `command: /opt/gitlab/embedded/bin/runsvdir-start` line is **deleted** — the entrypoint
execs it now. Leaving it would override the entrypoint and skip all validation.

- [ ] **Step 5: Verify all three paths**

```bash
cd ~/workspace/browser-use-benchmarks
bin/build build webarena/gitlab --datasets-dir "$HOME/benchmark-datasets"

# (a) no variables -> loud refusal
docker run --rm ghcr.io/shkolnik/webarena-gitlab:latest 2>&1 | head -n 5
# (b) MISMATCHED address -> loud refusal naming both addresses
docker run --rm -e HTTP_HOST=example.invalid -e HTTP_PORT=9999 \
  ghcr.io/shkolnik/webarena-gitlab:latest 2>&1 | head -n 8
# (c) matching address -> boots and becomes healthy
cid=$(docker run -d -e HTTP_HOST=localhost -e HTTP_PORT=8023 -p 8023:8023 \
        ghcr.io/shkolnik/webarena-gitlab:latest)
for i in $(seq 1 100); do
  st=$(docker inspect --format '{{json .State.Health}}' "$cid" | python3 -c \
       'import json,sys; print((json.load(sys.stdin) or {}).get("Status","none"))')
  echo "$((i*15))s health=$st"; [ "$st" = healthy ] && break; sleep 15
done
# Task 1's dump is the point: if it never goes healthy, this now says WHY (#65).
python3 - <<'PY'
import sys; sys.path.insert(0, "/home/agent/workspace/browser-use-benchmarks")
from pathlib import Path
from builder.docker import dump_health_log
dump_health_log(Path("images/webarena/compose.yml"), "gitlab")
PY
docker rm -f "$cid"
```

**If (c) never reaches healthy, that is #65 reproducing — capture the health log and STOP.** It is
a finding, not a blocker to work around: report it and do not tune the numbers to make it pass.

- [ ] **Step 6: Commit**

```bash
git add images/webarena/gitlab/entrypoint.sh images/webarena/gitlab/Dockerfile images/webarena/compose.yml
git commit -m "gitlab: require HTTP_HOST/HTTP_PORT and refuse a mismatched address

gitlab cannot adopt an arbitrary address the way the other images can:
external_url is baked into gitlab.rb, GitLab emits absolute URLs from it, and
changing it needs \`gitlab-ctl reconfigure\` — minutes of chef on an image that
runs runsvdir-start directly to keep boot fast. Refusing loudly beats serving
links that point somewhere else, which is the failure this fleet already
shipped once.

compose's \`command:\` is dropped; the entrypoint execs runsvdir-start after
validating. HTTP_HOST is localhost here, not 127.0.0.1, because it must equal
the baked external_url."
```

---

### Task 9: Make the driver depend on the container healthcheck

**Why:** the finish line. With every image declaring a `HEALTHCHECK`, `up --wait` genuinely owns
readiness, and `poll_health`'s parallel readiness budget is redundant *and* misleading — its
hardcoded 10s per attempt against webshop's 57s first request could never succeed at any total
budget, and each attempt queued another connection against a single-threaded dev server until the
backlog was exhausted, surfacing as `Connection refused` and pointing the investigation at
publishing.

**But `poll_health` must not simply be deleted.** `up --wait` evaluates health *inside* the
container; `poll_health` probes from the *host, through the published port mapping*. #66 was
exactly a published-path failure: nginx healthy in-container while the host got `ECONNREFUSED` on
7770 for 901s. So it becomes a thin **reachability** check with a small budget.

⚠️ **Must land after Tasks 2-8.** It fails any image with no `HEALTHCHECK`.

**Files:**
- Modify: `builder/docker.py`
- Modify: `builder/manifest.py`
- Test: `tests/test_docker.py`, `tests/test_manifest.py`

**Interfaces:**
- Consumes: `container_health` / `dump_health_log` from Task 1.
- Produces:
  - `compose_service_image(compose: Path, service: str, check_output=...) -> str`
  - `image_healthcheck(image: str, check_output=...) -> dict | None`
  - `Manifest.reachability_timeout_s: int = 30` (replaces `healthcheck_timeout_s`)

- [ ] **Step 1: Write the failing tests**

`tests/test_manifest.py`:

```python
def test_retired_healthcheck_timeout_fails_loud(tmp_path):
    # Silently ignoring it would leave a 900 in the file doing nothing while
    # everyone assumed it was still the budget.
    (tmp_path / "image.toml").write_text(
        '[service]\nhealthcheck = "http://x/"\nhealthcheck_timeout_s = 900\n')
    try:
        load_manifest(tmp_path)
    except SystemExit as e:
        assert "healthcheck_timeout_s" in str(e)
        assert "HEALTHCHECK" in str(e)
    else:
        raise AssertionError("a retired key was accepted silently")

def test_reachability_timeout_defaults_and_overrides(tmp_path):
    (tmp_path / "image.toml").write_text('[service]\nhealthcheck = "http://x/"\n')
    assert load_manifest(tmp_path).reachability_timeout_s == 30
    (tmp_path / "image.toml").write_text(
        '[service]\nhealthcheck = "http://x/"\nreachability_timeout_s = 90\n')
    assert load_manifest(tmp_path).reachability_timeout_s == 90
```

`tests/test_docker.py`:

```python
def test_image_healthcheck_is_None_when_absent(tmp_path):
    assert docker_mod.image_healthcheck("img", check_output=lambda c, text=True: "null\n") is None

def test_image_healthcheck_parses_the_config(tmp_path):
    out = '{"Test":["CMD-SHELL","curl -f http://localhost/ || exit 1"],"Retries":3}'
    hc = docker_mod.image_healthcheck("img", check_output=lambda c, text=True: out)
    assert hc["Retries"] == 3

def test_smoke_fails_loud_when_the_image_declares_no_healthcheck(tmp_path, monkeypatch):
    # Without a HEALTHCHECK, `up --wait` only waits for RUNNING — it would report
    # success on a container that never served a byte. That silent degradation is
    # the exact thing this gate exists to prevent, so it must be an error, not a
    # warning.
    repo = _smoke_repo(tmp_path)
    ref = ImageRef("webarena", "reddit", repo / "images" / "webarena" / "reddit")
    monkeypatch.setattr(docker_mod, "run", lambda c: None)
    monkeypatch.setattr(docker_mod, "compose_services", lambda p: ["reddit"])
    monkeypatch.setattr(docker_mod, "compose_service_image", lambda c, s: "ghcr.io/x/reddit:latest")
    monkeypatch.setattr(docker_mod, "image_healthcheck", lambda img: None)
    try:
        docker_mod.run_smoke([ref], repo)
    except SystemExit as e:
        assert "declares no HEALTHCHECK" in str(e)
    else:
        raise AssertionError("smoke accepted an image with no healthcheck")

def test_smoke_still_checks_the_PUBLISHED_path_after_wait(tmp_path, monkeypatch):
    # `up --wait` proves health INSIDE the container. #66 was healthy inside and
    # ECONNREFUSED from the host for 901s, so the host-side check is not
    # redundant and must survive this refactor.
    repo = _smoke_repo(tmp_path)
    ref = ImageRef("webarena", "reddit", repo / "images" / "webarena" / "reddit")
    monkeypatch.setattr(docker_mod, "run", lambda c: None)
    monkeypatch.setattr(docker_mod, "compose_services", lambda p: ["reddit"])
    monkeypatch.setattr(docker_mod, "compose_service_image", lambda c, s: "img")
    monkeypatch.setattr(docker_mod, "image_healthcheck", lambda img: {"Retries": 3})
    polled = []
    monkeypatch.setattr(docker_mod, "poll_health",
                        lambda url, timeout_s=30: polled.append((url, timeout_s))
                        or docker_mod.Health(True, 1.0, "HTTP 200"))
    docker_mod.run_smoke([ref], repo)
    assert polled == [("http://localhost:9999/forums", 30)]

def test_smoke_reachability_budget_is_small_not_the_readiness_budget(tmp_path, monkeypatch):
    # A reachability check that still waits 900s would re-create the very
    # duplicate-readiness-budget this change removes.
    repo = _smoke_repo(tmp_path)
    ref = ImageRef("webarena", "reddit", repo / "images" / "webarena" / "reddit")
    monkeypatch.setattr(docker_mod, "run", lambda c: None)
    monkeypatch.setattr(docker_mod, "compose_services", lambda p: ["reddit"])
    monkeypatch.setattr(docker_mod, "compose_service_image", lambda c, s: "img")
    monkeypatch.setattr(docker_mod, "image_healthcheck", lambda img: {"Retries": 3})
    seen = []
    monkeypatch.setattr(docker_mod, "poll_health",
                        lambda url, timeout_s=30: seen.append(timeout_s)
                        or docker_mod.Health(True, 1.0, "HTTP 200"))
    docker_mod.run_smoke([ref], repo)
    assert seen == [30]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python3 -m pytest tests/test_docker.py tests/test_manifest.py -v`
Expected: FAIL — `image_healthcheck` / `compose_service_image` undefined,
`reachability_timeout_s` missing.

- [ ] **Step 3: Implement the manifest change**

In `builder/manifest.py`, replace the `healthcheck_timeout_s` field:

```python
    healthcheck: str | None = None
    # How long the PUBLISHED address gets to answer once `up --wait` has already
    # proven the container healthy from the inside. Readiness is the image's own
    # HEALTHCHECK now, so this is small on purpose: it exists to catch a
    # container that is healthy in-container and unreachable through the port
    # mapping, which is what happened to shopping for 901s (#66).
    reachability_timeout_s: int = 30
```

And in `load_manifest`, before constructing the `Manifest`:

```python
    svc = data.get("service", {})
    if "healthcheck_timeout_s" in svc:
        _die(image_dir,
             "healthcheck_timeout_s is retired — readiness is now the image's own "
             "Dockerfile HEALTHCHECK (--start-period/--timeout/--retries), and the "
             "driver only checks that the PUBLISHED address answers. Move the budget "
             "into the HEALTHCHECK and delete this key; set reachability_timeout_s "
             "only if the port mapping itself is slow to come up.")
```

then:

```python
        healthcheck=svc.get("healthcheck"),
        reachability_timeout_s=int(svc.get("reachability_timeout_s", 30)),
```

- [ ] **Step 4: Implement the driver change**

Add to `builder/docker.py`, next to `compose_services`:

```python
def compose_service_image(compose: Path, service: str,
                          check_output=subprocess.check_output) -> str:
    """The image a compose service resolves to, with variables interpolated."""
    cfg = json.loads(check_output(
        ["docker", "compose", "-f", str(compose), "config", "--format", "json"],
        text=True))
    return cfg["services"][service]["image"]

def image_healthcheck(image: str, check_output=subprocess.check_output) -> dict | None:
    """The image's declared HEALTHCHECK, or None if it declares none.

    `docker image inspect` prints the literal `null` in the absent case
    (verified 2026-08-07), which is why this parses json rather than reading a
    dotted template — the dotted form is what fails outright on the container
    side, and matching shapes here keeps the two readers honest.
    """
    out = check_output(["docker", "image", "inspect", "--format",
                        "{{json .Config.Healthcheck}}", image], text=True).strip()
    return None if out in ("", "null") else json.loads(out)
```

Retune `poll_health`'s default and document the new role:

```python
def poll_health(url: str, timeout_s: int = 30,
                opener=urllib.request.urlopen, sleep=time.sleep,
                clock=time.monotonic) -> Health:
    """Is the service REACHABLE at its published address?

    NOT a readiness check — the image's own HEALTHCHECK owns that, and
    `up --wait` has already blocked on it before this runs. This answers the one
    question the in-container check structurally cannot: does the port mapping
    work? shopping was healthy inside its container while the host got
    ECONNREFUSED on 7770 for 901s (#66), and no HEALTHCHECK could ever have seen
    that.
    """
```

(body unchanged)

Then rewrite the per-ref block inside `run_smoke`'s `try:` — the `up --wait` call keeps its place,
and the healthcheck assertion goes **before** it so a missing HEALTHCHECK fails in seconds rather
than after a full boot:

```python
        try:
            for ref in want:
                image = compose_service_image(compose, ref.service)
                if image_healthcheck(image) is None:
                    raise SystemExit(
                        f"error: {ref.name}'s image {image} declares no HEALTHCHECK — "
                        f"`docker compose up --wait` would only wait for the container to "
                        f"be RUNNING, which a container that never serves a byte also is. "
                        f"Add a HEALTHCHECK to images/{ref.benchmark}/{ref.service}/Dockerfile.")
            run(["docker", "compose", "-f", str(compose), "up", "-d", "--wait",
                 *[r.service for r in want]])
            for ref in want:
                man = load_manifest(ref.path)
                hc = man.healthcheck
                if hc is None:
                    raise SystemExit(f"error: {ref.name} has no [service].healthcheck in image.toml")
                # `up --wait` proved it healthy from the INSIDE. This is the
                # other half: reachable from the host, through the published
                # port mapping.
                health = poll_health(hc, timeout_s=man.reachability_timeout_s)
                if not health:
                    raise SystemExit(
                        f"error: smoke FAILED — {ref.name} reports healthy in-container but "
                        f"is not reachable at its published address {hc} after "
                        f"{health.elapsed_s:.0f}s (last attempt: {health.last})")
                print(f"{ref.name}: healthy and reachable at {hc} after {health.elapsed_s:.0f}s")
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `python3 -m pytest tests/ -v`
Expected: PASS, all of them. Update `test_smoke_honours_the_images_own_healthcheck_timeout` — it
asserts the retired key — to assert `reachability_timeout_s` instead. **Do not delete it.**

- [ ] **Step 6: Prove the new gate BITES — by regressing production**

Two separate regressions; both must fail:

```bash
cd ~/workspace/browser-use-benchmarks
# (a) an image with no HEALTHCHECK must be refused
docker build -q -t hcplan-nohc:test - <<'EOF'
FROM busybox
CMD httpd -f -p 8399
EOF
cat > /tmp/hcplan-compose.yml <<'EOF'
services:
  server:
    image: hcplan-nohc:test
    ports: ["8399:8399"]
EOF
python3 - <<'PY'
import sys; sys.path.insert(0, "/home/agent/workspace/browser-use-benchmarks")
from pathlib import Path
from builder.docker import compose_service_image, image_healthcheck
img = compose_service_image(Path("/tmp/hcplan-compose.yml"), "server")
print("image:", img, "healthcheck:", image_healthcheck(img))
PY
docker image rm -f hcplan-nohc:test
```

Expected: `healthcheck: None` — so `run_smoke` would raise. Then:

```bash
# (b) prove the gate RUNS in the real smoke path, not just that it bites in
# isolation. Temporarily strip miniwob's HEALTHCHECK from the PRODUCTION
# Dockerfile and confirm smoke refuses it.
cp images/miniwob/server/Dockerfile /tmp/hcplan-t9.bak
python3 - <<'PY'
import re, pathlib
p = pathlib.Path("images/miniwob/server/Dockerfile")
p.write_text(re.sub(r"(?ms)^HEALTHCHECK.*?\n(?=\n|LABEL|$)", "", p.read_text()))
PY
bin/build build miniwob/server --datasets-dir "$HOME/benchmark-datasets"
bin/build smoke miniwob/server; echo "smoke exit=$?"
cp /tmp/hcplan-t9.bak images/miniwob/server/Dockerfile
bin/build build miniwob/server --datasets-dir "$HOME/benchmark-datasets"
bin/build smoke miniwob/server; echo "smoke exit=$?"
```

Expected: first `smoke exit=1` with `declares no HEALTHCHECK`; second `smoke exit=0`.
**If the first passes, the gate is not wired into the real path.**

- [ ] **Step 7: Commit**

```bash
git add builder/docker.py builder/manifest.py tests/test_docker.py tests/test_manifest.py
git commit -m "smoke: let the container healthcheck own readiness, keep the published-path check

Every image now declares a HEALTHCHECK, so \`up --wait\` genuinely gates on
readiness and poll_health's parallel budget was both redundant and misleading:
its hardcoded 10s per attempt could never outlast webshop's 57s first request
at ANY total budget, and each attempt queued another connection against a
single-threaded dev server until the backlog was exhausted — which surfaced as
Connection refused and sent the investigation to publishing.

poll_health is NOT deleted. --wait checks health INSIDE the container; this
checks the host-side published path, and #66 was healthy-inside/refused-outside
for 901s. It is now a 30s reachability check.

Smoke refuses an image that declares no HEALTHCHECK rather than silently
degrading to a RUNNING-state wait. healthcheck_timeout_s is retired and now an
error, so no stale 900 sits in a file doing nothing."
```

---

### Task 10: Document the contract

**Why:** the contract now has two halves, five near-identical `entrypoint.sh` copies, and one
image (gitlab) that deliberately refuses. Without one page saying why, the duplication reads as an
accident and someone factors it into `builder/` — which would make every push rebuild the whole
fleet.

**Files:**
- Create: `docs/service-contract.md`
- Modify: `README.md` (one pointer line)

**Interfaces:**
- Consumes: the address table from this plan.
- Produces: nothing.

- [ ] **Step 1: Write `docs/service-contract.md`**

It must contain, at minimum:
- The address table from this plan, **regenerated from the files as they then are** — not copied
  from here. Verify each row against the actual Dockerfile and compose entry.
- The rule: `HEALTHCHECK` names the **in-container** listen port; `[service].healthcheck` and
  `HTTP_HOST`/`HTTP_PORT` name the **published** address. Reddit (`9999:80`) as the worked example.
- Why `poll_health` still exists (#66: healthy in-container, `ECONNREFUSED` from the host for
  901s).
- Why `entrypoint.sh` is duplicated per image rather than shared from `builder/`: a shared file
  under `builder/` makes `discover` take the `building all` branch on every push.
- Tuning guidance: **`--timeout` is the decisive parameter, not the total budget** — a probe that
  gives up before the app's slowest single response can never succeed at any number of retries
  (webshop: 57s first request vs a 10s probe). Docker never overlaps probes, so a long `--timeout`
  is safe. `--start-period` failures do not count toward `--retries`; a success during it marks
  healthy immediately. Worst case to unhealthy ≈ `start-period + retries × (timeout + interval)`.
- `curl` is the fleet's one probe binary — `wget` is missing from shopping-admin.
- `-L` is load-bearing wherever the probed path redirects; `curl -f` does not fail on 3xx.
- gitlab's deliberate exception and the reasoning behind option (A).

- [ ] **Step 2: Add the README pointer**

One line in the appropriate section:

```markdown
- [`docs/service-contract.md`](docs/service-contract.md) — what every runnable image must declare:
  a `HEALTHCHECK` (readiness, in-container) and required `HTTP_HOST`/`HTTP_PORT` (published address).
```

- [ ] **Step 3: Verify every claim in the doc against the tree**

For each row of the table:

```bash
cd ~/workspace/browser-use-benchmarks
for d in images/*/*/; do
  [ -f "$d/Dockerfile" ] || continue
  echo "=== $d ==="
  grep -nE '^(EXPOSE|HEALTHCHECK)' "$d/Dockerfile" || echo "  (no EXPOSE/HEALTHCHECK)"
  grep -n 'healthcheck' "$d/image.toml" 2>/dev/null || true
done
grep -nE 'HTTP_HOST|HTTP_PORT|ports:|- "' images/*/compose.yml
```

Fix the doc to match reality — **not the other way around**. `probe/synthetic` is legitimately
absent from the table; say so explicitly rather than leaving a silent gap.

- [ ] **Step 4: Commit**

```bash
git add docs/service-contract.md README.md
git commit -m "docs: write down the fleet service contract

Two halves — a HEALTHCHECK owning readiness in-container, and required
HTTP_HOST/HTTP_PORT naming the published address — plus why the entrypoints are
deliberately duplicated per image and why gitlab refuses rather than adopts."
```

---

## Landing it

- [ ] `python3 -m pytest tests/ -v` — all green.
- [ ] `git log --oneline main..fleet-service-contract` — every task's commits present, authored
      `shkolnik-beep`, **no `Co-Authored-By`**.
- [ ] Open the PR. **build.yml has no `pull_request` trigger** (only `workflow_dispatch` and
      `push` on main), so opening it costs no CI — only merging triggers a run.
- [ ] Merge with a **merge commit, never squash**.
- [ ] The merge touches `builder/`, so `discover` takes the `building all` branch: expect a
      **full-fleet rebuild**, serialized at `max-parallel: 1`. Budget hours, not minutes.
- [ ] Watch every service go green. **A red image here is a real finding** — do not loosen a
      `--timeout` or a `--start-period` to get green without first reading the health log Task 1
      added and saying what it showed.

## The duplicated `entrypoint.sh`, decided explicitly

#71's scope line says "consolidat[e] the duplicated `entrypoint.sh`". This plan **deliberately does
not**, and that is a reversal worth stating rather than leaving as an omission.

After this plan there are five near-identical validation blocks (miniwob, classifieds, reddit,
webshop, gitlab) plus the two shipped Magento ones — seven copies of ~25 lines. The DRY instinct
says extract. Three facts say don't, yet:

1. **A shared file under `builder/` makes every push rebuild the whole fleet.** `discover` takes
   the `shared build inputs changed: building all` branch on any `builder/` change. A typo fix in
   one image's error message would then cost a full-fleet rebuild — hours, serialized.
2. **The bodies are not actually identical, and the differences are the interesting part.**
   miniwob and webshop only validate; classifieds *derives* `CLASSIFIEDS`; gitlab *refuses* on
   mismatch; reddit's shell is `/bin/sh` (alpine) while classifieds' and webshop's is
   `/bin/bash`; each execs a different final command. A shared script would need a
   per-image hook for every one of those, at which point it is a framework, not a deduplication.
3. **The precedent exists and is deliberately narrow.** `build_cmd` already passes
   `--build-context stagelib=builder/stage-lib` to *every* image precisely so shared build-time
   code has a home — and its own comment says the alternative was "three drifting copies of the
   same partitioner". So sharing is available; it was reserved for a real algorithm
   (`partition-tree.py`), not for an argument-validation preamble.

**The lean if this is revisited:** extract only the *validation and error message* — the part that
is genuinely identical and that must stay consistent for the contract to mean anything — into
`builder/stage-lib/require-http-host.sh`, sourced by each image's own entrypoint, which keeps its
app-specific tail. Do it in a wave that is already paying for a full-fleet rebuild. Do **not** do
it inside this plan: it would make each per-image task depend on a shared file and destroy the
parallel execution grouping that is the whole reason these are separate tasks.

## Open questions this plan deliberately does NOT decide

1. **gitlab option (A) vs (B)** — Task 8. Escalate to James; implement (A) if unanswered.
2. **Should `[service].healthcheck` be derived from compose + `HTTP_HOST`/`HTTP_PORT` instead of
   written twice?** They are converging on the same value, and a derived one could not drift. Not
   done here because the two are not always equal — shopping uses `127.0.0.1` in `image.toml`
   while its published host could be anything — and because it would couple the manifest to
   compose parsing mid-plan. Worth revisiting once the contract is uniform.
3. **#64 remains open and this plan does not fix it:** the pytest suite still never runs in CI, so
   every test written here is enforced only by whoever remembers to run it locally. That is a
   defect class of its own ("not-in-CI"), and it makes Task 9's guards weaker than they look.
   Consider landing a pytest step in the same PR — it is three lines in `build.yml` — but note it
   would itself force a full-fleet rebuild, which this plan is already paying for. **That
   coincidence is an argument for doing it here.**
