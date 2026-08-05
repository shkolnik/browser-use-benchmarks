# browser-use-benchmarks: Builder + M0 probe + M1 MiniWoB++ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `bin/build` driver (download/build/push/smoke), the M0 synthetic registry-size probe assets, and the first real benchmark (MiniWoB++), per `docs/design.md`.

**Architecture:** A Python-stdlib package `builder/` with a thin `bin/build` entry point. Per-image config lives entirely in `images/<benchmark>/<service>/image.toml`; discovery is by glob (no root indexes). Datasets download into a git-ignored `datasets/` cache with resume/retry/mirror rotation and mandatory sha256 verification, then reach `docker build` via a supplementary build context.

**Tech Stack:** Python 3.11+ (stdlib only at runtime: `tomllib`, `hashlib`, `urllib`, `subprocess`), `curl` for downloads, Docker with BuildKit (`--build-context`), docker compose v2, pytest (dev-only).

## Global Constraints

- Runtime code uses **Python stdlib only** — no pip dependencies outside tests (design: "dependency-light").
- **Fail-loud:** checksum mismatch or exhausted mirrors must stop with the exact URL/file named; a failed file is quarantined (renamed `<name>.quarantine`), never handed to docker.
- Dockerfiles never fetch from the network; every input is git-tracked or a checksummed dataset.
- Image tags: `ghcr.io/shkolnik/<benchmark>-<service>:<yyyymmdd>.<gitshortsha>` and `:latest`; `--registry` overrides the `ghcr.io/shkolnik` prefix.
- Removing a service must remain: delete its directory (+ its block in the benchmark `compose.yml`). Never add root-level per-image registration.
- Commits by `shkolnik-beep` (user.email jshkolnik@gmail.com), **no Co-Authored-By**, each message ends with trailer:
  `Claude-Session: https://claude.ai/code/session_017LLYyHughqzE3T29Nprydo`
- x86-64 only. No multi-arch, no cache pruning automation, no build framework (YAGNI, per design).

## File Structure

```
bin/build                      # exec shim → builder.cli.main()
builder/__init__.py
builder/discover.py            # find image dirs by glob
builder/manifest.py            # image.toml load + validation (dataclasses)
builder/download.py            # cache/verify/quarantine + retry/mirror rotation
builder/docker.py              # build/push/smoke command construction + execution
builder/cli.py                 # argparse subcommands wiring
tests/test_discover.py  test_manifest.py  test_download.py  test_docker.py
tests/integration/test_end_to_end.py     # requires docker; skipped if absent
images/probe/synthetic/        # M0: generator + Dockerfile + image.toml
images/miniwob/compose.yml
images/miniwob/server/         # M1: image.toml + Dockerfile + nginx.conf
docs/registry-limits.md        # M0 findings template
.github/workflows/build.yml
.gitignore  README.md
```

---

### Task 1: Skeleton, discovery, `list` subcommand

**Files:**
- Create: `.gitignore`, `README.md`, `bin/build`, `builder/__init__.py`, `builder/discover.py`, `builder/cli.py`
- Test: `tests/test_discover.py`, `tests/conftest.py`

**Interfaces:**
- Produces: `discover.ImageRef` dataclass (`benchmark: str`, `service: str`, `path: Path`, property `name` = `f"{benchmark}-{service}"`), `discover.find_images(repo_root: Path, target: str) -> list[ImageRef]` where target is `all`, `<benchmark>`, or `<benchmark>/<service>`; raises `SystemExit` with message if target matches nothing. `cli.main(argv)` entry.

- [ ] **Step 1: Write failing tests**

`tests/conftest.py`:
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
```

`tests/test_discover.py`:
```python
from pathlib import Path
import pytest
from builder.discover import find_images

def make_repo(tmp_path: Path) -> Path:
    for b, s in [("miniwob", "server"), ("webarena", "gitlab"), ("webarena", "shopping")]:
        d = tmp_path / "images" / b / s
        d.mkdir(parents=True)
        (d / "image.toml").write_text("")
    # a service dir WITHOUT image.toml must be ignored
    (tmp_path / "images" / "webarena" / "notes").mkdir()
    return tmp_path

def test_all_finds_only_manifest_dirs(tmp_path):
    refs = find_images(make_repo(tmp_path), "all")
    assert sorted(r.name for r in refs) == ["miniwob-server", "webarena-gitlab", "webarena-shopping"]

