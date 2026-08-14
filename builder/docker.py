import hashlib
import json
import os
import shutil
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

def run_piped(src: list[str], dst: list[str]) -> None:
    """`src | dst`, failing the build if either end does.

    A shell would report only the right-hand status by default, which is the
    half that stays 0 when the producer dies mid-stream — a truncated archive
    extracting cleanly up to the cut.
    """
    print("+ " + " ".join(src) + " | " + " ".join(dst))
    producer = subprocess.Popen(src, stdout=subprocess.PIPE)
    consumer = subprocess.Popen(dst, stdin=producer.stdout)
    # Only the consumer should hold the read end, so the producer sees EPIPE and
    # exits if the consumer dies first rather than blocking on a full pipe.
    producer.stdout.close()
    consumer_rc, producer_rc = consumer.wait(), producer.wait()
    if producer_rc != 0 or consumer_rc != 0:
        raise SystemExit(f"error: command failed "
                         f"({' '.join(src)} -> {producer_rc}, "
                         f"{' '.join(dst)} -> {consumer_rc})")

def log_resources(phase: str, path: Path, log=print) -> None:
    """One JSONL reading of memory, swap and free disk at a phase boundary.

    A build that goes quiet is indistinguishable from one that is swapping or
    out of disk until someone can read these three numbers, and by then the
    ephemeral runner that held them is gone. Emitted in-stream because the log
    already timestamps every line.

    Best-effort: a missing /proc degrades to nulls rather than failing a build
    over its own instrumentation.
    """
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            mem[key] = int(value.split()[0])  # kB
    except (OSError, ValueError, IndexError):
        pass
    # statvfs needs a path that exists, and the datasets dir does not until
    # something writes to it — so the reading at :start, the one that says what
    # the job began with, is exactly the one that would come back null. Free
    # space is a property of the filesystem, not the directory, so the nearest
    # existing ancestor answers the same question.
    disk = None
    for candidate in (path.resolve(), *path.resolve().parents):
        try:
            disk = shutil.disk_usage(candidate)
            break
        except OSError:
            continue
    swap_total, swap_free = mem.get("SwapTotal"), mem.get("SwapFree")
    log(json.dumps({
        "metric": "resources",
        "phase": phase,
        "mem_available_mb": mem["MemAvailable"] // 1024 if "MemAvailable" in mem else None,
        "mem_total_mb": mem["MemTotal"] // 1024 if "MemTotal" in mem else None,
        "swap_used_mb": (swap_total - swap_free) // 1024
                        if swap_total is not None and swap_free is not None else None,
        "disk_free_gb": disk.free // 2**30 if disk else None,
        "disk_total_gb": disk.total // 2**30 if disk else None,
    }))


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

def output_paths(datasets_dir: Path, name: str) -> list[Path]:
    """The files a prepare output actually landed as: itself, or its parts.

    Registry layers over ~10G are refused, so a large output ships to the
    derived cache split into `<name>.part-NN` and arrives that way on a cache
    hit. image.toml names the whole thing and this resolves how it turned up,
    which keeps the part count — a function of the size, not of the recipe —
    out of a static list.

    Ordered by name, which is the split order: `split -d` pads the suffix, so
    lexicographic and numeric agree below 100 parts. Beyond that the suffix
    widens and a misordered join fails the extract rather than corrupting it.
    """
    whole = datasets_dir / name
    if whole.is_file():
        return [whole]
    return sorted(datasets_dir.glob(name + ".part-*"))

def prepare_fingerprint(ref: ImageRef, m: Manifest, datasets_dir: Path) -> dict:
    script_bytes = (ref.path / m.prepare.script).read_bytes()
    return {
        "script": m.prepare.script,
        "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
        # Summed, so an output fingerprints the same whether it arrived whole
        # from a derive or split from the cache.
        "outputs": {o: sum(p.stat().st_size for p in output_paths(datasets_dir, o))
                    for o in m.prepare.outputs},
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
    missing = [o for o in m.prepare.outputs if not output_paths(datasets_dir, o)]
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
    still = [o for o in m.prepare.outputs if not output_paths(datasets_dir, o)]
    if still:
        raise SystemExit(
            f"error: prepare for {ref.name} did not produce: {', '.join(still)}")
    stamp = prepare_stamp_path(datasets_dir, ref)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps(prepare_fingerprint(ref, m, datasets_dir), indent=2))
    print(f"{ref.name}: stamped {stamp}")


