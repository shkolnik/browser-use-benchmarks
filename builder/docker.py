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

def run_build(refs, registry: str, datasets_dir: Path, repo_root: Path) -> None:
    version = version_tag(repo_root)
    for ref in refs:
        m = load_manifest(ref.path)
        if m.source.kind == "docker-save":
            for cmd in load_cmds(ref, m, registry, datasets_dir, version):
                run(cmd)
        else:
            run(build_cmd(ref, m, registry, datasets_dir, version))

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
