# VWA classifieds — Osclass 8.1.2 appliance

Status **2026-08-06: provenance fully resolved and verified by reconstruction; the image
is not built yet.** Everything below marked *measured* was read off the running upstream
stack or a live fetch, not from upstream docs.

## What the upstream image actually is

`jykoh/classifieds@sha256:a2a794da92f62a8d7ffd02314e4fab40ba6c7fc08f568371608f88f0ef605e43`
is **76.86 GB**: a 105 MB Osclass 8.1.2 app tree plus **73 GB of item photos** in
`oc-content/uploads`, all inside one 77.8 GB `COPY` layer. Its docker history is complete
buildkit down to `php:8.1.27-cli`, with no hand `docker commit`s — contrast
`../../webarena/reddit`, whose top four layers are commits.

But a readable history is not the same as a reproducible source, and that distinction is
where the earlier version of this file went wrong.

## ⚠️ Correction: the v8.1.2 "tag" this file used to name does not exist

The previous text said *"Osclass upstream: github.com/osclass/Osclass, tag v8.1.2 — that
tag IS the parity pin."* **There is no such tag.** Measured:

- `github.com/osclass/Osclass` stops at **v3.7.4** (last push 2022).
- `mindstellar/Osclass` (where `navjottomer/Osclass` redirects) stops at **v3.9.0**.
- `MercanoGlobal/Osclass-Enterprise` is a *rebrand fork off 3.8.1* — its CHANGELOG opens
  with "Osclass Enterprise 3.10.0, Rebranded the project" and contains **none** of the
  8.1.2 entries.
- **osclass.org is gone** — the domain now serves a Thai gambling site.

The 8.x line is a different project lineage (3.x → 4.x in 2020 → 8.x in 2021-2023),
published by **Osclass Point**. The app itself names its own home: URLs baked into
`oc-includes/osclass/` point at `osclasspoint.com` and `osclass-classifieds.com/download`,
and that download page points at **SourceForge `osclass-by-osclasspoint`**, which carries
`osclass-v8.1.2.zip` — the same name as the upstream image's `COPY osclass-v8.1.2` layer.

That zip is the parity pin. It is Apache-2.0, and it is the only surviving public origin
found for this code.

## The app tree reconstructs byte-for-byte — verified, not asserted

Every file in the deployed tree was sha256'd against the extracted zip:

| | count |
|---|---|
| files compared (excluding `oc-content/uploads`) | 1,918 |
| **byte-identical to the public zip** | **1,912** |
| differing | 6 |
| present only in the image | 7 |

The 7 image-only files are: `config.php`, `classifieds_restore.sql`, three `oc-content/*.log`
runtime logs (73 MB of debris — **not shipped** by the rebuild), `robots.txt`, and a new
controller `oc-includes/osclass/controller/reset.php`.

`webarena-modifications.patch` here captures every difference. Applied to the pristine zip
it yields a tree that is **byte-identical to the deployed one across all 1,919 files, with
zero mismatches and nothing missing** — that reconstruction is how the patch was validated,
and re-running it is how the build's audit stage will keep it honest.

What the patch actually does, semantically:

1. **`index.php`** — silences PHP error display, and routes `page=reset` to the new
   controller.
