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