def test_benchmark_target(tmp_path):
    refs = find_images(make_repo(tmp_path), "webarena")
    assert sorted(r.service for r in refs) == ["gitlab", "shopping"]

def test_service_target(tmp_path):
    refs = find_images(make_repo(tmp_path), "miniwob/server")
    assert [r.name for r in refs] == ["miniwob-server"]
    assert refs[0].path == tmp_path / "images" / "miniwob" / "server"

def test_unknown_target_fails_loud(tmp_path):
    with pytest.raises(SystemExit, match="no images match"):
        find_images(make_repo(tmp_path), "nope")
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_discover.py -v` → FAIL (`ModuleNotFoundError: builder`).

- [ ] **Step 3: Implement**

`builder/discover.py`:
```python
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class ImageRef:
    benchmark: str
    service: str
    path: Path

    @property
    def name(self) -> str:
        return f"{self.benchmark}-{self.service}"

def find_images(repo_root: Path, target: str) -> list[ImageRef]:
    refs = [
        ImageRef(p.parent.parent.name, p.parent.name, p.parent)
        for p in sorted((repo_root / "images").glob("*/*/image.toml"))
    ]
    if target != "all":
        bench, _, svc = target.partition("/")
        refs = [r for r in refs if r.benchmark == bench and (not svc or r.service == svc)]
    if not refs:
        raise SystemExit(f"error: no images match target '{target}' under {repo_root}/images")
    return refs
```

`builder/cli.py` (subcommands beyond `list` are wired in later tasks):
```python
import argparse
from pathlib import Path
from builder.discover import find_images

def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="build", description="benchmark image builder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "download", "build", "push", "smoke"):
        sp = sub.add_parser(name)
        sp.add_argument("target", help="all, <benchmark>, or <benchmark>/<service>")
        sp.add_argument("--registry", default="ghcr.io/shkolnik")
        sp.add_argument("--datasets-dir", type=Path, default=None)
    args = ap.parse_args(argv)
    refs = find_images(repo_root(), args.target)
    if args.cmd == "list":
        for r in refs:
            print(f"{r.benchmark}/{r.service}\t{r.name}")
        return 0
    raise SystemExit(f"error: '{args.cmd}' not implemented yet")
```

`bin/build`:
```python
#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from builder.cli import main
sys.exit(main())
```

`builder/__init__.py` empty. `.gitignore`:
```
datasets/
__pycache__/
*.quarantine
```
`README.md`: one paragraph (builder service for benchmark server images; see `docs/design.md`; usage `bin/build list|download|build|push|smoke <target>`).

- [ ] **Step 4: chmod + verify** — `chmod +x bin/build; python -m pytest tests/ -v` → PASS; `bin/build list all` from repo root prints nothing yet? (no images/ dir exists → expect the loud "no images match" exit — correct).

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: driver skeleton — glob discovery and list subcommand"` (+ session trailer).

---

### Task 2: Manifest loading and validation

**Files:**
- Create: `builder/manifest.py`
- Test: `tests/test_manifest.py`

**Interfaces:**
- Produces: `manifest.Dataset` (`filename: str`, `sha256: str`, `urls: list[str]`), `manifest.Manifest` (`datasets: list[Dataset]`, `healthcheck: str | None`, `build_args: dict[str,str]`), `manifest.load_manifest(image_dir: Path) -> Manifest` raising `SystemExit` on missing/invalid fields with path+field named.

- [ ] **Step 1: Write failing tests**

`tests/test_manifest.py`:
```python
from pathlib import Path
import pytest
from builder.manifest import load_manifest

GOOD = '''
[[datasets]]
filename = "data.tar.gz"
sha256 = "a" * 0  # replaced below
urls = ["https://mirror1/x", "https://mirror2/x"]

[service]
healthcheck = "http://localhost:8080/"

[build.args]
FOO = "bar"
'''.replace('"a" * 0  # replaced below', '"%s"' % ("a" * 64))

def write(tmp_path: Path, text: str) -> Path:
    (tmp_path / "image.toml").write_text(text)
    return tmp_path

def test_load_good(tmp_path):
    m = load_manifest(write(tmp_path, GOOD))
    assert m.datasets[0].filename == "data.tar.gz"
    assert len(m.datasets[0].sha256) == 64
    assert m.datasets[0].urls[1].endswith("mirror2/x")
    assert m.healthcheck == "http://localhost:8080/"
    assert m.build_args == {"FOO": "bar"}

def test_no_datasets_ok(tmp_path):
    m = load_manifest(write(tmp_path, "[service]\nhealthcheck='http://x/'\n"))
    assert m.datasets == []

def test_bad_sha256_fails_loud(tmp_path):
    bad = GOOD.replace("a" * 64, "nothex")
    with pytest.raises(SystemExit, match="sha256"):
        load_manifest(write(tmp_path, bad))

def test_missing_urls_fails_loud(tmp_path):
    bad = GOOD.replace('urls = ["https://mirror1/x", "https://mirror2/x"]', "urls = []")
    with pytest.raises(SystemExit, match="urls"):
        load_manifest(write(tmp_path, bad))

def test_missing_manifest_fails_loud(tmp_path):
    with pytest.raises(SystemExit, match="image.toml"):
        load_manifest(tmp_path)
```

