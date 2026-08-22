"""The task-suite schema and the driver's view of it.

Deliberately NOT part of `builder/`. `builder/discover.py` takes its "shared
build inputs changed: building all" branch on any change under `builder/`, and
`.github/workflows/build.yml` filters pushes on `images/**`, `builder/**` and
`bin/**` — so a schema edit made there would rebuild the whole fleet. The same
reason keeps the CLI at `python3 -m harness` instead of `bin/tasks`.

`suite` and `results` are stdlib-only, like `builder/manifest.py`. `fleet` needs
PyYAML because deploy/compose.yml is YAML, and it says so at the import.
"""
