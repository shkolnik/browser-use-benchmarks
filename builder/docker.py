import hashlib
import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import NamedTuple
from builder.discover import ImageRef
from builder.manifest import Manifest, load_manifest

def run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd))
    if subprocess.run(cmd).returncode != 0:
        raise SystemExit(f"error: command failed: {' '.join(cmd)}")

def version_tag(repo_root: Path) -> str:
    # Pure function of HEAD — never wall clock. CI computes the tag
    # independently in the build, push, and clean steps, and a build slow
    # enough to cross midnight made push retry a tag that was never created.
    out = subprocess.run(
        ["git", "-C", str(repo_root), "show", "-s", "--format=%cs %h", "HEAD"],
        capture_output=True, text=True, check=True).stdout.split()
    return f"{out[0].replace('-', '')}.{out[1]}"

def build_cmd(ref: ImageRef, m: Manifest, registry: str, datasets_dir: Path,
              version: str, repo_root: Path) -> list[str]:
    # `stagelib` is offered to every image whether or not its Dockerfile COPYs
    # from it: a named build context costs nothing unreferenced, and the
    # alternative — per-image opt-in — is how three images ended up with three
    # drifting copies of the same partitioner.
    cmd = ["docker", "build",
           "--build-context", f"datasets={datasets_dir}",
           "--build-context", f"stagelib={repo_root / 'builder' / 'stage-lib'}"]
    for k, v in m.build_args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    cmd += ["-t", f"{registry}/{ref.name}:{version}",
            "-t", f"{registry}/{ref.name}:latest", str(ref.path)]
    return cmd

def load_cmds(ref: ImageRef, m: Manifest, registry: str,
              datasets_dir: Path, version: str) -> list[list[str]]:
    tarball = datasets_dir / m.source.dataset
    if not tarball.is_file():
        raise SystemExit(f"error: {tarball} not found — run download for {ref.name} first")
    return [
        ["docker", "load", "-i", str(tarball)],
        ["docker", "tag", m.source.tag, f"{registry}/{ref.name}:{version}"],
        ["docker", "tag", m.source.tag, f"{registry}/{ref.name}:latest"],
    ]

def push_cmds(ref: ImageRef, registry: str, version: str) -> list[list[str]]:
    return [["docker", "push", f"{registry}/{ref.name}:{version}"],
            ["docker", "push", f"{registry}/{ref.name}:latest"]]

# Derived artifacts are expensive (reddit's is a ~41G media tar) so they are
# cached on the runner between jobs — but "the file is there" was being used as
# a proxy for "the file is right", and the two came apart. Run 31126802473
# restored a 499.7 MB dump in SEVEN MILLISECONDS and failed audit with
# `relation "users" does not exist`: the runner's datasets dir still held the
# EMPTY dump produced back when the derive script dumped the wrong database.
# That recipe was fixed and its cache key bumped r1 -> r2, which correctly
# stranded the bad entry in the GHCR cache — but the bump never fired, because
# a presence check meant the script never ran to consult its key. The GHCR
# cache was keyed by recipe; the local datasets dir was keyed by nothing.
#
# So record what a successful derivation actually left behind, and reuse
# artifacts only when they still match it. Hashing the script into the stamp
# is what makes a recipe change self-enforcing: edit the derive script — bump
# RECIPE, fix a dumped database name, anything — and every artifact it
# previously produced stops being reusable, with no second place to remember.
def prepare_stamp_path(datasets_dir: Path, ref: ImageRef) -> Path:
    return datasets_dir / ".prepare" / f"{ref.name}.json"

def prepare_input_datasets(m: Manifest) -> list:
    """Every upstream artifact the prepare script fetches for itself.

    The manifest is the ONE place their sha256s are written down. They are both
    the GHCR cache tag and part of the on-disk provenance stamp, so a script
    that hardcoded its own copy could serve artifacts derived from a superseded
    input after the pin was updated in only one of the two places.
    """
    return [ds for ds in m.datasets if ds.prepare_input]

def prepare_input_dataset(m: Manifest):
    """The pin, when the image has EXACTLY ONE — else None.

    Deliberately None for a multi-input image rather than "the first one": a
    script that keyed its cache on one pin out of several would name an
    identity that does not distinguish its own artifacts. run_prepare exports
    PREPARE_INPUT_SHA256 only from this, so such a script fails loud on the
    `: "${PREPARE_INPUT_SHA256:?}"` line instead of caching under a partial key.
    """
    pins = prepare_input_datasets(m)
    return pins[0] if len(pins) == 1 else None

