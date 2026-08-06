# webshop-server — provenance & build notes

Upstream: [princeton-nlp/WebShop](https://github.com/princeton-nlp/WebShop), pinned at
commit `64fa2a5c15c7daa698b9ac93f5bb5437b634c9bd` (master head as of 2024-09-06; MIT).
A Flask app (port 3000) over ~1.18M scraped Amazon products with a pyserini/lucene
search index and a continuous 0–1 reward.

## Datasets

| file | size (bytes) | sha256 status |
|---|---|---|
| `webshop-src-64fa2a5.tar.gz` | 32,261,562 | computed here (curl + sha256sum) |
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
  the Dockerfile installs only the `==`-pinned lines of upstream's `requirements.txt`
  (dropping unpinned gdown/gradio/pytest/requests_mock — download/dev tools the served
  app never imports), and adds `torch==1.11.0+cpu` (same version, no CUDA payload; the
  server never imports torch), `faiss-cpu==1.7.2` (setup.sh conda-installs it unpinned;
  pyserini imports it), `Werkzeug==2.1.2` (Flask 2.1.2 breaks on Werkzeug>=2.3's
  removed `url_quote`), and `typing_extensions==4.5.0` (thinc 8.0.17 resolves pydantic
  to 1.8.2, which crashes at spacy import on typing_extensions>=4.6). Each break was
  hit live in this build, not speculative.
- **Validated live 2026-08-05:** index build over all 1,181,370 products took ~9 min
  (~5 min convert + ~4 min lucene, peak well under 39G RAM); `GET /` answers 302
  immediately at startup (so the 120s smoke healthcheck passes), while the FIRST real
  page load (`/abc`) takes ~2m20s loading the 5G JSON + goals. Search
  (`/search_results/<sid>/<kw>/1`) returns relevant products with ASINs/prices, and
  item pages render. Final image ~17G unpacked.
