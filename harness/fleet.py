"""Where each service answers, composed rather than restated.

A suite manifest maps a placeholder token to a SERVICE — "webarena/shopping" —
and never to a URL. The URL is composed here from three facts that each live in
exactly one place:

  the published port      deploy/compose.yml's ports mapping
  the path under it       that service's image.toml [service].base_path
  the host                the caller's, because BENCH_HOST has no default and is
                          baked into what several of these images serve

Restating any of them in a manifest would be a second copy of a pin, which
docs/design.md calls a silent-wrong-data hole rather than redundancy.

The images/<benchmark>/<service> name is derived from each compose entry's image
reference instead of a hand-kept table, so adding a service to the fleet is one
edit and not two — the same no-index-files rule the rest of the repo follows.
"""

from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover - environment, not logic
    raise SystemExit(
        "error: harness.fleet needs PyYAML because deploy/compose.yml is YAML. "
        "`pip install pyyaml`. harness.suite and harness.results are stdlib-only "
        "and do not need it."
    ) from exc


@dataclass(frozen=True)
class Service:
    name: str          # "webarena/shopping", the way images/*/*/ names it
    compose_name: str  # "shopping", the way deploy/compose.yml names it
    image: str         # "ghcr.io/shkolnik/webarena-shopping:latest"
    port: int          # the PUBLISHED port's default
    port_var: str      # the variable that overrides it, e.g. "SHOPPING_PORT"
    base_path: str     # "" for the ones served at the root

    def base_url(self, host: str, port: int | None = None) -> str:
        """The address a client types, including the path the app lives under.

        A port number alone cannot reconstruct this: shopping-admin serves the
        storefront at the root and adminhtml under /admin on the same port, so a
        caller that templates a path onto host:7780 reaches the storefront —
        which answers 200, the wrong app rather than an error.
        """
        return f"http://{host}:{port or self.port}{self.base_path}"


def split_ports(mapping: str) -> list[str]:
    """Split a compose ports string on ':' — ignoring ':' inside ${...}.

    "${BIND_ADDR:-0.0.0.0}:${GITLAB_PORT:-8023}:${GITLAB_PORT:-8023}" has six
    colons and three fields; a plain split gets this wrong in exactly the case
    that matters, since the default-value syntax is itself colon-bearing.
    """
    fields, buf, depth = [], "", 0
    for ch in str(mapping):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == ":" and depth == 0:
            fields.append(buf)
            buf = ""
        else:
            buf += ch
    fields.append(buf)
    return fields


def _var_and_default(field: str) -> tuple[str, str]:
    """Read `${NAME:-default}` into ("NAME", "default"); a literal into ("", it)."""
    field = field.strip()
    if field.startswith("${") and field.endswith("}"):
        body = field[2:-1]
        name, sep, default = body.partition(":-")
        return name, (default if sep else "")
    return "", field


def image_to_service_name(image: str) -> str:
    """"ghcr.io/shkolnik/webarena-shopping-admin:latest" -> "webarena/shopping-admin".

    builder/discover.py builds the image name as f"{benchmark}-{service}", so
    the split is at the FIRST hyphen and never the last: shopping-admin is one
    service of the webarena benchmark, not an admin service of webarena-shopping.
    """
    short = image.rsplit("/", 1)[-1].split(":", 1)[0]
    benchmark, _, service = short.partition("-")
    return f"{benchmark}/{service}"


def load_fleet(repo_root: Path) -> dict[str, Service]:
    """Every service deploy/compose.yml brings up, keyed the way images/ names it."""
    repo_root = Path(repo_root)
    compose = repo_root / "deploy" / "compose.yml"
    if not compose.is_file():
        raise SystemExit(f"error: {compose} not found")
    services = yaml.safe_load(compose.read_text()).get("services") or {}

    out = {}
    for compose_name, spec in services.items():
        image = str(spec.get("image", ""))
        if not image:
            # The proxy overlay's own container has no image here; a base-file
            # entry without one is a compose file this cannot describe.
            raise SystemExit(
                f"error: {compose}: service {compose_name!r} declares no image, so "
                "nothing says which images/ directory it is")
        name = image_to_service_name(image)
        ports = spec.get("ports") or []
        if not ports:
            raise SystemExit(
                f"error: {compose}: service {compose_name!r} publishes no port, so no "
                "client address exists for it")
        # The published side is the second-to-last field: host:published:container,
        # or published:container when no bind address is given.
        fields = split_ports(ports[0])
        port_var, default = _var_and_default(fields[-2])
        if not default.isdigit():
            raise SystemExit(
                f"error: {compose}: {compose_name!r} publishes {fields[-2]!r}, which has "
                "no numeric default, so nothing says where it answers unless the "
                "variable is set")
        out[name] = Service(
            name=name,
            compose_name=compose_name,
            image=image,
            port=int(default),
            port_var=port_var,
            base_path=_base_path(repo_root, name),
        )
    return out


def _base_path(repo_root: Path, name: str) -> str:
    manifest = repo_root / "images" / name / "image.toml"
    if not manifest.is_file():
        # A deployed service with no images/ directory is a fleet this repo does
        # not build. Say so rather than defaulting the path to the root, which
        # would silently address the wrong app.
        raise SystemExit(
            f"error: deploy/compose.yml brings up {name!r} but images/{name}/image.toml "
            "does not exist, so nothing says what path its app is served under")
    from builder.manifest import load_manifest
    return load_manifest(manifest.parent).base_path