- [ ] **Step 2: Run to verify failure** — FAIL (no module).

- [ ] **Step 3: Implement**

`builder/manifest.py`:
```python
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

@dataclass(frozen=True)
class Dataset:
    filename: str
    sha256: str
    urls: list[str]

@dataclass(frozen=True)
class Manifest:
    datasets: list[Dataset] = field(default_factory=list)
    healthcheck: str | None = None
    build_args: dict[str, str] = field(default_factory=dict)

def _die(path: Path, msg: str):
    raise SystemExit(f"error: {path / 'image.toml'}: {msg}")

def load_manifest(image_dir: Path) -> Manifest:
    p = image_dir / "image.toml"
    if not p.is_file():
        _die(image_dir, "image.toml not found")
    data = tomllib.loads(p.read_text())
    datasets = []
    for i, d in enumerate(data.get("datasets", [])):
        for key in ("filename", "sha256", "urls"):
            if key not in d:
                _die(image_dir, f"datasets[{i}] missing '{key}'")
        if not _SHA256.match(d["sha256"]):
            _die(image_dir, f"datasets[{i}].sha256 is not 64 lowercase hex chars")
        if not d["urls"]:
            _die(image_dir, f"datasets[{i}].urls is empty")
        datasets.append(Dataset(d["filename"], d["sha256"], list(d["urls"])))
    return Manifest(
        datasets=datasets,
        healthcheck=data.get("service", {}).get("healthcheck"),
        build_args={k: str(v) for k, v in data.get("build", {}).get("args", {}).items()},
    )
```

- [ ] **Step 4: Run** — `python -m pytest tests/ -v` → PASS.
- [ ] **Step 5: Commit** — `feat: image.toml manifest loading with loud validation`.

---

### Task 3: Download — cache, verify, quarantine, retry/mirror rotation

**Files:**
- Create: `builder/download.py`
- Modify: `builder/cli.py` (wire `download`)
- Test: `tests/test_download.py`

**Interfaces:**
- Consumes: `Dataset` from Task 2, `ImageRef`/`find_images` from Task 1.
- Produces: `download.sha256_of(path: Path) -> str`; `download.ensure_dataset(ds: Dataset, datasets_dir: Path, fetch=fetch_curl, attempts_per_url: int = 3, log=print) -> Path`; `download.fetch_curl(url: str, dest: Path) -> None` (raises `FetchError` on failure); `download.FetchError(Exception)`; `download.default_datasets_dir(repo_root) -> Path` (= `repo_root/"datasets"`); `download.run_download(refs, datasets_dir, fetch=fetch_curl)` used by the CLI.

- [ ] **Step 1: Write failing tests**

