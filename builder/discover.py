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
    """Longest build first — the order CI enqueues the matrix in.

    The fleet has more images than the vCPU quota has runner slots, so the jobs
    that do not fit start as earlier ones finish. Which jobs those are is NOT
    decided by this order: every matrix job carries the same
    `[self-hosted, burst]` label, one homogeneous pool, so a runner that
    registers claims whichever queued job the scheduler hands it. Pickup is the
    registration race.

    So this is a cost signal, not a schedule. It is kept because it costs
    nothing but the order the array is written in, and because anything that
    does control placement — a runner supply that boots one VM per named job —
    reads its priority off exactly this. The race itself is cheap to lose here:
    the makespan is pinned by the single longest image, which is well above
    total work divided over the slots, so even a worst-case pickup order lands
    within a few minutes of the longest-first optimum.

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
