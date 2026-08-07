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
    # True = only the prepare script consumes this, and only when its GHCR
    # derived cache misses. The normal download path skips it; the script
    # fetches it on demand. Without this the build downloads e.g. shopping's
    # 67.6G upstream tar on every cold runner and then never opens it, because
    # the cache check lives inside the script and runs long after download.
    prepare_input: bool = False

@dataclass(frozen=True)
class Source:
    kind: str  # "build" (docker build from Dockerfile) or "docker-save" (load + re-tag a tar)
    dataset: str | None = None  # docker-save only: datasets[].filename of the docker-save tar
    tag: str | None = None      # docker-save only: the image tag embedded in the tar's manifest.json

@dataclass(frozen=True)
class Prepare:
    script: str          # runs from the image dir after download, before build
    outputs: list[str]   # files it must leave in the datasets dir; all present = skip


@dataclass(frozen=True)
class Manifest:
    datasets: list[Dataset] = field(default_factory=list)
    healthcheck: str | None = None
    build_args: dict[str, str] = field(default_factory=dict)
    source: Source = Source(kind="build")
    prepare: Prepare | None = None

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
        datasets.append(Dataset(d["filename"], d["sha256"], list(d["urls"]),
                                prepare_input=bool(d.get("prepare_input", False))))
    src = data.get("source", {})
    kind = src.get("kind", "build")
    if kind not in ("build", "docker-save"):
        _die(image_dir, f"source.kind must be 'build' or 'docker-save', got '{kind}'")
    if kind == "docker-save":
        for key in ("dataset", "tag"):
            if key not in src:
                _die(image_dir, f"source.{key} is required when source.kind = 'docker-save'")
        if src["dataset"] not in {d.filename for d in datasets}:
            _die(image_dir,
                 f"source.dataset '{src['dataset']}' does not match any datasets[].filename")
        if data.get("build"):
            _die(image_dir, "docker-save images take no [build] section — nothing is built")
    elif src and set(src) != {"kind"}:
        _die(image_dir, "source.dataset/source.tag are only valid with kind = 'docker-save'")
    prepare = None
    prep = data.get("prepare")
    if prep is not None:
        if kind != "build":
            _die(image_dir, "[prepare] is only valid with source.kind = 'build'")
        for key in ("script", "outputs"):
            if key not in prep:
                _die(image_dir, f"prepare missing '{key}'")
        if not (image_dir / prep["script"]).is_file():
            _die(image_dir, f"prepare.script '{prep['script']}' not found in image dir")
        if not prep["outputs"]:
            _die(image_dir, "prepare.outputs is empty")
        prepare = Prepare(prep["script"], list(prep["outputs"]))
    lazy = [d.filename for d in datasets if d.prepare_input]
    # More than one is allowed: a cache that still sends you back to the
    # upstream mirror for a second file is not a checkpoint you can rebuild
    # forward from, however small that file is. run_prepare represents the
    # whole set in PREPARE_INPUTS_DIGEST (and withholds the singular
    # PREPARE_INPUT_SHA256), so no key can name a partial identity.
    if lazy and prepare is None:
        _die(image_dir,
             f"datasets marked prepare_input ({', '.join(lazy)}) but there is no "
             "[prepare] section to fetch them — nothing would ever download them")
    return Manifest(
        datasets=datasets,
        healthcheck=data.get("service", {}).get("healthcheck"),
        build_args={k: str(v) for k, v in data.get("build", {}).get("args", {}).items()},
        source=Source(kind=kind, dataset=src.get("dataset"), tag=src.get("tag")),
        prepare=prepare,
    )