`tests/test_download.py`:
```python
import hashlib
from pathlib import Path
import pytest
from builder.manifest import Dataset
from builder.download import ensure_dataset, sha256_of, FetchError

PAYLOAD = b"benchmark bytes"
GOOD_SHA = hashlib.sha256(PAYLOAD).hexdigest()

def ds(urls):
    return Dataset(filename="d.bin", sha256=GOOD_SHA, urls=urls)

def writer(payload):
    def fetch(url, dest: Path):
        dest.write_bytes(payload)
    return fetch

def test_downloads_and_verifies(tmp_path):
    out = ensure_dataset(ds(["u1"]), tmp_path, fetch=writer(PAYLOAD))
    assert out.read_bytes() == PAYLOAD

def test_cached_copy_skips_fetch(tmp_path):
    (tmp_path / "d.bin").write_bytes(PAYLOAD)
    def boom(url, dest):
        raise AssertionError("fetch called despite valid cache")
    assert ensure_dataset(ds(["u1"]), tmp_path, fetch=boom).read_bytes() == PAYLOAD

def test_mirror_rotation(tmp_path):
    calls = []
    def flaky(url, dest: Path):
        calls.append(url)
        if url == "bad":
            raise FetchError("conn reset")
        dest.write_bytes(PAYLOAD)
    out = ensure_dataset(ds(["bad", "good"]), tmp_path, fetch=flaky, attempts_per_url=1)
    assert out.read_bytes() == PAYLOAD and calls == ["bad", "good"]

def test_retries_same_url(tmp_path):
    calls = []
    def flaky(url, dest: Path):
        calls.append(url)
        if len(calls) < 2:
            raise FetchError("timeout")
        dest.write_bytes(PAYLOAD)
    ensure_dataset(ds(["u1"]), tmp_path, fetch=flaky, attempts_per_url=3)
    assert calls == ["u1", "u1"]

def test_checksum_mismatch_quarantines_and_dies(tmp_path):
    with pytest.raises(SystemExit, match="sha256 mismatch.*d.bin"):
        ensure_dataset(ds(["u1"]), tmp_path, fetch=writer(b"corrupt"))
    assert (tmp_path / "d.bin.quarantine").read_bytes() == b"corrupt"
    assert not (tmp_path / "d.bin").exists()

def test_stale_cache_requarantined_and_refetched(tmp_path):
    (tmp_path / "d.bin").write_bytes(b"old corrupt")
    out = ensure_dataset(ds(["u1"]), tmp_path, fetch=writer(PAYLOAD))
    assert out.read_bytes() == PAYLOAD

def test_all_mirrors_exhausted_dies_naming_urls(tmp_path):
    def dead(url, dest):
        raise FetchError("404")
    with pytest.raises(SystemExit, match="u1.*u2"):
        ensure_dataset(ds(["u1", "u2"]), tmp_path, fetch=dead, attempts_per_url=2)
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement**

`builder/download.py`:
```python
import hashlib
import subprocess
from pathlib import Path
from builder.manifest import Dataset

class FetchError(Exception):
    pass

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()

def fetch_curl(url: str, dest: Path) -> None:
    # -C - resumes a partial dest from a previous attempt; --fail turns HTTP
    # errors into exit codes instead of saving an error page as the dataset.
    proc = subprocess.run(
        ["curl", "--fail", "--location", "--continue-at", "-",
         "--connect-timeout", "30", "--output", str(dest), url])
    if proc.returncode != 0:
        raise FetchError(f"curl exited {proc.returncode} for {url}")

def default_datasets_dir(repo_root: Path) -> Path:
    return repo_root / "datasets"

def _quarantine(path: Path, log):
    q = path.with_name(path.name + ".quarantine")
    path.replace(q)
    log(f"quarantined bad file to {q}")

def ensure_dataset(ds: Dataset, datasets_dir: Path, fetch=fetch_curl,
                   attempts_per_url: int = 3, log=print) -> Path:
    datasets_dir.mkdir(parents=True, exist_ok=True)
    final = datasets_dir / ds.filename
    if final.exists():
        if sha256_of(final) == ds.sha256:
            log(f"{ds.filename}: cached and verified, skipping download")
            return final
        log(f"{ds.filename}: cached copy fails verification")
        _quarantine(final, log)
    part = datasets_dir / (ds.filename + ".part")
    errors = []
    for url in ds.urls:
        for attempt in range(1, attempts_per_url + 1):
            try:
                log(f"{ds.filename}: fetching {url} (attempt {attempt}/{attempts_per_url})")
                fetch(url, part)
            except FetchError as e:
                errors.append(f"{url}: {e}")
                continue
            size = part.stat().st_size if part.exists() else 0
            if not part.exists() or sha256_of(part) != ds.sha256:
                _quarantine(part if part.exists() else final, log) if part.exists() else None
                if part.with_name(part.name + ".quarantine").exists():
                    part.with_name(part.name + ".quarantine").replace(
                        final.with_name(final.name + ".quarantine"))
                raise SystemExit(
                    f"error: sha256 mismatch for {ds.filename} from {url} "
                    f"({size} bytes) — quarantined, not passed to docker")
            part.replace(final)
            log(f"{ds.filename}: verified ({size} bytes)")
            return final
    raise SystemExit(
        f"error: all mirrors exhausted for {ds.filename}: " + "; ".join(errors))

def run_download(refs, datasets_dir: Path, fetch=fetch_curl) -> None:
    from builder.manifest import load_manifest
    for ref in refs:
        for ds in load_manifest(ref.path).datasets:
            ensure_dataset(ds, datasets_dir, fetch=fetch)
