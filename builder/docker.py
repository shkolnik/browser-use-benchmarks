import os
import subprocess
import time
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path
from builder.discover import ImageRef
from builder.manifest import Manifest, load_manifest

def run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd))
    if subprocess.run(cmd).returncode != 0:
        raise SystemExit(f"error: command failed: {' '.join(cmd)}")

def version_tag(repo_root: Path) -> str:
    sha = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return f"{date.today():%Y%m%d}.{sha}"

def build_cmd(ref: ImageRef, m: Manifest, registry: str,
              datasets_dir: Path, version: str) -> list[str]:
    cmd = ["docker", "build", "--build-context", f"datasets={datasets_dir}"]
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

def run_prepare(ref: ImageRef, m: Manifest, registry: str, datasets_dir: Path) -> None:
    missing = [o for o in m.prepare.outputs if not (datasets_dir / o).is_file()]
    if not missing:
        print(f"{ref.name}: prepare outputs present, skipping {m.prepare.script}")
        return
    print(f"{ref.name}: running {m.prepare.script} (missing: {', '.join(missing)})")
    env = dict(os.environ, DATASETS_DIR=str(datasets_dir.resolve()), REGISTRY=registry)
    proc = subprocess.run(["/bin/bash", m.prepare.script], cwd=ref.path, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"error: prepare script failed for {ref.name}")
    still = [o for o in m.prepare.outputs if not (datasets_dir / o).is_file()]
    if still:
        raise SystemExit(
            f"error: prepare for {ref.name} did not produce: {', '.join(still)}")


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
            run(build_cmd(ref, m, registry, datasets_dir, version))

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

def poll_health(url: str, timeout_s: int = 120,
                opener=urllib.request.urlopen, sleep=time.sleep) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with opener(url, timeout=10) as resp:
                if 200 <= resp.status < 400:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError, TimeoutError):
            pass
        if time.monotonic() >= deadline:
            return False
        sleep(2)

def run_smoke(refs, repo_root: Path) -> None:
    benches = sorted({r.benchmark for r in refs})
    for bench in benches:
        compose = repo_root / "images" / bench / "compose.yml"
        if not compose.is_file():
            raise SystemExit(f"error: {compose} not found — every benchmark needs a compose.yml")
        run(["docker", "compose", "-f", str(compose), "up", "-d", "--wait"])
        try:
            for ref in [r for r in refs if r.benchmark == bench]:
                hc = load_manifest(ref.path).healthcheck
                if hc is None:
                    raise SystemExit(f"error: {ref.name} has no [service].healthcheck in image.toml")
                if not poll_health(hc):
                    raise SystemExit(f"error: smoke FAILED — {ref.name} never became healthy at {hc}")
                print(f"{ref.name}: healthy at {hc}")
        finally:
            run(["docker", "compose", "-f", str(compose), "down", "-v"])
