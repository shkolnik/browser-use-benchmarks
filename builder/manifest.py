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
class Media:
    """An inert subtree that the build threads through without transforming it.

    Declaring this section is how an image opts into the media path: the subtree
    is bucketed into tars on the CI host and ADDed straight into the final
    image, instead of being extracted into the restore stage and COPYed out of
    it entry by entry. No section = today's behaviour, unchanged.

    It is deliberately a SUBTREE and not the whole staged tree. shopping stages
    all of /opt/magento — composer's vendor tree, generated/, the config.php
    written during restore — and only pub/media is inert. The rest keeps the
    existing partition-and-COPY machinery.
    """
    archive: str         # prepare output holding the tree, e.g. shopping_media.tar
    strip: str           # subtree within that archive to treat as media, '' = all
    dest: str            # absolute path in the image, e.g. /opt/magento/pub/media
    # ADD --chown target, e.g. app:app. '' emits no --chown at all, which makes
    # ADD extract the archive's own uid/gid and mode verbatim. That is the right
    # answer for a tree owned by more than one account — a Postgres cluster
    # beside a renderer's cache — where a single --chown would flatten a split
    # the image depends on. It is the wrong answer for a tree whose archive
    # carries whatever uid the machine it was captured on happened to use.
    chown: str
    limit_kb: int        # per-bucket ceiling; buckets become layers
    max_buckets: int     # ceiling, not a target; empty tars are not emitted
    # A floor on the entries the archive must carry, asserted on the host before
    # any of them become a layer. Nothing downstream can check this: the tree
    # never enters a build stage, so an image that ships an empty or truncated
    # media archive builds clean, smokes clean (the gates grep HTML, and a page
    # renders without its photos), and is only wrong once someone looks at it.
    # 0 = no floor.
    min_entries: int = 0
    # Most restore stages never read the media — shopping's reindex is DB/ES
    # only and its in-build assertions grep HTML. The ones that do pay a
    # bind-mount and a --target split; the default is not to.
    restore_needs_media: bool = False


@dataclass(frozen=True)
class Manifest:
    datasets: list[Dataset] = field(default_factory=list)
    healthcheck: str | None = None
    # How long the PUBLISHED address gets to answer once `up --wait` has already
    # proven the container healthy from the inside. Readiness is the image's own
    # HEALTHCHECK now, so this is small on purpose: it exists to catch a
    # container that is healthy in-container and unreachable through the port
    # mapping, which is what happened to shopping for 901s (#66).
    reachability_timeout_s: int = 30
    build_args: dict[str, str] = field(default_factory=dict)
    source: Source = Source(kind="build")
    prepare: Prepare | None = None
    media: Media | None = None

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
    media = None
    med = data.get("media")
    if med is not None:
        for key in ("archive", "dest", "chown", "limit_kb", "max_buckets"):
            if key not in med:
                _die(image_dir, f"media missing '{key}'")
        # Either origin will do. A derived archive is the shopping case: a prepare
        # script builds it, so its identity is the script's. A pinned dataset is
        # the map case: the archive IS the upstream object, there is nothing to
        # derive, and `download` has already verified its sha256 end to end —
        # a stronger guarantee than a prepare output carries, not a weaker one.
        # run_media_prep resolves both through the same output_paths() call.
        known = [d.filename for d in datasets] + (prepare.outputs if prepare else [])
        if med["archive"] not in known:
            _die(image_dir, f"media.archive '{med['archive']}' is neither a pinned "
                            f"dataset nor a prepare output ({', '.join(known)})")
        if not str(med["dest"]).startswith("/"):
            _die(image_dir, "media.dest must be an absolute path in the image")
        # ADD --chown does not fail the build on an unresolvable name: it lands
        # everything 0:0 root:root and exits clean. There is no way to catch that
        # from here (the image's passwd database is not known until it is built),
        # which is why the final stage carries a directory-ownership assert.
        # Required as a KEY even when empty: whether the media path imposes one
        # owner or preserves the archive's is a decision about the shipped tree,
        # and an omitted field would let it be made by not thinking about it.
        if str(med["chown"]) and ":" not in str(med["chown"]):
            _die(image_dir, "media.chown must be 'user:group', or '' to keep "
                            "the archive's own uid/gid")
        media = Media(
            archive=med["archive"],
            strip=str(med.get("strip", "")).strip("/"),
            dest=str(med["dest"]).rstrip("/"),
            chown=str(med["chown"]),
            limit_kb=int(med["limit_kb"]),
            max_buckets=int(med["max_buckets"]),
            min_entries=int(med.get("min_entries", 0)),
            restore_needs_media=bool(med.get("restore_needs_media", False)),
        )
    svc = data.get("service", {})
    if "healthcheck_timeout_s" in svc:
        _die(image_dir,
             "healthcheck_timeout_s is retired — readiness is now the image's own "
             "Dockerfile HEALTHCHECK (--start-period/--timeout/--retries), and the "
             "driver only checks that the PUBLISHED address answers. Move the budget "
             "into the HEALTHCHECK and delete this key; set reachability_timeout_s "
             "only if the port mapping itself is slow to come up.")
    return Manifest(
        datasets=datasets,
        healthcheck=svc.get("healthcheck"),
        reachability_timeout_s=int(svc.get("reachability_timeout_s", 30)),
        build_args={k: str(v) for k, v in data.get("build", {}).get("args", {}).items()},
        source=Source(kind=kind, dataset=src.get("dataset"), tag=src.get("tag")),
        prepare=prepare,
        media=media,
    )