def media_dir(ref: ImageRef) -> Path:
    """Where the bucket tars live: inside the build context, on purpose.

    build_cmd passes str(ref.path) as the positional context, and ADD
    auto-extracts only from the DEFAULT context — it has no --from — so this is
    the one place the final stage can ADD them from. Gitignored.
    """
    return ref.path / ".media"

def media_work_dir(datasets_dir: Path, ref: ImageRef) -> Path:
    """Where the archive is extracted: OUTSIDE the build context, on purpose.

    A loose tree inside the image dir would hand buildkit millions of small
    files to sync into its content store as build context, which is the
    per-entry cost the media path exists to avoid.
    """
    return datasets_dir / ".media-work" / ref.name.replace("/", "-")

def run_media_prep(ref: ImageRef, m: Manifest, datasets_dir: Path,
                   repo_root: Path) -> None:
    """Extract the media archive and bucket it into tars, on the host.

    Runs between prepare and build, so the final stage can ADD the tars instead
    of the restore stage materialising the tree and the final stage COPYing it
    back out entry by entry.
    """
    media, out = m.media, media_dir(ref)
    work = media_work_dir(datasets_dir, ref)
    parts = output_paths(datasets_dir, media.archive)
    if not parts:
        raise SystemExit(f"error: {datasets_dir / media.archive} not found "
                         f"(nor its parts) — prepare should have left it")

    shutil.rmtree(out, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    # A 45 GiB extract of small files is mute for its whole run without this,
    # which reads exactly like a hang. A tar record is 10240 bytes, so 100000
    # records is a line per ~1 GiB, carrying bytes moved and the rate.
    #
    # Read from stdin rather than by name, so a split archive is joined in the
    # pipe. Writing the parts back out as one file first would cost a full read
    # and a full write of the whole archive, plus a second copy of it on the
    # disk this workload is already bound by — for bytes whose only consumer is
    # the extract on the other side of the join.
    extract = ["tar", "x", "-C", str(work),
               "--checkpoint=100000",
               "--checkpoint-action=echo=  media extract: %T after %ds"]
    if media.strip:
        # --strip-components counts path segments, so derive it from the prefix
        # rather than making each image state a number that has to stay in sync
        # with a path it already declares.
        extract += [f"--strip-components={len(media.strip.split('/'))}", media.strip]
    run_piped(["cat", *(str(p) for p in parts)], extract)

    run(["python3", str(repo_root / "builder" / "stage-lib" / "bucket-media.py"),
         str(media.limit_kb), str(media.max_buckets), str(work), str(out)])

    if not media.restore_needs_media:
        # The extracted tree is the largest transient on disk; dropping it now
        # keeps peak disk at the archive plus the tars.
        shutil.rmtree(work, ignore_errors=True)

def clean_media(refs, datasets_dir: Path) -> None:
    """Remove bucket tars and any extracted tree.

    Deliberately NOT called from run_build: build, smoke and push are separate
    CLI invocations, so the tars must survive from one to the next. Called after
    a successful push and from run_clean, which CI runs under always().
    """
    for ref in refs:
        for path in (media_dir(ref), media_work_dir(datasets_dir, ref)):
            if path.exists():
                print(f"+ rm -rf {path}")
                shutil.rmtree(path, ignore_errors=True)

def run_build(refs, registry: str, datasets_dir: Path, repo_root: Path) -> None:
    version = version_tag(repo_root)
    for ref in refs:
        m = load_manifest(ref.path)
        if m.source.kind == "docker-save":
            for cmd in load_cmds(ref, m, registry, datasets_dir, version):
                run(cmd)
        else:
            log_resources(f"{ref.name}:start", datasets_dir)
            if m.prepare:
                run_prepare(ref, m, registry, datasets_dir)
                log_resources(f"{ref.name}:after-prepare", datasets_dir)
            if m.media:
                run_media_prep(ref, m, datasets_dir, repo_root)
                log_resources(f"{ref.name}:after-media-prep", datasets_dir)
            run(build_cmd(ref, m, registry, datasets_dir, version, repo_root))
            log_resources(f"{ref.name}:after-build", datasets_dir)

def clean_cmds(ref: ImageRef, m: Manifest, registry: str, version: str) -> list[list[str]]:
    tags = [f"{registry}/{ref.name}:{version}", f"{registry}/{ref.name}:latest"]
    if m.source.kind == "docker-save":
        tags.append(m.source.tag)
    return [["docker", "image", "rm", "-f"] + tags]

def run_clean(refs, registry: str, repo_root: Path, runner=subprocess.run, log=print,
              datasets_dir: Path | None = None) -> None:
    # Best-effort by design: clean runs in CI's always() step, where the image
    # may never have been built — and `docker image rm -f` exits non-zero on a
    # missing image (verified live), so failures warn and cleaning continues.
    version = version_tag(repo_root)
    for ref in refs:
        for cmd in clean_cmds(ref, load_manifest(ref.path), registry, version):
            log("+ " + " ".join(cmd))
            if runner(cmd).returncode != 0:
                log(f"warning: cleanup command failed (continuing): {' '.join(cmd)}")
    # Media tars ride here rather than in a workflow step of their own: this
    # runs under always(), so it covers the paths that would otherwise strand
    # them — a pull-request build, which smokes and never pushes, and any
    # failure between build and push. Idempotent after a successful push.
    if datasets_dir is not None:
        clean_media(refs, datasets_dir)

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

def run_push(refs, registry: str, repo_root: Path,
             datasets_dir: Path | None = None) -> None:
    version = version_tag(repo_root)
    for ref in refs:
        for cmd in push_cmds(ref, registry, version):
            run_with_retry(cmd)
    # After the push: the tars are build context, and a retrying push must not
    # find them gone.
    if datasets_dir is not None:
        clean_media(refs, datasets_dir)

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

def _dump(cmd: list[str], what: str, runner=subprocess.run, log=print) -> None:
    """Print one diagnostic section, and make an EMPTY one say why it is empty.

    Two separate defects produced the gitlab failures' unreadable diagnostics
    (#76), and this addresses both:

    1. The output was not missing, it was DISPLACED. These sections used to
       print a header and then let the child write straight to the inherited
       fd. Python block-buffers stdout when it is a pipe — which is what CI
       gives it — so every header sat in the buffer until exit while the child
       output went out immediately. The log showed the docker output up front
       and a run of bare headers at the end. Capturing the child's output and
       logging it ourselves makes ordering structural rather than a matter of
       flush discipline. (bin/build also line-buffers now, which fixes the same
       displacement for `run`'s `+ cmd` echoes everywhere else.)

    2. A section that genuinely had nothing to show was indistinguishable from
       one that failed. `ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null`
       prints nothing and says nothing when an image ships neither tool. An
       empty section now carries the exit code, and stderr is folded in rather
       than discarded, so the reader can tell "nothing was listening" from
       "the probe could not run".

    Best-effort throughout: this runs on an already-failing path and must never
    replace the real error with one of its own.
    """
    log(f"=== {what} ===")
    try:
        p = runner(cmd, capture_output=True, text=True)
    except Exception as e:
        log(f"(could not collect: {e})")
        return
    # A faked runner in a test may return None; treat that as "no output"
    # rather than crashing the diagnostic path.
    body = ((getattr(p, "stdout", "") or "") + (getattr(p, "stderr", "") or "")).rstrip()
    if body:
        log(body)
    else:
        log(f"(no output; `{' '.join(cmd)}` exited {getattr(p, 'returncode', '?')})")

def dump_service_logs(compose: Path, service: str, tail: str = "400",
                      runner=subprocess.run) -> None:
    _dump(["docker", "compose", "-f", str(compose), "logs", "--tail", tail, service],
          f"docker compose logs (last {tail} lines) for {service}", runner=runner)

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
    # The listener probe keeps its fallback chain but no longer sends the
    # failures to /dev/null: an image with neither tool used to print nothing
    # and explain nothing, which reads exactly like "nothing is listening" —
    # the opposite conclusion from the one the operator should draw.
    probes = [
        (["docker", "compose", "-f", str(compose), "ps", service],
         f"what compose published for {service}"),
        (["docker", "compose", "-f", str(compose), "exec", "-T", service,
          "sh", "-c", "ss -ltnp || netstat -ltnp || "
                      "echo '(neither ss nor netstat is present in this image)'"],
         f"listeners inside the container for {service}"),
    ]
    for cmd, what in probes:
        _dump(cmd, what, runner=runner)

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
            # Before the boot, not after: a missing HEALTHCHECK is a defect in
            # the image, and finding it costs seconds here instead of a full
            # start-period. `up --wait` on an image with none silently degrades
            # to waiting for RUNNING, which a container that never serves a
            # byte also reaches.
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
        except BaseException:
            # BaseException, not Exception: SystemExit is how everything in
            # here reports failure, and it is not an Exception.
            for ref in want:
                dump_service_logs(compose, ref.service)
                dump_service_diagnostics(compose, ref.service)
            raise
        finally:
            run(["docker", "compose", "-f", str(compose), "down", "-v"])
