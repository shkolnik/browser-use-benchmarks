import subprocess
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

def push_cmds(ref: ImageRef, registry: str, version: str) -> list[list[str]]:
    return [["docker", "push", f"{registry}/{ref.name}:{version}"],
            ["docker", "push", f"{registry}/{ref.name}:latest"]]

def run_build(refs, registry: str, datasets_dir: Path, repo_root: Path) -> None:
    version = version_tag(repo_root)
    for ref in refs:
        run(build_cmd(ref, load_manifest(ref.path), registry, datasets_dir, version))

def run_push(refs, registry: str, repo_root: Path) -> None:
    version = version_tag(repo_root)
    for ref in refs:
        for cmd in push_cmds(ref, registry, version):
            run(cmd)
