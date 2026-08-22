# Security posture

**These images carry known-vulnerable dependencies by design. Run them only on
trusted networks. Do not expose them to the public internet.**

## What that means

Every image in this repo pins old versions of operating-system packages,
language runtimes, libraries and applications — in several cases years past
their upstream end of life. Those pins are deliberate and load-bearing. They
are not an oversight, a stalled upgrade queue, or a gap someone means to get
to.

A benchmark harness is not a web application. Its job is to render the same
pages, in the same way, for as long as the published results it is compared
against remain meaningful. An agent's score is a function of the exact bytes a
page serves. Upgrading a dependency that changes those bytes does not just risk
a regression; it silently invalidates comparison with every result anyone has
already published against the upstream environments.

The coupling is tighter than it looks. Some measured examples:

- **Magento (shopping, shopping-admin)** renders prices, dates and number
  formats through ICU. A newer base image ships a newer ICU, and locale data
  changes between ICU releases — so the rendered text of a product page can
  change without a line of application code moving.
- **GitLab** reorganised its primary navigation across the 16.x series. Task
  descriptions that name a menu path stop being followable, independent of
  whether the underlying feature still exists.
- **Kiwix (wikipedia)** changed its URL scheme in 3.4: article paths that were
  served directly now redirect. Any evaluator matching on URL sees a different
  answer.

So the images stay where they are. Dependabot pull requests against these
pins are declined on that basis, not on the merits of the individual advisory.

## What we do instead

- **Provenance over patch level.** Every byte in an image traces to a pinned
  digest, a committed lockfile, or a sha256-pinned dataset. Dockerfiles never
  touch the network. You can audit exactly what is in an image and where it
  came from — see [`docs/design.md`](docs/design.md).
- **No untrusted input by design.** The images are self-contained appliances:
  all data is baked in, nothing is fetched at runtime, and the only input is
  the agent under evaluation.
- **Honest labelling.** This document, rather than a version number that
  implies a maintenance cadence that does not exist.

## Vendored upstream code and task data

`tasks/` holds each benchmark's task set together with the upstream code that
scores it, copied byte-for-byte at a pinned commit and left unmodified. That is
third-party Python in this repository, which nothing else here is, so it is
worth being plain about what it is and is not.

It is vendored to be **read and reimplemented against**, not imported blind. Two
properties of the upstream evaluators make that distinction load-bearing:
WebArena and VisualWebArena task files carry `func:`-prefixed strings that
upstream resolves with a bare `eval()` against its evaluator module's globals, so
whatever runs them executes expressions that arrive with the task data; and
scoring drives a live browser against a running benchmark site after the episode
ends. A harness that adopts this code adopts both. Run it where you run the
images — on a trusted network, against these appliances, and nowhere else.

What the vendoring does give you is provenance. Every file records the commit it
came from and its sha256, `tests/test_task_suites.py` holds the bytes against
those records on every push, and each benchmark's directory carries the upstream
licence it is redistributed under.

## How to run them safely

- Keep them on a trusted LAN, a lab VLAN, or a host-local network.
- Do not port-forward them, and do not place them behind a public reverse proxy.
- Treat any credentials they ship as public knowledge, because they are. The
  benchmark application logins are published in the upstream benchmark
  repositories, and each suite's `tasks/*/*/suite.toml` records the set its tasks
  use — scoring cannot be described without them, since some tasks are scored by
  minting an admin API token.
- Do not put real data in them.
- Prefer ephemeral hosts. The containers hold no state you need to keep; a
  rebuild from the published image is always the recovery path.

## Reporting

Vulnerabilities in **this repository's own code** — the builder, the CI
workflows, the entrypoints and service supervisors under `images/` — are in
scope and worth reporting via a GitHub issue.

Advisories against the pinned upstream dependencies are **out of scope** and
will be closed with a pointer to this document. They are known, they are
expected, and they are the accepted cost of a stable benchmark. The same applies
to the upstream evaluator code vendored under `tasks/`: it is pinned to a commit
and kept unmodified on purpose, so a report that it should be patched is a report
that it should stop matching upstream.
