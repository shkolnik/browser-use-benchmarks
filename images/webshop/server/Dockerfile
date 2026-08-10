# syntax=docker/dockerfile:1
# WebShop server, built from our fork shkolnik/WebShop at
# d8c8dc1b0605f86a5d734b37245abff857da38aa (upstream 64fa2a5 + shkolnik/WebShop#1).
# The fork's one behavioural change: the corpus is transformed into an LMDB product
# store HERE, at image-build time, instead of being json.load-ed on the first request
# that touches a product. Same rendered output, byte for byte; 57s -> 1.5s to first
# response, ~19G -> ~0.8G peak, and the three items_*.json files stop being runtime
# inputs entirely (see the bind mount below).
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
RUN --mount=type=bind,from=datasets,source=webshop-src-d8c8dc1.tar.gz,target=/tmp/webshop-src.tar.gz \
    mkdir /webshop \
 && tar -xzf /tmp/webshop-src.tar.gz --strip-components=1 -C /webshop \
 && mkdir -p /webshop/data
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
# The fork appends a pinned `lmdb==1.4.1` to requirements.txt, so it arrives through
# the same grep as everything else rather than as another hand-added package here.
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

# Point the app at the FULL dataset (upstream defaults to the 1000-item sample and
# documents editing these two constants to run on all products). The greps fail the
# build if a source bump ever makes the substitution stop matching.
RUN sed -i \
      -e "s|items_ins_v2_1000.json|items_ins_v2.json|" \
      -e "s|items_shuffle_1000.json|items_shuffle.json|" \
      web_agent_site/utils.py \
 && grep -q "data/items_shuffle.json'" web_agent_site/utils.py \
 && grep -q "data/items_ins_v2.json'" web_agent_site/utils.py

# Transform the corpus into the LMDB product store the server reads at boot. This is
# the whole point of the fork: the parse happens once, here, instead of on the first
# request in every container.
#
# The three items_*.json files are BIND-MOUNTED rather than COPY-ed, because after this
# step nothing reads them again — `load_products` opens only the store, and
# `load_attributes` is now called from build_product_store.py alone (verified by grep
# across web_agent_site/ at the pinned commit). Mounting keeps ~5.3G out of the image
# entirely; a COPY could not be undone by a later `rm`, since the bytes would already
# be committed to their own layer.
#
# Ordering is load-bearing in both directions: AFTER the sed, because the store is
# built from whatever DEFAULT_FILE_PATH names, and BEFORE the convert step below,
# which reads products back through `load_products` and so needs the store to exist.
RUN --mount=type=bind,from=datasets,source=items_shuffle.json,target=/webshop/data/items_shuffle.json \
    --mount=type=bind,from=datasets,source=items_ins_v2.json,target=/webshop/data/items_ins_v2.json \
    --mount=type=bind,from=datasets,source=items_human_ins.json,target=/webshop/data/items_human_ins.json \
    cd search_engine \
 && python build_product_store.py \
 && test -f /webshop/data/products.lmdb/data.mdb

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
# --chmod so the mode is a property of the BUILD, not of the checkout.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
# Not the app directly: HTTP_HOST/HTTP_PORT are validated before anything binds.
CMD ["/entrypoint.sh"]

# The app still binds port 3000 before it has loaded anything — `all_products` is a
# module global that web_agent_site/app.py's index() populates behind
# `if search_engine is None` — so the first request is still the one that does the
# work. What changed is how much work that is: it now mmaps the prebuilt store and
# attaches the Lucene index rather than parsing 5.1G of JSON. Measured on the fork:
# 1.5s to first response, 5.2s to docker-healthy, against 57s before — and that
# pre-fork load peaked around 19G RSS, close enough to this host's 39G/no-swap to
# involve the OOM killer.
#
# The old --timeout=115s existed solely to let one probe wait out that 57s load,
# so it retires with it.
#
# The start period is 180s, not the ~10x-the-measured-time 60s it was first set
# to. 5.2s to healthy was measured on a dedicated CI runner starting one image;
# on a NAS bringing the whole fleet up at once the same load ran past 60s and the
# container was declared unhealthy about 90s after its own start, while serving
# correctly minutes later. A measurement on an idle host is a floor, not a
# ceiling: contention can only inflate it, so sizing the grace period as a small
# multiple of the idle number encodes an assumption the deployment gets to break.
# 180s still surfaces a genuinely stuck container in about three minutes.
#
# Probe /abc, not `/`. `/` is served by home(), which only redirects and
# touches no data, so a probe of `/` proves nothing unless it follows the
# redirect: measured, `/` is 302 (195 bytes), /abc is 200 (1,466 bytes).
# Naming the destination directly gets the same proof in one request instead of
# two, and needs no -L. This redirect happens to stay on localhost, so -L was
# safe here — but the rule is fleet-wide on purpose: a healthcheck answers "is
# this container serving?" and must not depend on DNS, the proxy, or anything
# else outside the container. Two probes elsewhere DID follow a redirect to the
# published name and went permanently unhealthy for it.
HEALTHCHECK --start-period=180s --interval=5s --timeout=15s --retries=3 \
  CMD curl -fsS --max-time 10 http://localhost:3000/abc >/dev/null || exit 1

LABEL org.opencontainers.image.source="https://github.com/shkolnik/browser-use-benchmarks"
