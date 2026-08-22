"""Load a captured benchmark suite through one schema.

Every `tasks/<benchmark>/<suite>/suite.toml` describes its benchmark in that
benchmark's own terms, which is what made the capture honest and is exactly what
a driver cannot consume. This module reads the normalized core — the sections a
driver needs to spin up a task — and hands everything else through untouched: a
section the core does not claim lands in `Suite.extra`, and a key the core does
not claim inside a section it does lands in that section's own `extra`. Loading
a manifest is not allowed to be the thing that loses what it records.

Failures are loud and name what the check prevents, the way builder/manifest.py
fails. A manifest that half-parses is worse than one that does not load: the
driver runs against a task it has misunderstood and scores it.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[a-z][a-z0-9-]*$")

# The sections the core claims. Everything else in a manifest lands in
# Suite.extra verbatim.
CORE_SECTIONS = frozenset({
    "suite", "upstream", "tasks", "addressing", "instance", "auth",
    "accounts", "reset", "scoring", "requires", "open_decisions",
    "external_pins", "decisions_not_applicable",
})

# How a task record becomes the URL an agent starts at.
ADDRESSING_KINDS = frozenset({
    # The record carries the URL(s) — webarena, vwa.
    "start_url_field",
    # The URL is built from the instance key — webshop's /fixed_<i>.
    "path_template",
    # The record carries a path relative to one service — miniwob's url_path.
    "record_path",
})

# Where the instruction the agent sees comes from. The distinction is not
# cosmetic: for the last two it exists nowhere but in the result record, because
# WebShop redraws its price clause per boot and MiniWoB regenerates its
# utterance per episode.
INSTRUCTION_SOURCES = frozenset({"literal", "server_rendered", "page_rendered"})

RESET_SCOPES = frozenset({"task", "episode", "suite"})
RESET_KINDS = frozenset({
    "none",              # nothing happens; state carries into the next task
    "new_session",       # a new identifier, no server-side action
    "page_episode",      # an in-page sequence re-arms the task
    "http_endpoint",     # POST somewhere and the server restores itself
    "container_recreate",
})

# How far one answer to an open decision reaches.
DECISION_SCOPES = frozenset({"suite", "benchmark", "fleet"})


# The keys each core section models, by its TOML spelling. A manifest key that is
# not here is not rejected — it is handed through in that section's `extra`. The
# set is declared rather than derived from the dataclasses because two of them
# rename a key (`addressing.field` -> `field_name`) and a derived set would then
# quietly report the wrong thing.
CLAIMED_KEYS = {
    "upstream": frozenset({"repo", "commit", "license", "files"}),
    "tasks": frozenset({"file", "format", "instruction_source", "instruction_field"}),
    "addressing": frozenset({"kind", "field", "template", "separator", "base_service",
                             "sites", "substitution"}),
    "instance": frozenset({"key", "from_file", "chosen_by_harness"}),
    "auth": frozenset({"kind", "decided_by", "tasks_with_session", "tasks_anonymous"}),
    "scoring": frozenset({"evaluator_kinds", "reward_range", "success", "offline_scorable",
                          "needs_live_page", "needs_judge", "aggregate", "census"}),
    "requires": frozenset({"services", "not_built", "not_deployed", "required_env",
                           "runnable", "blocked", "blocked_by"}),
    "reset": frozenset({"scope", "kind", "contaminates", "how", "cost_s"}),
    "external_pins": frozenset({"what", "pinned_at", "sha256"}),
    "open_decisions": frozenset({"id", "scope", "kind", "question", "options", "affects",
                                 "affects_tasks"}),
}


def _extra(table: dict, section: str) -> dict:
    return {k: v for k, v in table.items() if k not in CLAIMED_KEYS[section]}


def _die(path: Path, msg: str):
    raise SystemExit(f"error: {path}: {msg}")


@dataclass(frozen=True)
class VendoredFile:
    # Relative to the BENCHMARK directory, always. The two bases in play — this
    # one and tasks.file's suite-relative one — each have exactly one rule, and
    # a manifest that spelled a vendored path "../upstream/x.py" would resolve
    # against whichever root a reader guessed.
    path: str
    sha256: str
    bytes: int
    upstream_path: str = ""


@dataclass(frozen=True)
class Upstream:
    repo: str
    commit: str
    license: str
    files: tuple[VendoredFile, ...] = ()
    # What this section records that the core does not name, verbatim.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Tasks:
    file: str
    format: str
    instruction_source: str
    # Empty unless instruction_source is "literal": the other two have no field
    # to name, because the text does not exist until the episode runs.
    instruction_field: str = ""
    # What this section records that the core does not name, verbatim.
    extra: dict = field(default_factory=dict)

    @property
    def instruction_is_recoverable(self) -> bool:
        """True when the instruction can be read back out of the task file.

        False makes `instruction` mandatory in a result record: nothing else in
        the world will still hold the text the agent was given.
        """
        return self.instruction_source == "literal"


@dataclass(frozen=True)
class Addressing:
    kind: str
    # start_url_field / record_path: the record field holding the URL or path.
    field_name: str = ""
    # path_template: e.g. "/fixed_{index}".
    template: str = ""
    # start_url_field: the in-band separator that splits one field into several
    # tabs. Bare-string split on the exact spaced token, never a strip().
    separator: str = ""
    # Which service a bare path is relative to. Absent exactly where the
    # record's own placeholder token supplies the origin.
    base_service: str = ""
    # Placeholder token -> service name, as images/*/*/ names it.
    sites: dict[str, str] = field(default_factory=dict)
    # The substitution rule, verbatim from upstream, where there is one.
    substitution: dict = field(default_factory=dict)
    # What this section records that the core does not name, verbatim.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Instance:
    key: tuple[str, ...]
    from_file: tuple[str, ...]
    chosen_by_harness: tuple[str, ...]
    # What this section records that the core does not name, verbatim.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Auth:
    kind: str                      # "storage_state" | "none"
    decided_by: str = ""
    tasks_with_session: int = 0
    tasks_anonymous: int = 0
    # What this section records that the core does not name, verbatim.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Account:
    name: str
    username: str
    password: str


@dataclass(frozen=True)
class ExternalPin:
    """Data this suite needs whose pin has one source of truth somewhere else.

    WebShop's goal corpus is pinned in images/webshop/server/image.toml and
    VisualWebArena's input images in tasks/vwa/datasets.toml. Restating either
    sha256 here would be the silent-wrong-data hole docs/design.md describes,
    so the manifest points at the pin instead of copying it.
    """
    what: str
    pinned_at: str          # repo-root-relative path to the file holding the pin
    sha256: str = ""        # the pinned hash, where the pin is of a single blob
    # What this section records that the core does not name, verbatim.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Reset:
    scope: str
    kind: str
    # True where what a task changes is still there for the next one. `kind` is
    # not enough: WebArena and WebShop both reset nothing per task, but
    # WebArena's tasks place orders and open issues against a server that keeps
    # them, and WebShop's server writes nothing at all. One means task order is
    # part of the run definition; the other means it is free.
    contaminates: bool = False
    how: str = ""
    # None where it has not been measured. An unmeasured number is left out
    # rather than guessed, the way image.toml leaves out build_minutes.
    cost_s: float | None = None
    # What this section records that the core does not name, verbatim.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BlockedBy:
    service: str
    tasks: int
    task_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Requires:
    services: tuple[str, ...]
    not_built: tuple[str, ...] = ()
    not_deployed: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    runnable: int = 0
    blocked: int = 0
    blocked_by: tuple[BlockedBy, ...] = ()
    # What this section records that the core does not name, verbatim.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Scoring:
    evaluator_kinds: tuple[str, ...]
    reward_range: tuple[float, float]
    # A predicate over `score`, as an expression. Recorded rather than assumed:
    # WebShop's continuous reward makes success a threshold, not a truthiness.
    success: str
    offline_scorable: int
    needs_live_page: int
    needs_judge: int
    aggregate: str
    # The counts measured over the task file. Kept, and kept out of the way:
    # webarena's census is 34 keys and a driver reads none of them.
    census: dict = field(default_factory=dict)
    # What this section records that the core does not name, verbatim.
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionOption:
    # A slug, so a result record can say WHICH option a run took. Free-text
    # options can be read by a person and referenced by nothing.
    id: str
    text: str


@dataclass(frozen=True)
class OpenDecision:
    id: str
    scope: str
    kind: str
    question: str
    options: tuple[DecisionOption, ...]
    affects: str = ""
    # How many of the suite's tasks the answer moves. None where it genuinely
    # cannot be counted — no field marks which tasks depend on the relative
    # order of equally-ranked search results, and guessing would be worse than
    # leaving it out.
    affects_tasks: int | None = None
    # What this section records that the core does not name, verbatim.
    extra: dict = field(default_factory=dict)

    @property
    def option_ids(self) -> tuple[str, ...]:
        return tuple(o.id for o in self.options)


@dataclass(frozen=True)
class Suite:
    name: str
    benchmark: str
    description: str
    task_count: int
    path: Path                     # the suite.toml itself
    upstream: Upstream
    tasks: Tasks
    addressing: Addressing
    instance: Instance
    auth: Auth
    scoring: Scoring
    requires: Requires
    accounts: tuple[Account, ...] = ()
    resets: tuple[Reset, ...] = ()
    open_decisions: tuple[OpenDecision, ...] = ()
    external_pins: tuple[ExternalPin, ...] = ()
    # Decision id -> why it does not apply here. A fleet-scoped question that a
    # suite simply omits is indistinguishable from one nobody considered;
    # naming the reason is what makes the omission auditable.
    decisions_not_applicable: dict = field(default_factory=dict)
    # Every section the core does not claim, verbatim. A manifest may record
    # anything it needs; loading it must not be the thing that loses it.
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.benchmark}/{self.name}"

    @property
    def dir(self) -> Path:
        return self.path.parent

    def uid(self, instance: dict) -> str:
        """The stable name of one runnable instance.

        `<benchmark>/<suite>#<v>` for a single-field key, and
        `<benchmark>/<suite>#<k>=<v>,<k>=<v>` for a compound one. The rule lives
        here rather than in six manifests: a naming scheme has one source of
        truth for the same reason a pin does.
        """
        missing = [k for k in self.instance.key if k not in instance]
        if missing:
            raise KeyError(
                f"{self.id}: instance is missing {missing}; this suite is keyed "
                f"by {list(self.instance.key)}"
            )
        if len(self.instance.key) == 1:
            return f"{self.id}#{instance[self.instance.key[0]]}"
        body = ",".join(f"{k}={instance[k]}" for k in self.instance.key)
        return f"{self.id}#{body}"

    def resolve(self, rel: str) -> Path:
        """A vendored path is relative to the suite dir, or to the benchmark dir.

        Scoring code and licences are shared by every suite of a benchmark and
        live one level up, so both roots are legitimate and a manifest does not
        have to say which it meant.
        """
        for root in (self.dir, self.dir.parent):
            candidate = root / rel
            if candidate.is_file():
                return candidate
        return self.dir / rel

    def decisions_to_answer(self) -> tuple[str, ...]:
        """The open-decision ids a run of this suite has to answer.

        Every one of them, because each was recorded precisely because leaving
        it implicit would bury a policy choice — several change the metric
        rather than the implementation.
        """
        return tuple(d.id for d in self.open_decisions)


def _require(path: Path, data: dict, section: str) -> dict:
    if section not in data:
        _die(path, f"[{section}] is missing")
    if not isinstance(data[section], dict):
        _die(path, f"[{section}] must be a table")
    return data[section]


def _str(path: Path, table: dict, where: str, key: str, *, required: bool = True) -> str:
    value = table.get(key, "")
    if not isinstance(value, str):
        _die(path, f"{where}.{key} must be a string, got {value!r}")
    if required and not value:
        _die(path, f"{where}.{key} is empty")
    return value


def _int(path: Path, table: dict, where: str, key: str) -> int:
    if key not in table:
        _die(path, f"{where}.{key} is missing")
    value = table[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _die(path, f"{where}.{key} must be a non-negative integer, got {value!r}")
    return value


def _strs(path: Path, table: dict, where: str, key: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        _die(path, f"{where}.{key} must be a list of strings, got {value!r}")
    return tuple(value)


def _load_upstream(path: Path, data: dict) -> Upstream:
    up = _require(path, data, "upstream")
    commit = _str(path, up, "upstream", "commit")
    if not _COMMIT.fullmatch(commit):
        _die(path, f"upstream.commit {commit!r} is not a 40-char sha — a branch or a "
                   "tag moves, and a moving ref cannot say which bytes were vendored")
    files = []
    entries = up.get("files", [])
    if not isinstance(entries, list) or not entries:
        _die(path, "[[upstream.files]] records nothing — a capture with no vendored "
                   "blob has no provenance to check")
    for i, entry in enumerate(entries):
        where = f"upstream.files[{i}]"
        sha = _str(path, entry, where, "sha256")
        if not _SHA256.fullmatch(sha):
            _die(path, f"{where}.sha256 is not 64 lowercase hex chars")
        rel = _str(path, entry, where, "path")
        if rel.startswith(("/", "../")):
            _die(path, f"{where}.path {rel!r} must be relative to the benchmark "
                       "directory; a '../' spelling resolves against whichever root "
                       "a reader guesses")
        files.append(VendoredFile(
            path=rel,
            sha256=sha,
            bytes=_int(path, entry, where, "bytes"),
            upstream_path=_str(path, entry, where, "upstream_path", required=False),
        ))
    return Upstream(
        repo=_str(path, up, "upstream", "repo"),
        commit=commit,
        license=_str(path, up, "upstream", "license"),
        files=tuple(files),
        extra=_extra(up, "upstream"),
    )


def _load_addressing(path: Path, data: dict) -> Addressing:
    addr = _require(path, data, "addressing")
    kind = _str(path, addr, "addressing", "kind")
    if kind not in ADDRESSING_KINDS:
        _die(path, f"addressing.kind must be one of {sorted(ADDRESSING_KINDS)}, got {kind!r}")
    field_name = _str(path, addr, "addressing", "field", required=False)
    template = _str(path, addr, "addressing", "template", required=False)
    if kind in ("start_url_field", "record_path") and not field_name:
        _die(path, f"addressing.field is required when addressing.kind = {kind!r} — "
                   "nothing else says which record field holds the address")
    if kind == "path_template":
        if not template:
            _die(path, "addressing.template is required when addressing.kind = "
                       "'path_template'")
        if "{" not in template:
            _die(path, f"addressing.template {template!r} interpolates nothing, so every "
                       "task would resolve to the same URL")
    sites = addr.get("sites", {})
    if not isinstance(sites, dict) or any(not isinstance(v, str) for v in sites.values()):
        _die(path, "[addressing.sites] must map a token to a service name")
    substitution = addr.get("substitution", {})
    if not isinstance(substitution, dict):
        _die(path, "[addressing.substitution] must be a table")
    if sites and not substitution:
        _die(path, "[addressing.sites] maps tokens but [addressing.substitution] does not "
                   "say how they are replaced — the order is load-bearing where one token "
                   "is a prefix of another")
    return Addressing(
        kind=kind,
        field_name=field_name,
        template=template,
        separator=_str(path, addr, "addressing", "separator", required=False),
        base_service=_str(path, addr, "addressing", "base_service", required=False),
        sites=dict(sites),
        substitution=dict(substitution),
        extra=_extra(addr, "addressing"),
    )


def _load_instance(path: Path, data: dict) -> Instance:
    inst = _require(path, data, "instance")
    key = _strs(path, inst, "instance", "key")
    if not key:
        _die(path, "instance.key is empty — nothing would name one runnable task")
    from_file = _strs(path, inst, "instance", "from_file")
    chosen = _strs(path, inst, "instance", "chosen_by_harness")
    if tuple(from_file) + tuple(chosen) != key:
        _die(path, f"instance.from_file + instance.chosen_by_harness is "
                   f"{list(from_file) + list(chosen)}, which is not instance.key {list(key)}; "
                   "every part of the key comes from exactly one of the two")
    return Instance(key=key, from_file=from_file, chosen_by_harness=chosen,
                    extra=_extra(inst, "instance"))


def _load_auth(path: Path, data: dict) -> Auth:
    auth = _require(path, data, "auth")
    kind = _str(path, auth, "auth", "kind")
    if kind not in ("storage_state", "none"):
        _die(path, f"auth.kind must be 'storage_state' or 'none', got {kind!r}")
    decided_by = _str(path, auth, "auth", "decided_by", required=False)
    if kind == "storage_state" and not decided_by:
        _die(path, "auth.decided_by is required when auth.kind = 'storage_state' — "
                   "webarena decides by storage_state being non-null and NOT by "
                   "require_login, which is true on all 812 records")
    return Auth(
        kind=kind,
        decided_by=decided_by,
        tasks_with_session=auth.get("tasks_with_session", 0),
        tasks_anonymous=auth.get("tasks_anonymous", 0),
        extra=_extra(auth, "auth"),
    )


def _load_resets(path: Path, data: dict) -> tuple[Reset, ...]:
    entries = data.get("reset", [])
    if isinstance(entries, dict):
        _die(path, "[reset] must be [[reset]] — reset has a scope, and a suite can "
                   "declare more than one (webarena resets nothing per task and "
                   "recreates containers per suite)")
    if not entries:
        _die(path, "[[reset]] records nothing; a suite that resets nothing says so "
                   "with scope='task', kind='none'")
    out = []
    seen = set()
    for i, entry in enumerate(entries):
        where = f"reset[{i}]"
        scope = _str(path, entry, where, "scope")
        kind = _str(path, entry, where, "kind")
        if scope not in RESET_SCOPES:
            _die(path, f"{where}.scope must be one of {sorted(RESET_SCOPES)}, got {scope!r}")
        if kind not in RESET_KINDS:
            _die(path, f"{where}.kind must be one of {sorted(RESET_KINDS)}, got {kind!r}")
        if scope in seen:
            _die(path, f"two [[reset]] entries claim scope {scope!r}; one scope has one answer")
        seen.add(scope)
        cost = entry.get("cost_s")
        if cost is not None and not isinstance(cost, (int, float)):
            _die(path, f"{where}.cost_s must be a number or absent, got {cost!r}")
        contaminates = entry.get("contaminates")
        if not isinstance(contaminates, bool):
            _die(path, f"{where}.contaminates must be true or false. It is what "
                       "separates WebArena's 'none' — every mutation leaks into every "
                       "later task, so order is part of the run — from WebShop's, "
                       "where the server writes nothing")
        if kind == "container_recreate" and contaminates:
            _die(path, f"{where} recreates the container and still claims to "
                       "contaminate; a fresh container from the published image is "
                       "the definition of not carrying state forward")
        out.append(Reset(
            scope=scope, kind=kind, contaminates=contaminates,
            how=_str(path, entry, where, "how", required=False),
            cost_s=None if cost is None else float(cost),
            extra=_extra(entry, "reset"),
        ))
    if "task" not in seen and "episode" not in seen:
        _die(path, "[[reset]] declares no per-task or per-episode entry, so nothing says "
                   "what a driver does between two tasks")
    return tuple(out)


def _load_scoring(path: Path, data: dict, task_count: int) -> Scoring:
    sc = _require(path, data, "scoring")
    kinds = _strs(path, sc, "scoring", "evaluator_kinds")
    if not kinds:
        _die(path, "scoring.evaluator_kinds is empty — nothing would score a task")
    rng = sc.get("reward_range")
    if (not isinstance(rng, list) or len(rng) != 2
            or any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in rng)):
        _die(path, f"scoring.reward_range must be [low, high], got {rng!r}")
    if rng[0] >= rng[1]:
        _die(path, f"scoring.reward_range {rng} is not ordered low-to-high")
    offline = _int(path, sc, "scoring", "offline_scorable")
    live = _int(path, sc, "scoring", "needs_live_page")
    judge = _int(path, sc, "scoring", "needs_judge")
    if offline + live != task_count:
        _die(path, f"scoring.offline_scorable {offline} + scoring.needs_live_page {live} "
                   f"is {offline + live}, not the suite's {task_count} tasks; the two "
                   "partition the suite, so a gap means a task nothing plans to score")
    if judge > task_count:
        _die(path, f"scoring.needs_judge {judge} exceeds the suite's {task_count} tasks")
    census = sc.get("census", {})
    if not isinstance(census, dict):
        _die(path, "[scoring.census] must be a table")
    return Scoring(
        evaluator_kinds=kinds,
        reward_range=(float(rng[0]), float(rng[1])),
        success=_str(path, sc, "scoring", "success"),
        offline_scorable=offline,
        needs_live_page=live,
        needs_judge=judge,
        aggregate=_str(path, sc, "scoring", "aggregate"),
        census=dict(census),
        extra=_extra(sc, "scoring"),
    )


def _load_requires(path: Path, data: dict, task_count: int) -> Requires:
    req = _require(path, data, "requires")
    services = _strs(path, req, "requires", "services")
    if not services:
        _die(path, "requires.services is empty — a suite runs against something")
    not_built = _strs(path, req, "requires", "not_built")
    not_deployed = _strs(path, req, "requires", "not_deployed")
    overlap = set(not_built) & set(not_deployed)
    if overlap:
        _die(path, f"{sorted(overlap)} is both not_built and not_deployed; they are "
                   "different problems with different fixes")
    for name in set(not_built) | set(not_deployed):
        if name not in services:
            _die(path, f"{name!r} is declared unavailable but is not among requires.services")
    runnable = _int(path, req, "requires", "runnable")
    blocked = _int(path, req, "requires", "blocked")
    if runnable + blocked != task_count:
        _die(path, f"requires.runnable {runnable} + requires.blocked {blocked} does not "
                   f"account for the suite's {task_count} tasks")
    blocked_by = []
    for i, entry in enumerate(req.get("blocked_by", [])):
        where = f"requires.blocked_by[{i}]"
        service = _str(path, entry, where, "service")
        if service not in set(not_built) | set(not_deployed):
            _die(path, f"{where}.service {service!r} blocks tasks but is neither "
                       "not_built nor not_deployed, so nothing says why it is missing")
        ids = entry.get("task_ids", [])
        if not isinstance(ids, list) or any(not isinstance(v, int) for v in ids):
            _die(path, f"{where}.task_ids must be a list of integers")
        count = _int(path, entry, where, "tasks")
        if ids and len(ids) != count:
            _die(path, f"{where} lists {len(ids)} task_ids but claims {count} tasks")
        blocked_by.append(BlockedBy(service=service, tasks=count, task_ids=tuple(ids)))
    if bool(blocked) != bool(blocked_by):
        _die(path, f"requires.blocked is {blocked} and [[requires.blocked_by]] has "
                   f"{len(blocked_by)} entries; a blocked task has a reason and a "
                   "reason blocks a task")
    if blocked_by:
        counts = [b.tasks for b in blocked_by]
        # A task can be blocked by two missing services at once, so the entries
        # bound `blocked` from both sides rather than summing to it.
        if not max(counts) <= blocked <= sum(counts):
            _die(path, f"requires.blocked is {blocked}, outside the "
                       f"[{max(counts)}, {sum(counts)}] the blocked_by counts allow")
    return Requires(
        services=services, not_built=not_built, not_deployed=not_deployed,
        required_env=_strs(path, req, "requires", "required_env"),
        runnable=runnable, blocked=blocked, blocked_by=tuple(blocked_by),
        extra=_extra(req, "requires"),
    )


def _load_external_pins(path: Path, data: dict) -> tuple[ExternalPin, ...]:
    out = []
    for i, entry in enumerate(data.get("external_pins", [])):
        where = f"external_pins[{i}]"
        sha = _str(path, entry, where, "sha256", required=False)
        if sha and not _SHA256.fullmatch(sha):
            _die(path, f"{where}.sha256 is not 64 lowercase hex chars")
        pinned_at = _str(path, entry, where, "pinned_at")
        if pinned_at.startswith(("/", ".")):
            _die(path, f"{where}.pinned_at {pinned_at!r} must be repo-root-relative, so "
                       "one string names the pin from anywhere")
        out.append(ExternalPin(
            what=_str(path, entry, where, "what"), pinned_at=pinned_at, sha256=sha,
            extra=_extra(entry, "external_pins")))
    return tuple(out)


def _load_decisions(path: Path, data: dict, benchmark: str, task_count: int) -> tuple[OpenDecision, ...]:
    out, seen = [], set()
    for i, entry in enumerate(data.get("open_decisions", [])):
        where = f"open_decisions[{i}]"
        ident = _str(path, entry, where, "id")
        if not _SLUG.fullmatch(ident):
            _die(path, f"{where}.id {ident!r} is not a lowercase slug")
        if not ident.startswith((f"{benchmark}-", "fleet-")):
            _die(path, f"{where}.id {ident!r} is namespaced to neither {benchmark} nor fleet")
        if ident in seen:
            _die(path, f"{where}.id {ident!r} is declared twice in one suite")
        seen.add(ident)
        scope = _str(path, entry, where, "scope")
        if scope not in DECISION_SCOPES:
            _die(path, f"{where}.scope must be one of {sorted(DECISION_SCOPES)}, got {scope!r}")
        if scope == "fleet" and not ident.startswith("fleet-"):
            _die(path, f"{where}.id {ident!r} claims fleet scope but is namespaced to a "
                       "benchmark, so one answer could not be found by its id")
        raw_options = entry.get("options", [])
        if not isinstance(raw_options, list) or any(not isinstance(o, dict) for o in raw_options):
            _die(path, f"{where}.options must be [[open_decisions.options]] tables of "
                       "id and text. A free-text option can be read by a person and "
                       "referenced by nothing, so no result could say which was taken")
        if len(raw_options) < 2:
            _die(path, f"{where}.options offers {len(raw_options)} choice(s); a decision "
                       "with one option is a decision already made")
        options, option_ids = [], set()
        for j, opt in enumerate(raw_options):
            oid = _str(path, opt, f"{where}.options[{j}]", "id")
            if not _SLUG.fullmatch(oid):
                _die(path, f"{where}.options[{j}].id {oid!r} is not a lowercase slug")
            if oid in option_ids:
                _die(path, f"{where}.options[{j}].id {oid!r} is declared twice")
            option_ids.add(oid)
            options.append(DecisionOption(
                id=oid, text=_str(path, opt, f"{where}.options[{j}]", "text")))
        affects_tasks = entry.get("affects_tasks")
        if affects_tasks is not None and (
                not isinstance(affects_tasks, int) or isinstance(affects_tasks, bool)
                or affects_tasks < 0):
            _die(path, f"{where}.affects_tasks must be a non-negative integer or absent, "
                       f"got {affects_tasks!r}")
        if affects_tasks is not None and affects_tasks > task_count:
            _die(path, f"{where}.affects_tasks {affects_tasks} exceeds the suite's "
                       f"{task_count} tasks")
        out.append(OpenDecision(
            id=ident, scope=scope,
            kind=_str(path, entry, where, "kind"),
            question=_str(path, entry, where, "question"),
            options=tuple(options),
            affects=_str(path, entry, where, "affects", required=False),
            affects_tasks=affects_tasks,
            extra=_extra(entry, "open_decisions"),
        ))
    return tuple(out)


def load_suite(path: Path) -> Suite:
    """Read one suite.toml. Raises SystemExit naming the failure it prevents."""
    path = Path(path)
    if not path.is_file():
        _die(path, "suite.toml not found")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        _die(path, f"is not valid TOML: {exc}")

    meta = _require(path, data, "suite")
    name = _str(path, meta, "suite", "name")
    benchmark = _str(path, meta, "suite", "benchmark")
    if name != path.parent.name or benchmark != path.parent.parent.name:
        _die(path, f"[suite] calls this {benchmark}/{name} but it is discovered as "
                   f"{path.parent.parent.name}/{path.parent.name}; discovery is by glob, "
                   "so the directory is the name")
    task_count = _int(path, meta, "suite", "task_count")
    if task_count == 0:
        _die(path, "suite.task_count is 0 — an empty suite passes every test it has")

    tasks_tbl = _require(path, data, "tasks")
    source = _str(path, tasks_tbl, "tasks", "instruction_source")
    if source not in INSTRUCTION_SOURCES:
        _die(path, f"tasks.instruction_source must be one of "
                   f"{sorted(INSTRUCTION_SOURCES)}, got {source!r}")
    instruction_field = _str(path, tasks_tbl, "tasks", "instruction_field", required=False)
    if source == "literal" and not instruction_field:
        _die(path, "tasks.instruction_field is required when instruction_source is "
                   "'literal' — otherwise nothing says which field the agent is given")
    if source != "literal" and instruction_field:
        _die(path, f"tasks.instruction_field is set but instruction_source is {source!r}; "
                   "the text is not in the file, so naming a field claims otherwise")
    tasks = Tasks(
        file=_str(path, tasks_tbl, "tasks", "file"),
        format=_str(path, tasks_tbl, "tasks", "format"),
        instruction_source=source,
        instruction_field=instruction_field,
        extra=_extra(tasks_tbl, "tasks"),
    )

    accounts = []
    for acct_name, entry in (data.get("accounts") or {}).items():
        where = f"accounts.{acct_name}"
        if not isinstance(entry, dict):
            _die(path, f"[{where}] must be a table of username and password")
        accounts.append(Account(
            name=acct_name,
            username=_str(path, entry, where, "username"),
            password=_str(path, entry, where, "password"),
        ))

    decisions = _load_decisions(path, data, benchmark, task_count)
    not_applicable = data.get("decisions_not_applicable", {})
    if not isinstance(not_applicable, dict) or any(
            not isinstance(v, str) or not v for v in not_applicable.values()):
        _die(path, "[decisions_not_applicable] must map a decision id to the reason it "
                   "does not apply; an empty reason says nothing")
    for ident in not_applicable:
        if ident in {d.id for d in decisions}:
            _die(path, f"{ident!r} is both an open decision here and declared not "
                       "applicable")

    return Suite(
        name=name,
        benchmark=benchmark,
        description=_str(path, meta, "suite", "description"),
        task_count=task_count,
        path=path,
        upstream=_load_upstream(path, data),
        tasks=tasks,
        addressing=_load_addressing(path, data),
        instance=_load_instance(path, data),
        auth=_load_auth(path, data),
        scoring=_load_scoring(path, data, task_count),
        requires=_load_requires(path, data, task_count),
        accounts=tuple(sorted(accounts, key=lambda a: a.name)),
        resets=_load_resets(path, data),
        open_decisions=decisions,
        external_pins=_load_external_pins(path, data),
        decisions_not_applicable=dict(not_applicable),
        extra={k: v for k, v in data.items() if k not in CORE_SECTIONS},
    )


def discover_suites(repo_root: Path) -> list[Suite]:
    """Every captured suite, found the way images are: by glob, with no index.

    Adding a suite is creating a directory; removing one is deleting it.
    """
    return [load_suite(p) for p in sorted(Path(repo_root).glob("tasks/*/*/suite.toml"))]