```

Note for implementer: the mismatch-quarantine block above is awkward as sketched — simplify while keeping behavior (mismatched `.part` must end up at `<filename>.quarantine`, `final` must not exist, message must include filename+url+bytes). The tests define the contract; make them pass with clean code.

Wire in `builder/cli.py` (replace the final `raise SystemExit` line):
```python
    from builder.download import default_datasets_dir, run_download
    dsdir = args.datasets_dir or default_datasets_dir(repo_root())
    if args.cmd == "download":
        run_download(refs, dsdir)
        return 0
    raise SystemExit(f"error: '{args.cmd}' not implemented yet")
```

- [ ] **Step 4: Run** — `python -m pytest tests/ -v` → all PASS.
- [ ] **Step 5: Commit** — `feat: dataset download with cache, sha256 verify, quarantine, mirror rotation`.

---

### Task 4: `build` and `push` subcommands

**Files:**
- Create: `builder/docker.py`
- Modify: `builder/cli.py`
- Test: `tests/test_docker.py`

**Interfaces:**
- Consumes: `ImageRef`, `Manifest`.
- Produces: `docker.version_tag(repo_root: Path) -> str` (yyyymmdd.shortsha via `git -C repo_root rev-parse --short HEAD` + `date`), `docker.build_cmd(ref, manifest, registry, datasets_dir, version) -> list[str]`, `docker.push_cmds(ref, registry, version) -> list[list[str]]`, `docker.run(cmd: list[str])` (subprocess, `SystemExit` on nonzero naming the command), `docker.run_build(refs, ...)` / `docker.run_push(refs, ...)` for the CLI.

- [ ] **Step 1: Write failing tests** (pure command-construction; no docker needed)

`tests/test_docker.py`:
```python
from pathlib import Path
from builder.discover import ImageRef
from builder.manifest import Manifest
from builder.docker import build_cmd, push_cmds

REF = ImageRef("miniwob", "server", Path("/repo/images/miniwob/server"))

def test_build_cmd():
    m = Manifest(build_args={"FOO": "bar"})
    cmd = build_cmd(REF, m, "ghcr.io/shkolnik", Path("/repo/datasets"), "20260805.abc1234")
    assert cmd == [
        "docker", "build",
        "--build-context", "datasets=/repo/datasets",
        "--build-arg", "FOO=bar",
        "-t", "ghcr.io/shkolnik/miniwob-server:20260805.abc1234",
        "-t", "ghcr.io/shkolnik/miniwob-server:latest",
        "/repo/images/miniwob/server",
    ]

def test_push_cmds():
    cmds = push_cmds(REF, "localhost:5000", "20260805.abc1234")
    assert cmds == [
        ["docker", "push", "localhost:5000/miniwob-server:20260805.abc1234"],
        ["docker", "push", "localhost:5000/miniwob-server:latest"],
    ]
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement**

`builder/docker.py`:
```python
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
```

Wire into `cli.py` (before the not-implemented line):
```python
    from builder import docker
    if args.cmd == "build":
        docker.run_build(refs, args.registry, dsdir, repo_root())
        return 0
    if args.cmd == "push":
        docker.run_push(refs, args.registry, repo_root())
        return 0
```

- [ ] **Step 4: Run** — unit tests PASS.
- [ ] **Step 5: Commit** — `feat: build and push subcommands`.

---

### Task 5: `smoke` subcommand

**Files:**
- Modify: `builder/docker.py`, `builder/cli.py`
- Test: `tests/test_docker.py` (append)

**Interfaces:**
- Produces: `docker.poll_health(url: str, timeout_s: int = 120, opener=urllib.request.urlopen, sleep=time.sleep) -> bool`; `docker.run_smoke(refs, repo_root) -> None` — groups refs by benchmark, for each: `docker compose -f images/<benchmark>/compose.yml up -d --build=false`... (see impl), polls each service's `healthcheck` from its manifest, always `compose down -v`, `SystemExit` naming the first unhealthy service.

- [ ] **Step 1: Write failing tests** (append to `tests/test_docker.py`)

