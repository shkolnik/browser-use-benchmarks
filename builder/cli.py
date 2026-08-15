import argparse
from pathlib import Path
from builder.discover import find_images

def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="build", description="benchmark image builder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "download", "build", "push", "promote", "smoke",
                 "clean", "published-revision"):
        sp = sub.add_parser(name)
        sp.add_argument("target", help="all, <benchmark>, or <benchmark>/<service>")
        sp.add_argument("--registry", default="ghcr.io/shkolnik")
        sp.add_argument("--datasets-dir", type=Path, default=None)
        if name == "download":
            sp.add_argument(
                "--prepare-inputs", action="store_true",
                help="fetch ONLY datasets marked prepare_input (what a derive "
                     "script calls on its cache-miss path); default fetches "
                     "everything else")
    args = ap.parse_args(argv)
    refs = find_images(repo_root(), args.target)
    if args.cmd == "list":
        for r in refs:
            print(f"{r.benchmark}/{r.service}\t{r.name}")
        return 0

    if args.cmd == "published-revision":
        # One line per image, always — a blank second field says "nothing
        # published, or published without a revision", which the caller must
        # treat exactly like an unknown anchor. Reporting per image rather
        # than per invocation is what keeps the eleven answers independent.
        from builder.registry import Unavailable, published_revision
        import sys
        for r in refs:
            try:
                rev = published_revision(f"{args.registry}/{r.name}:latest") or ""
            except Unavailable as e:
                # Not fatal: an unreachable registry means every image looks
                # unbuilt and the run rebuilds everything, which is correct
                # but expensive, so it is said out loud rather than inferred
                # from an unexplained full rebuild.
                print(f"warning: cannot read {r.name}: {e}", file=sys.stderr)
                rev = ""
            print(f"{r.benchmark}/{r.service}\t{rev}")
        return 0

    from builder.download import default_datasets_dir, run_download
    dsdir = args.datasets_dir or default_datasets_dir(repo_root())
    if args.cmd == "download":
        run_download(refs, dsdir, prepare_inputs=args.prepare_inputs)
        return 0

    from builder import docker
    if args.cmd == "build":
        docker.run_build(refs, args.registry, dsdir, repo_root())
        return 0
    if args.cmd == "push":
        docker.run_push(refs, args.registry, repo_root(), dsdir)
        return 0
    if args.cmd == "promote":
        docker.run_promote(refs, args.registry)
        return 0
    if args.cmd == "clean":
        docker.run_clean(refs, args.registry, repo_root())
        return 0
    docker.run_smoke(refs, repo_root())
    return 0