def prepare_inputs_digest(m: Manifest) -> str | None:
    """Order-independent identity of the WHOLE pinned input set, or None if <2.

    None below two so single-input images keep the exact stamp and cache key
    they already have — generalising the mechanism must not strand four
    working GHCR caches.
    """
    pins = prepare_input_datasets(m)
    if len(pins) < 2:
        return None
    lines = sorted(f"{ds.filename}:{ds.sha256}" for ds in pins)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()

def prepare_fingerprint(ref: ImageRef, m: Manifest, datasets_dir: Path) -> dict:
    script_bytes = (ref.path / m.prepare.script).read_bytes()
    return {
        "script": m.prepare.script,
        "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
        "outputs": {o: (datasets_dir / o).stat().st_size for o in m.prepare.outputs},
        # Without this the artifacts survive a pin change untouched: the script
        # is unchanged, the sizes are unchanged, so the derive is skipped and
        # the build silently uses data derived from the previous upstream tar.
        "prepare_input_sha256": (pin.sha256 if (pin := prepare_input_dataset(m)) else None),
        # Absent from every stamp written before multi-input support, which is
        # why it is read with .get(): None == None keeps those stamps valid.
        "prepare_inputs_digest": prepare_inputs_digest(m),
    }

def prepare_reuse_check(ref: ImageRef, m: Manifest, datasets_dir: Path) -> str | None:
    """None if the cached artifacts may be reused, else why they may not be."""
    missing = [o for o in m.prepare.outputs if not (datasets_dir / o).is_file()]
    if missing:
        return f"missing: {', '.join(missing)}"
    stamp = prepare_stamp_path(datasets_dir, ref)
    if not stamp.is_file():
        return "no provenance stamp — artifacts of unknown origin"
    try:
        recorded = json.loads(stamp.read_text())
    except (json.JSONDecodeError, OSError):
        return "provenance stamp is unreadable"
    current = prepare_fingerprint(ref, m, datasets_dir)
    if recorded.get("script_sha256") != current["script_sha256"]:
        return f"{m.prepare.script} changed since these artifacts were derived"
    if recorded.get("prepare_input_sha256") != current["prepare_input_sha256"]:
        pin = prepare_input_dataset(m)
        return f"{pin.filename}'s pinned sha256 changed since these artifacts were derived"
    if recorded.get("prepare_inputs_digest") != current["prepare_inputs_digest"]:
        return ("the pinned prepare_input set changed since these artifacts "
                "were derived")
    for name, size in current["outputs"].items():
        was = recorded.get("outputs", {}).get(name)
        if was != size:
            return f"{name} is {size} bytes, was {was} when derived"
    return None

def run_prepare(ref: ImageRef, m: Manifest, registry: str, datasets_dir: Path) -> None:
    reason = prepare_reuse_check(ref, m, datasets_dir)
    if reason is None:
        print(f"{ref.name}: prepare outputs verified against their stamp, "
              f"skipping {m.prepare.script}")
        return
    print(f"{ref.name}: running {m.prepare.script} ({reason})")
    # REPO_ROOT/IMAGE let the script call `$REPO_ROOT/bin/build download
    # --prepare-inputs $IMAGE` on its cache-miss path — the lazy half of
    # prepare_input datasets, which the download step deliberately skipped.
    env = dict(os.environ, DATASETS_DIR=str(datasets_dir.resolve()), REGISTRY=registry,
               REPO_ROOT=str(Path(__file__).resolve().parents[1]),
               IMAGE=f"{ref.benchmark}/{ref.service}")
    # The pin, from the manifest, so the script never keeps its own copy.
    if pin := prepare_input_dataset(m):
        env["PREPARE_INPUT_SHA256"] = pin.sha256
        env["PREPARE_INPUT_FILE"] = pin.filename
    if digest := prepare_inputs_digest(m):
        env["PREPARE_INPUTS_DIGEST"] = digest
    if pins := prepare_input_datasets(m):
        # `sha256sum -c` format, so a script can verify a cache hit in one
        # line. Only pinned inputs can be checked this way — a derived
        # artifact has no pin, which is why the other scripts cannot.
        env["PREPARE_INPUT_PINS"] = "\n".join(f"{d.sha256}  {d.filename}" for d in pins)
    proc = subprocess.run(["/bin/bash", m.prepare.script], cwd=ref.path, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"error: prepare script failed for {ref.name}")
    still = [o for o in m.prepare.outputs if not (datasets_dir / o).is_file()]
    if still:
        raise SystemExit(
            f"error: prepare for {ref.name} did not produce: {', '.join(still)}")
    stamp = prepare_stamp_path(datasets_dir, ref)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps(prepare_fingerprint(ref, m, datasets_dir), indent=2))
    print(f"{ref.name}: stamped {stamp}")


