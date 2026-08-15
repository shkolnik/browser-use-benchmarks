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

def order_by_cost(refs: list[ImageRef]) -> list[ImageRef]:
    """Longest build first — the order CI should enqueue the matrix in.

    The fleet has more images than the vCPU quota has runner slots, so the jobs
    that do not fit start as earlier ones finish. Which jobs those are is
    decided entirely by matrix order, and the default — alphabetical, from
    find_images' glob — put the 0-minute image first and a 70-minute one tenth.
    Longest-first is the standard makespan heuristic and needs nothing but the
    order the array is written in.

    Ties break by name so the order is stable: an unstable matrix would make
    two runs of the same commit hard to compare against each other.
    """
    from builder.manifest import load_manifest

    def key(ref: ImageRef):
        minutes = load_manifest(ref.path).build_minutes
        # Unmeasured first: see Manifest.build_minutes for why the unknown case
        # is scheduled as if it were expensive.
        return (minutes is not None, -(minutes or 0), ref.benchmark, ref.service)

    return sorted(refs, key=key)
