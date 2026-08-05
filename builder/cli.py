import argparse
from pathlib import Path
from builder.discover import find_images

def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="build", description="benchmark image builder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "download", "build", "push", "smoke", "clean"):
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

    from builder.download import default_datasets_dir, run_download
    dsdir = args.datasets_dir or default_datasets_dir(repo_root())
    if args.cmd == "download":
        run_download(refs, dsdir)
        return 0

    from builder import docker
    if args.cmd == "build":
        docker.run_build(refs, args.registry, dsdir, repo_root())
        return 0
    if args.cmd == "push":
        docker.run_push(refs, args.registry, repo_root())
        return 0
    if args.cmd == "clean":
        docker.run_clean(refs, args.registry, repo_root())
        return 0
    docker.run_smoke(refs, repo_root())
    return 0