def run_build(refs, registry: str, datasets_dir: Path, repo_root: Path) -> None:
    version = version_tag(repo_root)
    for ref in refs:
        m = load_manifest(ref.path)
        if m.source.kind == "docker-save":
            for cmd in load_cmds(ref, m, registry, datasets_dir, version):
                run(cmd)
        else:
            if m.prepare:
                run_prepare(ref, m, registry, datasets_dir)
            run(build_cmd(ref, m, registry, datasets_dir, version, repo_root))

def clean_cmds(ref: ImageRef, m: Manifest, registry: str, version: str) -> list[list[str]]:
    tags = [f"{registry}/{ref.name}:{version}", f"{registry}/{ref.name}:latest"]
    if m.source.kind == "docker-save":
        tags.append(m.source.tag)
    return [["docker", "image", "rm", "-f"] + tags]

def run_clean(refs, registry: str, repo_root: Path, runner=subprocess.run, log=print) -> None:
    # Best-effort by design: clean runs in CI's always() step, where the image
    # may never have been built — and `docker image rm -f` exits non-zero on a
    # missing image (verified live), so failures warn and cleaning continues.
    version = version_tag(repo_root)
    for ref in refs:
        for cmd in clean_cmds(ref, load_manifest(ref.path), registry, version):
            log("+ " + " ".join(cmd))
            if runner(cmd).returncode != 0:
                log(f"warning: cleanup command failed (continuing): {' '.join(cmd)}")

def run_with_retry(cmd: list[str], attempts: int = 5, runner=subprocess.run,
                   sleep=time.sleep, log=print) -> None:
    # Registry pushes can die on the server's whole-push timeout, but completed
    # layers are digest-deduped across attempts, so retrying converges as long
    # as at least one layer finishes per attempt.
    for attempt in range(1, attempts + 1):
        log(f"+ {' '.join(cmd)} (attempt {attempt}/{attempts})")
        if runner(cmd).returncode == 0:
            return
        sleep(min(60, 10 * attempt))
    raise SystemExit(f"error: command failed after {attempts} attempts: {' '.join(cmd)}")

def run_push(refs, registry: str, repo_root: Path) -> None:
    version = version_tag(repo_root)
    for ref in refs:
        for cmd in push_cmds(ref, registry, version):
            run_with_retry(cmd)

class Health(NamedTuple):
    """Outcome of polling a healthcheck URL.

    Truthy on success so callers read as before, but it also carries how long
    the wait took and what the LAST attempt saw. A bare False cannot tell a
    container that never opened the port from one that answered 503 the whole
    time, and those want opposite fixes.
    """
    ok: bool
    elapsed_s: float
    last: str

    def __bool__(self) -> bool:
        return self.ok

def poll_health(url: str, timeout_s: int = 120,
                opener=urllib.request.urlopen, sleep=time.sleep,
                clock=time.monotonic) -> Health:
    start = clock()
    deadline = start + timeout_s
    last = "no attempt completed"
    while True:
        try:
            with opener(url, timeout=10) as resp:
                last = f"HTTP {resp.status}"
                if 200 <= resp.status < 400:
                    return Health(True, clock() - start, last)
        except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as e:
            # urllib raises HTTPError (a URLError) for >=400, so an error PAGE
            # lands here too — keep the status rather than the exception text.
            last = f"HTTP {e.code}" if getattr(e, "code", None) else f"{type(e).__name__}: {e}"
        if clock() >= deadline:
            return Health(False, clock() - start, last)
        sleep(2)