2. **`oc-includes/osclass/controller/reset.php`** (new) — the `RESET_TOKEN` endpoint. On
   POST with a matching `token`, it shells out to `mysql … < /usr/src/myapp/classifieds_restore.sql`.
   ⚠️ It also `echo`s that command, **password included**, to any caller holding the token.
   Reproduced as-is for parity; flagged, not fixed, and noted for the security pass (#52).
3. **`oc-includes/osclass/utils.php`** — comments out the four `@unlink` calls that delete
   a listing's image files. This is load-bearing for the benchmark: it is what lets the
   reset script restore deleted listings without their photos having been destroyed.
4. **`oc-content/themes/sigma/main.php`** — renders subcategories on the home page.
5. **`themes/sigma/search.php`** and **`oc-includes/osclass/gui/search.php`** — add
   "only search terms of 4 or more characters are valid" to the empty-results message.
6. **`oc-includes/osclass/controller/item.php`** — a trailing newline and nothing else.

The trailing-newline hunks in several files are not edits anyone made on purpose; the tree
was copied via macOS (it still carries `.DS_Store` and `._*` AppleDouble files, which the
rebuild also drops). They are kept in the patch only so the reconstruction is exactly
byte-identical rather than nearly so.

## ⚠️ Upstream's own compose project cannot start — measured

Brought up verbatim from the archive.org zip, `classifieds_db` **fails**:

```
[Entrypoint]: running /docker-entrypoint-initdb.d/classifieds_restore.sql
ERROR 1146 (42S02) at line 2: Table 'osclass.oc_t_item_location' doesn't exist
```

All three files sit in `/docker-entrypoint-initdb.d`, and MySQL runs them in alphabetical
order — so `classifieds_restore.sql` (which is a *reset* script, `DELETE FROM … WHERE
fk_i_item_id >= 84143`) runs against an empty database before `init_db.sh` has created
anything, and the entrypoint exits 1. The appliance built here sidesteps this by design:
the restore is an explicit build-time step, and `classifieds_restore.sql` ships as a file
for the reset endpoint to execute, never as an init script.

(The zip's copy and the image's copy of `classifieds_restore.sql` are both 19,001 bytes and
differ only in the position of one blank line. The rebuild ships the zip's copy — same
content, hash-pinned provenance.)

## Measured validation pins

From the booted upstream stack (`mysql:8.1` + the dump). The earlier guess of "~39 tables,
60 seeded items, one user" was close on two counts and wrong on the third:

| pin | value |
|---|---|
| tables in `osclass` | 39 |
| `oc_t_item` | 84,149 |
| `oc_t_item_description` | 84,152 |
| `oc_t_item_resource` | 84,149 |
| `oc_t_user` | 1 |
| `oc_t_category` | 23 |
| `MAX(pk_i_id)` in `oc_t_item` | 84,154 |
| items with `pk_i_id >= 84143` (what reset deletes) | 12 |
| `oc-content/uploads` | 73 GB, **84,148 per-item directories**, 336,634 files |
| largest single item directory | 6.3 MB |
| app tree excluding uploads | 105 MB |
| PHP / Osclass | 8.1.27 / 8.1.2 |

The uploads are per-item directories, not a flat dump, so the staging-bucket partitioner
splits them at item granularity and no single piece comes close to a layer limit.

## Deviations from upstream, and why

1. **⚠️ MySQL 8.4 LTS instead of 8.1.** Parity policy says match upstream, and upstream is
   `mysql:8.1` — but **8.1 is unobtainable**: it was an innovation release, and Oracle's apt
   repo for bookworm today publishes only `mysql-8.0`, `mysql-8.4-lts`, `mysql-9.7-lts` and
   `mysql-innovation` (measured against the live `Release` file). 8.0 is *older* than
   upstream and already past EOL; 8.4 LTS is the maintained successor to the line 8.1 sat
   in. Checked by running rather than assumed: the dump restored into `mysql:8.4` gives
   **identical counts on all seven pins** and logs no errors. The dump is plain
   InnoDB/MyISAM `utf8mb3` with no routines, views or triggers and no MySQL-8-only
   collations, which is why this is a safe substitution.
2. **One appliance, not two containers.** Upstream is a compose project; every image in
   this fleet is a single appliance with one healthcheck. `config.php`'s `DB_HOST` therefore
   becomes `127.0.0.1` instead of `db`. `WEB_PATH` stays `getenv("CLASSIFIEDS")` so the base
   URL is still injected at runtime.
3. **Runtime debris dropped**: the three `oc-content/*.log` files (73 MB), `.DS_Store` and
   the `._*` AppleDouble files are not shipped.

## Build shape

`image.toml` datasets = the archive.org compose zip (for the dump and the reset script) and
`osclass-v8.1.2.zip` from SourceForge, both sha256-pinned. `[prepare]` derives the 73 GB of
uploads from the upstream image by digest — they exist in neither zip — and splits them for
the derived-inputs cache. The Dockerfile then goes `runtime` → `restore` → `audited` (its
own layer) → final with one `COPY` per staging bucket, using the shared
`builder/stage-lib/partition-tree.py`.