```python
from builder.docker import poll_health

class FakeResp:
    def __init__(self, code): self.status = code
    def __enter__(self): return self
    def __exit__(self, *a): return False

def test_poll_health_succeeds_after_flaps():
    seq = [ConnectionRefusedError(), FakeResp(500), FakeResp(200)]
    def opener(url, timeout):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    assert poll_health("http://x/", timeout_s=10, opener=opener, sleep=lambda s: None)

def test_poll_health_times_out():
    def opener(url, timeout):
        raise ConnectionRefusedError()
    assert not poll_health("http://x/", timeout_s=0, opener=opener, sleep=lambda s: None)
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement** (append to `builder/docker.py`)

```python
import time
import urllib.request
import urllib.error

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
```

Wire `smoke` into cli.py: `docker.run_smoke(refs, repo_root()); return 0`. Delete the now-unreachable `not implemented` line.

- [ ] **Step 4: Run** — `python -m pytest tests/ -v` → PASS.
- [ ] **Step 5: Commit** — `feat: smoke subcommand — compose up, healthcheck poll, always down`.

---

### Task 6: End-to-end integration test (docker + local registry)

**Files:**
- Create: `tests/integration/test_end_to_end.py`, `tests/integration/__init__.py`

**Interfaces:** Consumes everything above; produces the confidence that the pipeline works against real docker. Skipped cleanly when docker is unavailable.

- [ ] **Step 1: Write the test**

```python
import shutil
import subprocess
import hashlib
from pathlib import Path
import pytest
from builder.discover import ImageRef
from builder.manifest import Dataset, Manifest
from builder import docker as bdocker
from builder.download import ensure_dataset

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or subprocess.run(
        ["docker", "info"], capture_output=True).returncode != 0,
    reason="docker unavailable")

def test_download_build_push_roundtrip(tmp_path):
    payload = b"hello dataset\n"
    src = tmp_path / "srv"; src.mkdir(); (src / "d.bin").write_bytes(payload)
    ds = Dataset("d.bin", hashlib.sha256(payload).hexdigest(),
                 [f"file://{src}/d.bin"])
    dsdir = tmp_path / "datasets"
    # file:// fetch via curl exercises the real fetch path
    ensure_dataset(ds, dsdir)

    imgdir = tmp_path / "images" / "itest" / "svc"; imgdir.mkdir(parents=True)
    (imgdir / "Dockerfile").write_text(
        "FROM busybox\nCOPY --from=datasets d.bin /d.bin\nCMD cat /d.bin\n")
    (imgdir / "image.toml").write_text("")
    ref = ImageRef("itest", "svc", imgdir)

    reg = subprocess.run(["docker", "run", "-d", "-p", "127.0.0.1:5000:5000",
                          "registry:2"], capture_output=True, text=True, check=True).stdout.strip()
    try:
        version = "itest.1"
        bdocker.run(bdocker.build_cmd(ref, Manifest(), "127.0.0.1:5000", dsdir, version))
        for cmd in bdocker.push_cmds(ref, "127.0.0.1:5000", version):
            bdocker.run(cmd)
        out = subprocess.run(
            ["docker", "run", "--rm", f"127.0.0.1:5000/itest-svc:{version}"],
            capture_output=True, text=True, check=True).stdout
        assert out == payload.decode()
    finally:
        subprocess.run(["docker", "rm", "-f", reg], capture_output=True)
        subprocess.run(["docker", "rmi", "-f",
                        f"127.0.0.1:5000/itest-svc:{version}",
                        "127.0.0.1:5000/itest-svc:latest"], capture_output=True)
```

- [ ] **Step 2: Run** — `python -m pytest tests/integration -v` → PASS on this sandbox (docker present). If `COPY --from=datasets` errors, ensure BuildKit (`DOCKER_BUILDKIT=1` env in `docker.run` for build commands) — add it to `build_cmd`'s execution env if needed and note it.
- [ ] **Step 3: Commit** — `test: end-to-end integration — download, build with dataset context, push to local registry, run`.

---

### Task 7: M0 probe assets

**Files:**
- Create: `images/probe/synthetic/{image.toml,Dockerfile,generate-dataset.sh}`, `images/probe/compose.yml`, `docs/registry-limits.md`

**Interfaces:** Consumes the driver as-is. The probe dataset is generated locally (not downloaded), so `image.toml` has **no** `[[datasets]]` entries; `generate-dataset.sh` writes chunk files straight into `datasets/`.

- [ ] **Step 1: Write the generator**

`images/probe/synthetic/generate-dataset.sh`:
```bash
#!/usr/bin/env bash
# Generate incompressible probe chunks into the datasets cache.
# Usage: generate-dataset.sh <datasets-dir> <num-chunks> <chunk-gb>
set -euo pipefail
dsdir=$1 n=$2 gb=$3
mkdir -p "$dsdir"
for i in $(seq -w 1 "$n"); do
  f="$dsdir/probe-chunk-$i.bin"
  if [ -s "$f" ]; then echo "exists: $f"; continue; fi
  echo "generating $f (${gb}G random)"
  head -c "${gb}G" /dev/urandom > "$f.part" && mv "$f.part" "$f"