def compose_services(compose: Path, check_output=subprocess.check_output) -> list[str]:
    out = check_output(
        ["docker", "compose", "-f", str(compose), "config", "--services"], text=True)
    return [line.strip() for line in out.splitlines() if line.strip()]

def dump_service_logs(compose: Path, service: str, tail: str = "400",
                      runner=subprocess.run) -> None:
    # Best-effort: this runs on a path that is already failing, so it must not
    # replace the real error with one of its own.
    print(f"=== docker compose logs (last {tail} lines) for {service} ===")
    try:
        runner(["docker", "compose", "-f", str(compose), "logs",
                "--tail", tail, service])
    except Exception as e:
        print(f"(could not collect logs: {e})")

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

def dump_service_diagnostics(compose: Path, service: str,
                             runner=subprocess.run) -> None:
    # Logs alone could not explain shopping (CI 31170194781): supervisord said
    # nginx was RUNNING and the host still got ECONNREFUSED for 901s. ECONNREFUSED
    # means nothing is bound on the container end of the DNAT, so the two facts
    # that separate "the server never bound" from "publishing is broken" are the
    # port bindings and the listener table — neither of which is in any log.
    # Best-effort, like the log dump: this runs on an already-failing path.
    dump_health_log(compose, service)
    probes = [
        (["docker", "compose", "-f", str(compose), "ps", service],
         "what compose published"),
        (["docker", "compose", "-f", str(compose), "exec", "-T", service,
          "sh", "-c", "ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null"],
         "listeners inside the container"),
    ]
    for cmd, what in probes:
        print(f"=== {what} for {service} ===")
        try:
            runner(cmd)
        except Exception as e:
            print(f"(could not collect: {e})")

def run_smoke(refs, repo_root: Path) -> None:
    benches = sorted({r.benchmark for r in refs})
    for bench in benches:
        compose = repo_root / "images" / bench / "compose.yml"
        if not compose.is_file():
            raise SystemExit(f"error: {compose} not found — every benchmark needs a compose.yml")
        want = [r for r in refs if r.benchmark == bench]
        # Only the TARGETED services come up. Bringing the whole file up is what
        # kept this out of CI: images/webarena/compose.yml declares shopping,
        # shopping-admin, reddit and gitlab, so smoke-testing a reddit build
        # would try to pull ~75G of images the job never built and does not
        # have. Compose service name == the image directory name, asserted
        # rather than assumed, because a rename would otherwise silently smoke
        # nothing at all.
        declared = set(compose_services(compose))
        missing = sorted(r.service for r in want if r.service not in declared)
        if missing:
            raise SystemExit(
                f"error: {compose} declares no service named {', '.join(missing)} — "
                f"a compose service must be named after its images/{bench}/<service>/ directory")
        # `up --wait` goes INSIDE the try, and every failure dumps logs on the
        # way out. It used to sit outside, which left the single most likely
        # smoke failure — a container that never reports healthy — as the one
        # failure this whole observability path could not see: `--wait` treats
        # unhealthy as terminal, so it raised past both the log dump and the
        # teardown, printing nothing but its own exit code and leaving the
        # container running for the next job to trip over. Twice on gitlab,
        # 2026-08-07. The dump moved out here for the same reason: whatever
        # fails between here and the last healthcheck, the evidence is the
        # container's logs, and the finally-block is about to delete them.
        try:
            run(["docker", "compose", "-f", str(compose), "up", "-d", "--wait",
                 *[r.service for r in want]])
            for ref in want:
                man = load_manifest(ref.path)
                hc = man.healthcheck
                if hc is None:
                    raise SystemExit(f"error: {ref.name} has no [service].healthcheck in image.toml")
                health = poll_health(hc, timeout_s=man.healthcheck_timeout_s)
                if not health:
                    raise SystemExit(
                        f"error: smoke FAILED — {ref.name} never became healthy at {hc} "
                        f"after {health.elapsed_s:.0f}s (last attempt: {health.last})")
                print(f"{ref.name}: healthy at {hc} after {health.elapsed_s:.0f}s")
        except BaseException:
            # BaseException, not Exception: SystemExit is how everything in
            # here reports failure, and it is not an Exception.
            for ref in want:
                dump_service_logs(compose, ref.service)
                dump_service_diagnostics(compose, ref.service)
            raise
        finally:
            run(["docker", "compose", "-f", str(compose), "down", "-v"])
