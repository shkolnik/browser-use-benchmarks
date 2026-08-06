# syntax=docker/dockerfile:1
# WebShop server, pinned to upstream 64fa2a5c15c7daa698b9ac93f5bb5437b634c9bd.
# Python 3.8 matches upstream's documented environment (README: Python 3.8.13);
# openjdk-11 is required by pyserini 0.17.0 for the lucene index build + search —
# the JRE stays in the final image because LuceneSearcher needs a JVM at query
# time, not just while indexing.
#
# DEVIATION from the "no network in docker build" rule: apt-get + pip below do reach the
# network. Unlike miniwob (static files) this image must install a pinned Python stack;
# every pip version is pinned (upstream's requirements.txt at the pinned commit, minus
# its unpinned lines — see below), and all *datasets* still arrive checksummed via the
# datasets build context. See README.md.
FROM python:3.8-bullseye

RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-11-jre-headless \
 && rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64

# Upstream source at the pinned commit (bind mount: the tar never enters a layer).
RUN --mount=type=bind,from=datasets,source=webshop-src-64fa2a5.tar.gz,target=/tmp/webshop-src.tar.gz \
    mkdir /webshop \
 && tar -xzf /tmp/webshop-src.tar.gz --strip-components=1 -C /webshop
WORKDIR /webshop

# Install exactly the '==' pinned lines of upstream's requirements.txt (Flask 2.1.2,
# pyserini 0.17.0, spacy 3.3.0, torch 1.11.0, ...). The grep drops the UNpinned lines
# — gdown, gradio, pytest, requests_mock — dataset-download and dev/test tools the
# served app never imports, whose unpinned resolution would be today's, not 2022's.
# Two additions, both pinned:
#   torch==1.11.0+cpu — same version as upstream's pin, minus the ~2.5G CUDA payload
#     (the served app never imports torch; only the gym text-env does).
#   faiss-cpu==1.7.2 — setup.sh conda-installs faiss-cpu unpinned; pyserini imports
#     faiss at module load, so pin the wheel contemporary with pyserini 0.17.0.
#   Werkzeug==2.1.2 — Flask 2.1.2 declares only Werkzeug>=2.0, and Werkzeug 2.3
#     removed url_quote, breaking `from flask import ...` at import time (verified in
#     this build). 2.1.2 is Werkzeug's contemporary release for Flask 2.1.2.
#   typing_extensions==4.5.0 — spacy 3.3.0's dep chain (thinc 8.0.17) resolves
#     pydantic to 1.8.2, whose compiled validators crash on typing_extensions>=4.6
#     ("issubclass() arg 1 must be a class" at spacy import; verified live in this
#     image). 4.5.0 is the last release before the break.
RUN grep '==' requirements.txt > /tmp/requirements-pinned.txt \
 && sed -i 's|^torch==1.11.0$|torch==1.11.0+cpu|' /tmp/requirements-pinned.txt \
 && grep -qx 'torch==1.11.0+cpu' /tmp/requirements-pinned.txt \
 && pip install --no-cache-dir -r /tmp/requirements-pinned.txt faiss-cpu==1.7.2 Werkzeug==2.1.2 typing_extensions==4.5.0 \
      --extra-index-url https://download.pytorch.org/whl/cpu \
 && rm /tmp/requirements-pinned.txt

# The spaCy model goal.py loads, installed from the checksummed release tarball.
RUN --mount=type=bind,from=datasets,source=en_core_web_sm-3.3.0.tar.gz,target=/tmp/en_core_web_sm.tar.gz \
    pip install --no-cache-dir /tmp/en_core_web_sm.tar.gz

# Full product datasets (upstream setup.sh -d all layout: data/ under the repo root).
COPY --from=datasets items_shuffle.json items_ins_v2.json items_human_ins.json /webshop/data/

# Point the app at the FULL dataset (upstream defaults to the 1000-item sample and
# documents editing these two constants to run on all products). The greps fail the
# build if a source bump ever makes the substitution stop matching.
RUN sed -i \
      -e "s|items_ins_v2_1000.json|items_ins_v2.json|" \
      -e "s|items_shuffle_1000.json|items_shuffle.json|" \
      web_agent_site/utils.py \
 && grep -q "data/items_shuffle.json'" web_agent_site/utils.py \
 && grep -q "data/items_ins_v2.json'" web_agent_site/utils.py

# Build the lucene search index at image-build time (upstream setup.sh does this once at
# install). convert writes resources{,_100,_1k,_100k}/documents.jsonl; we index only the
# full `resources` set — `indexes` is the directory the server uses when serving all
# products (run_indexing.sh's other three index dirs serve the sampled configs we don't
# run) — and drop the intermediate documents.jsonl trees in the same layer.
RUN cd search_engine \
 && mkdir -p resources resources_100 resources_1k resources_100k indexes \
 && python convert_product_file_format.py \
 && python -m pyserini.index.lucene \
      --collection JsonCollection \
      --input resources \
      --index indexes \
      --generator DefaultLuceneDocumentGenerator \
      --threads 1 \
      --storePositions --storeDocvectors --storeRaw \
 && rm -rf resources resources_100 resources_1k resources_100k

EXPOSE 3000
CMD ["python", "-m", "web_agent_site.app"]

LABEL org.opencontainers.image.source="https://github.com/shkolnik/browser-use-benchmarks"
