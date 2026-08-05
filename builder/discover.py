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