done
```

`images/probe/synthetic/Dockerfile` (one `COPY` per chunk = one layer per chunk — the layer-size knob; ARG-driven so small local tests and the 120G run share it):
```dockerfile
# syntax=docker/dockerfile:1
FROM busybox
# Layer-per-chunk on purpose: the probe measures per-layer registry behavior.
COPY --from=datasets probe-chunk-*.bin /data/
CMD ls -l /data
```
(Note: a single `COPY` of the glob is ONE layer. To get layer-per-chunk the Dockerfile needs N explicit `COPY` lines — have `generate-dataset.sh` also emit `Dockerfile.generated` with one `COPY --from=datasets probe-chunk-<i>.bin /data/` line per chunk, and document `docker build -f`. Implementer: do the generated-Dockerfile variant; keep the committed Dockerfile as the small default with 2 chunks.)

`images/probe/synthetic/image.toml`:
```toml
[service]
healthcheck = "http://localhost:1/"  # probe is not a server; smoke is N/A — see compose
```
Correction: probe is not runnable; simplest honest shape is **no compose service and no healthcheck**: `image.toml` content just `# probe image: no datasets (generated), no service` — and `smoke` will fail loud on it, which is correct and documented in `docs/registry-limits.md`. `images/probe/compose.yml` is then NOT created. (Implementer: manifest with zero datasets and no healthcheck is valid per Task 2 tests.)

`docs/registry-limits.md`: table skeleton with columns Registry | total size | chunk size | result | per-layer errors | push wall-time | pull-back verified | date, plus a "How to run the probe" section:
```
bin/build download probe   # no-op (no datasets)
images/probe/synthetic/generate-dataset.sh datasets/ 12 10
bin/build build probe --datasets-dir datasets/
bin/build push probe --registry ghcr.io/shkolnik   # and --registry docker.io/<ns>
```

- [ ] **Step 2: Verify locally at small scale** — run the generator with `2` chunks of `1`G, `bin/build build probe`, push to a local `registry:2`, `docker pull` back, record the run works. Do NOT generate 120G in this sandbox as part of the task.
- [ ] **Step 3: Commit** — `feat(probe): M0 synthetic registry-size probe assets and findings template`.

---

### Task 8: M1 MiniWoB++ benchmark

**Files:**
- Create: `images/miniwob/server/{image.toml,Dockerfile,nginx.conf}`, `images/miniwob/compose.yml`

**Interfaces:** Consumes the whole driver. Produces the first real, smoke-tested benchmark.

- [ ] **Step 1: Pin the dataset.** MiniWoB++ is static HTML/JS from Farama-Foundation/miniwob-plusplus. Pick the latest release tag (check `https://github.com/Farama-Foundation/miniwob-plusplus/tags`; v1.0 era). Download the tag archive once with curl, compute `sha256sum`, and write the REAL values into `image.toml`:

