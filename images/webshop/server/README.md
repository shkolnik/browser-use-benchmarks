# webshop-server — provenance & build notes

Source: **our fork** [shkolnik/WebShop](https://github.com/shkolnik/WebShop), pinned at
commit `d8c8dc1b0605f86a5d734b37245abff857da38aa` (master, 2026-08-07; MIT). That is
upstream [princeton-nlp/WebShop](https://github.com/princeton-nlp/WebShop) `64fa2a5`
(master head as of 2024-09-06) plus [shkolnik/WebShop#1](https://github.com/shkolnik/WebShop/pull/1).
A Flask app (port 3000) over ~1.18M scraped Amazon products with a pyserini/lucene
search index and a continuous 0–1 reward.

## Why a fork

Upstream loads the corpus lazily *at runtime*: the first request that touches a product
`json.load`s the 5.1G `items_shuffle.json` into ~1.18M live Python dicts. Measured here:
**57s to first response and ~19G peak**, OOM-killed under `docker run -m 16g`. That is a
per-container cost paid on every start, and it is what the old 120s health-check start
period and 900s smoke budget existed to absorb.

The fork moves that transform to **image-build time**, into an LMDB store of one
zlib-compressed JSON blob per product plus packed order/pricing/category indexes. The
server mmaps it and decodes products on access, so the resident set is reclaimable page
cache rather than heap. Rendered output is byte-identical (verified across 23 pages and
50 goals against two unmodified baselines; see the PR).

| | upstream | fork |
|---|---|---|
| time to first response | 57 s | **1.5 s** |
| time to docker `healthy` | ≥57 s | **5.2 s** |
| peak memory | ~19 G | **~0.8 G** |
| lowest working `docker run -m` | OOM at 16g | **768m** |

Two consequences for this image, both reflected in the Dockerfile:

- **The three `items_*.json` files are now build-time-only inputs.** `load_products`
  opens the store alone, and `load_attributes` is called only by
  `search_engine/build_product_store.py`. So they are bind-mounted into the build step
  rather than `COPY`-ed, keeping ~5.3G out of the shipped image.
- **Step ordering is load-bearing.** `build_product_store.py` runs after the `sed` that
  repoints `utils.py` at the full dataset (it builds whatever `DEFAULT_FILE_PATH` names)
  and before `convert_product_file_format.py` (which now reads products back out through
  `load_products`, so the store must already exist).

Rebasing onto a newer upstream is an ordinary fork merge plus a new pin in `image.toml`;
the trust model is unchanged, since either way the pin is a commit hash.

## Datasets

| file | size (bytes) | sha256 status |
|---|---|---|
| `webshop-src-d8c8dc1.tar.gz` | 32,264,591 | computed here (curl + sha256sum) |
| `items_shuffle.json` | 5,479,720,229 (~5.1G) | HF LFS oid; **confirmed by local download 2026-08-05** (`bin/build download` computed the same sha256) |
| `items_ins_v2.json` | 186,295,270 (~178M) | HF LFS oid; confirmed by local download 2026-08-05 |
| `items_human_ins.json` | 5,137,548 | computed here; downloaded from both mirrors, byte-identical |
| `en_core_web_sm-3.3.0.tar.gz` | 12,800,188 | computed here |

Provenance: upstream's `setup.sh -d all` fetches the three `items_*` files from Google
Drive via `gdown` (ids `1A2whVgOO0euk5O13n2iYDM0bQRkkRduB`, `1s2j6NgHljiZzQNL3veZaAiyW_qDEgBNi`,
`14Kb5SPBk_jfdLZ_CDBNitW98QLDlKR5O`). Google Drive is not curl-fetchable (confirm-token
dance), so we use Hugging Face community mirrors instead — `YWZBrandon/webshop-data`,
`HongbangYuan/webshop`, `Yuxuan13/webshop_dataset` — which report identical sizes and
identical LFS sha256 oids for the two large files. An LFS oid IS the file's sha256, so
`bin/build download` verifies against exactly that value; three mirrors agreeing is the
current provenance evidence. When the first real download completes, the computed hash
either confirms these entries or fails loudly — either outcome is the verification.
**2026-08-05: the first real download completed and confirmed both LFS-oid entries** —
the builder computed matching sha256s for `items_shuffle.json` and `items_ins_v2.json`.

## Deviations / deferred

- **`docker build` uses the network** (apt openjdk-11, pip) — unlike miniwob. Everything
  pip installs is version-pinned by upstream's `requirements.txt` at the pinned commit;
  all datasets still arrive checksummed via the `datasets` build context. Vendoring
  wheels would restore the no-network property but is not worth it for v1.
- **Full-dataset constants:** upstream defaults `web_agent_site/utils.py` to the
  1000-item sample; the Dockerfile seds `DEFAULT_FILE_PATH`/`DEFAULT_ATTR_PATH` to the
  full files (upstream's documented way to serve all products) and greps to fail the
  build if the substitution ever stops matching.
- **Product store built at image-build time** (`search_engine/build_product_store.py`,
  from the fork) — the LMDB the server reads at boot. Writing goes to a scratch env with
  a sparse 32G map and is then LMDB copy-compacted into place, so what ships is a dense
  ~2.7G file rather than a sparse one a docker layer's tar would expand.
- **Index build happens at image-build time** (lucene over the full ~1.18M products,
  single-threaded — expect a long build and a large image, roughly 6G of data + index +
  a torch-bearing Python stack). Only the full `resources` set is indexed; the
  100/1k/100k sampled indexes from upstream's `run_indexing.sh` are skipped, and the
  intermediate `documents.jsonl` trees (~2x the products file) are deleted in the same
  layer.
- **spaCy model:** `setup.sh` downloads `en_core_web_lg`, but the served code
  (`web_agent_site/engine/goal.py`) loads `en_core_web_sm`; we ship only `_sm` as a
  checksummed dataset.
- **Pinned lines only, plus four extra pins (built + validated live 2026-08-05):**
  the Dockerfile installs only the `==`-pinned lines of the fork's `requirements.txt`
  (dropping unpinned gdown/gradio/pytest/requests_mock — download/dev tools the served
  app never imports), and adds `torch==1.11.0+cpu` (same version, no CUDA payload; the
  server never imports torch), `faiss-cpu==1.7.2` (setup.sh conda-installs it unpinned;
  pyserini imports it), `Werkzeug==2.1.2` (Flask 2.1.2 breaks on Werkzeug>=2.3's
  removed `url_quote`), and `typing_extensions==4.5.0` (thinc 8.0.17 resolves pydantic
  to 1.8.2, which crashes at spacy import on typing_extensions>=4.6). Each break was
  hit live in this build, not speculative.
- **Validated live 2026-08-05 (pre-fork):** index build over all 1,181,370 products took
  ~9 min; `GET /` answered 302 immediately at startup, while the FIRST real page load
  (`/abc`) took ~2m20s loading the 5G JSON + goals. Final image ~17.3G.
- **Validated live 2026-08-07 (this image, on the fork).** Full `bin/build build
  webshop/server` on the sandbox host:
  - `build_product_store.py` **118s**; lucene index **290s**, still `indexed: 1,181,370
    / unindexable: 0 / errors: 0` — same corpus, same count as pre-fork.
  - Image **17.3G → 15.8G**. Smaller than the 5.3G of JSON removed, because the 2.7G
    store replaces it; `/webshop/data/` in the built image contains `products.lmdb` and
    nothing else, which is the check that the bind mount really kept the JSON out.
  - `docker run -m 2g` reached **healthy in 6.9s** with a **527M peak** (cgroup
    `memory.peak`), against a 60s start period.
  - Serving verified, not just liveness: index 200 in 4ms; `/search_results/abc/shampoo/1`
    200 in 91ms, a 20K page carrying real ASINs; an item page 200 in 9ms showing a price.
    Byte-for-byte output equality with unmodified upstream was established separately, in
    shkolnik/WebShop#1, across 23 pages and 50 goals against two baselines.
