"""Turn a suite, an instance key and a running fleet into a runnable task.

This is the acceptance test for the whole schema. If a fact needed to start a
task is missing from `suite.toml`, `image.toml` or `deploy/compose.yml`, this
module cannot produce it and the gap is visible rather than assumed.

It stops at the browser. Nothing here drives an agent, executes an evaluator or
records a result — that is the next pass, and it is the first thing in this repo
that will need a running fleet.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from harness.fleet import Service
from harness.suite import Reset, Suite


@dataclass(frozen=True)
class TaskCase:
    uid: str
    suite: str
    instance: dict
    # None where the suite renders it at run time. That is not a gap: WebShop
    # composes the goal text at server boot and MiniWoB regenerates it per
    # episode, so the text does not exist until the task does.
    instruction: str | None
    instruction_source: str
    start_urls: tuple[str, ...]
    storage_state: str
    requires: tuple[str, ...]
    # The services THIS task cannot do without and the fleet cannot serve, with
    # the reason. A task with a non-empty map here is `blocked` in the results
    # schema, not an error. Empty on a task that never addresses a missing
    # service, even where the suite as a whole is missing several.
    unavailable: dict = field(default_factory=dict)
    resets: tuple[Reset, ...] = ()
    decisions_to_answer: tuple[str, ...] = ()
    # The upstream record, unmodified. The evaluator spec is executable code
    # resolved by name against upstream's helper module, so it is carried
    # verbatim rather than parsed into something tidier.
    record: dict = field(default_factory=dict)

    @property
    def runnable(self) -> bool:
        return not self.unavailable


class Unresolvable(RuntimeError):
    pass


def _substituted_text(suite: Suite, urls: dict) -> str:
    """The task file's raw text with placeholder tokens replaced.

    A whole-file text replace on the bytes BEFORE json.loads, because that is
    what upstream does and tokens occur inside expected page content as well as
    in URL fields. The order is upstream's, verbatim: __SHOPPING__ is replaced
    before __SHOPPING_ADMIN__, and that is only safe because of the trailing
    "__" — a prefix-matching regex corrupts every __SHOPPING_ADMIN__ occurrence
    into "<shopping-url>_ADMIN__" and leaves valid-looking JSON behind.
    """
    text = suite.resolve(suite.tasks.file).read_text()
    order = suite.addressing.substitution.get("order") or list(suite.addressing.sites)
    for token in order:
        if token in suite.addressing.sites and token in urls:
            text = text.replace(token, urls[token])
    return text


def _records(suite: Suite, text: str) -> list:
    fmt = suite.tasks.format
    if fmt == "json_array":
        return json.loads(text)
    if fmt == "json_array_of_strings":
        return [{"index": i, "text": s} for i, s in enumerate(json.loads(text))]
    if fmt == "jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    raise Unresolvable(f"{suite.id}: unknown tasks.format {fmt!r}")


def _find(suite: Suite, records: list, instance: dict) -> dict:
    wanted = {k: instance[k] for k in suite.instance.from_file}
    for record in records:
        if all(record.get(k) == v for k, v in wanted.items()):
            return record
    raise Unresolvable(
        f"{suite.id}: no record matches {wanted}; this suite is keyed from the file "
        f"by {list(suite.instance.from_file)}")


def _start_urls(suite: Suite, record: dict, instance: dict, fleet: dict, host: str,
                urls: dict) -> tuple[str, ...]:
    kind = suite.addressing.kind
    if kind == "start_url_field":
        raw = record.get(suite.addressing.field_name) or ""
        sep = suite.addressing.separator
        parts = raw.split(sep) if sep and sep in raw else [raw]
        return tuple(p for p in parts if p)
    base = _base(suite, fleet, host)
    if kind == "path_template":
        return (base + suite.addressing.template.format(**instance),)
    if kind == "record_path":
        return (base + str(record.get(suite.addressing.field_name, "")),)
    raise Unresolvable(f"{suite.id}: unknown addressing.kind {kind!r}")


def _base(suite: Suite, fleet: dict, host: str) -> str:
    name = suite.addressing.base_service
    if not name:
        raise Unresolvable(
            f"{suite.id}: addressing.kind = {suite.addressing.kind!r} builds a path, and "
            "addressing.base_service does not say what it is a path under")
    service = fleet.get(name)
    if service is None:
        raise Unresolvable(f"{suite.id}: the fleet does not serve {name!r}")
    return service.base_url(host)


def missing_services(suite: Suite, fleet: dict[str, Service]) -> dict:
    """Every service the suite needs that this fleet cannot serve, and why.

    Suite-level: it says nothing about which tasks care. A WebArena run against
    the deployed fleet is missing four map services and still runs 684 of its
    812 tasks.
    """
    out = {}
    for service in suite.requires.services:
        if service in suite.requires.not_built:
            out[service] = "not built in this repo"
        elif service in suite.requires.not_deployed:
            out[service] = "built and published, absent from deploy/compose.yml"
        elif service not in fleet:
            out[service] = "not in the running fleet"
    return out


def blocking(suite: Suite, record: dict, missing: dict) -> dict:
    """Of the suite's missing services, the ones THIS task cannot do without.

    Three rules, in the order the manifest answers them:

    1. A [[requires.blocked_by]] entry that enumerates task_ids has already
       decided, task by task, and nothing else may overrule it.
    2. Otherwise a task reaches a service through that service's placeholder
       token. An unserved token is never substituted, so it is still in the
       record and its presence is the question answered.
    3. A suite that maps no tokens addresses one deployment for every task, so a
       missing service blocks all of them.

    What is deliberately NOT blocking: a missing service with no token in a suite
    that has tokens. WebArena's three map back ends are reached only through the
    map front end, and counting them per task would block all 812.
    """
    if not missing:
        return {}
    text = json.dumps(record)
    by_service = {b.service: b for b in suite.requires.blocked_by}
    out = {}
    for service, reason in missing.items():
        entry = by_service.get(service)
        if entry is not None and entry.task_ids:
            ident = record.get(suite.instance.from_file[0]) if suite.instance.from_file else None
            if ident in entry.task_ids:
                out[service] = reason
            continue
        tokens = [t for t, name in suite.addressing.sites.items() if name == service]
        if tokens:
            if any(token in text for token in tokens):
                out[service] = reason
        elif not suite.addressing.sites:
            out[service] = reason
    return out


def resolve(suite: Suite, instance: dict, fleet: dict[str, Service], host: str) -> TaskCase:
    """One runnable task case, or a TaskCase that says why it is not runnable."""
    missing = missing_services(suite, fleet)

    urls = {
        token: fleet[name].base_url(host)
        for token, name in suite.addressing.sites.items()
        if name in fleet
    }
    record = _find(suite, _records(suite, _substituted_text(suite, urls)), instance)

    instruction = None
    if suite.tasks.instruction_source == "literal":
        instruction = record.get(suite.tasks.instruction_field)

    return TaskCase(
        uid=suite.uid(instance),
        suite=suite.id,
        instance=dict(instance),
        instruction=instruction,
        instruction_source=suite.tasks.instruction_source,
        start_urls=_start_urls(suite, record, instance, fleet, host, urls),
        storage_state=str(record.get(suite.auth.decided_by) or "")
                      if suite.auth.kind == "storage_state" else "",
        requires=suite.requires.services,
        unavailable=blocking(suite, record, missing),
        resets=suite.resets,
        decisions_to_answer=suite.decisions_to_answer(),
        record=record,
    )
