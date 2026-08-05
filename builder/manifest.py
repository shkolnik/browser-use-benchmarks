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
class Source:
    kind: str  # "build" (docker build from Dockerfile) or "docker-save" (load + re-tag a tar)
    dataset: str | None = None  # docker-save only: datasets[].filename of the docker-save tar
    tag: str | None = None      # docker-save only: the image tag embedded in the tar's manifest.json

@dataclass(frozen=True)
class Manifest:
    datasets: list[Dataset] = field(default_factory=list)
    healthcheck: str | None = None
    build_args: dict[str, str] = field(default_factory=dict)
    source: Source = Source(kind="build")

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
    return Manifest(
        datasets=datasets,
        healthcheck=data.get("service", {}).get("healthcheck"),
        build_args={k: str(v) for k, v in data.get("build", {}).get("args", {}).items()},
        source=Source(kind=kind, dataset=src.get("dataset"), tag=src.get("tag")),
    )