```toml
[[datasets]]
filename = "miniwob-plusplus.tar.gz"
sha256 = "<paste the sha256sum output here — computed in this step, not invented>"
urls = ["https://github.com/Farama-Foundation/miniwob-plusplus/archive/refs/tags/<tag>.tar.gz"]

[service]
healthcheck = "http://localhost:8399/miniwob/click-button.html"
```
(Verify the healthcheck path against the archive layout — the HTML tasks live under `miniwob/html/miniwob/` in the repo; adjust the nginx root/path so `click-button.html` resolves. GitHub tag archives are stable per tag in practice; note in the manifest comment that a checksum break means the tag moved — investigate, don't bump blindly.)

- [ ] **Step 2: Dockerfile + nginx**

`images/miniwob/server/Dockerfile`:
```dockerfile
# syntax=docker/dockerfile:1
FROM nginx:alpine
COPY --from=datasets miniwob-plusplus.tar.gz /tmp/
RUN mkdir -p /usr/share/nginx/miniwob \
 && tar -xzf /tmp/miniwob-plusplus.tar.gz --strip-components=2 \
      -C /usr/share/nginx/miniwob "$(tar -tzf /tmp/miniwob-plusplus.tar.gz | head -1)html" \
 && rm /tmp/miniwob-plusplus.tar.gz
COPY nginx.conf /etc/nginx/conf.d/default.conf
```
(The `--strip-components`/path dance depends on the archive layout — implementer verifies against the real tarball and simplifies; the requirement is: task HTML served at `/miniwob/<task>.html`, port 8399 inside the container.)

`images/miniwob/server/nginx.conf`:
```nginx
server {
    listen 8399;
    root /usr/share/nginx/miniwob;
    autoindex on;
}
```

`images/miniwob/compose.yml`:
```yaml
services:
  server:
    image: ghcr.io/shkolnik/miniwob-server:latest
    ports:
      - "8399:8399"
```

- [ ] **Step 3: Run the full pipeline** — `bin/build download miniwob && bin/build build miniwob && bin/build smoke miniwob` → smoke reports healthy. Then `curl http://localhost:8399/...` manually once during `docker compose up` to eyeball a real task page renders (fetch the HTML, check it contains the task scaffold).
- [ ] **Step 4: Commit** — `feat(miniwob): M1 — first real benchmark image, full pipeline green`.

---

### Task 9: CI workflow

**Files:**
- Create: `.github/workflows/build.yml`

**Interfaces:** Consumes `bin/build`. Cannot run until James creates the GitHub repo + self-hosted runner; syntax-check only.

- [ ] **Step 1: Write the workflow**

```yaml
name: build-images
on:
  workflow_dispatch:
    inputs:
      target:
        description: "all, <benchmark>, or <benchmark>/<service>"
        default: all
  push:
    branches: [main]
    paths: ["images/**", "builder/**", "bin/**"]

jobs:
  discover:
    runs-on: [self-hosted, benchmark-builder]
    outputs:
      images: ${{ steps.ls.outputs.images }}
    steps:
      - uses: actions/checkout@v4
      - id: ls
        run: |
          target='${{ github.event.inputs.target || 'all' }}'
          echo "images=$(bin/build list "$target" | cut -f1 | jq -R -s -c 'split("\n")[:-1]')" >> "$GITHUB_OUTPUT"

  build:
    needs: discover
    runs-on: [self-hosted, benchmark-builder]
    strategy:
      fail-fast: false
      max-parallel: 1   # one huge build at a time; the disk is the constraint
      matrix:
        image: ${{ fromJSON(needs.discover.outputs.images) }}
    steps:
      - uses: actions/checkout@v4
      - name: login to ghcr
        run: echo '${{ secrets.GITHUB_TOKEN }}' | docker login ghcr.io -u '${{ github.actor }}' --password-stdin
      - name: download datasets (cached on runner)
        run: bin/build download '${{ matrix.image }}' --datasets-dir "$HOME/benchmark-datasets"
      - name: build
        run: bin/build build '${{ matrix.image }}' --datasets-dir "$HOME/benchmark-datasets"
      - name: push
        if: github.event_name == 'push' || github.event.inputs.target != ''
        run: bin/build push '${{ matrix.image }}'
```
(Change-detection on push is deliberately NOT implemented in v1 — the download cache + docker layer cache make a no-change rebuild cheap, and it keeps the workflow index-free. Note this in a comment. The probe image is excluded from CI by having its `image.toml` present but its dataset generation manual — `download` is a no-op and `build` with no chunks present fails loud; acceptable for v1, noted in registry-limits.md.)

Wait — that makes CI red on probe. Fix: name the probe benchmark dir `probe/` and add to the discover step: `bin/build list "$target" | cut -f1 | grep -v '^probe/'` (one grep in the workflow, not a root index; removing the probe dir later requires deleting that grep — acceptable, it's the workflow's own concern). Include this in the committed yaml with a comment.

- [ ] **Step 2: Syntax check** — `actionlint` if available, else `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/build.yml'))"` (pyyaml available on sandbox; if not, `docker run --rm -v $PWD:/repo rhysd/actionlint` or skip with a note).
- [ ] **Step 3: Commit** — `ci: matrix build workflow for self-hosted benchmark-builder runner`.

---

## Verification (whole plan)

- `python -m pytest tests/ -v` green including integration.
- `bin/build list all` shows probe + miniwob.
- `bin/build download miniwob && bin/build build miniwob && bin/build smoke miniwob` green end to end.
- Litmus re-check: removing miniwob = `git rm -r images/miniwob` (nothing else); removing the probe = its dir + one grep line in build.yml.
