# syntax=docker/dockerfile:1
# WebShop server, pinned to upstream 64fa2a5c15c7daa698b9ac93f5bb5437b634c9bd.
# Python 3.8 matches upstream's documented environment (README: Python 3.8.13);
# openjdk-11 is required by pyserini 0.17.0 for the lucene index build + search.
#
# DEVIATION from the "no network in docker build" rule: apt-get + pip below do reach the
# network. Unlike miniwob (static files) this image must install a pinned Python stack;
# every version is pinned by upstream's requirements.txt at the pinned commit, and all
# *datasets* still arrive checksummed via the datasets build context. See README.md.
FROM python:3.8-bullseye

RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-11-jre-headless \
 && rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64

# Upstream source at the pinned commit.
COPY --from=datasets webshop-src-64fa2a5.tar.gz /tmp/webshop-src.tar.gz
RUN mkdir /webshop \
 && tar -xzf /tmp/webshop-src.tar.gz --strip-components=1 -C /webshop \
 && rm /tmp/webshop-src.tar.gz
WORKDIR /webshop

# requirements.txt is fully version-pinned upstream (Flask 2.1.2, pyserini 0.17.0,
# spacy 3.3.0, torch 1.11.0, ...).
RUN pip install --no-cache-dir -r requirements.txt

# The spaCy model goal.py loads, installed from the checksummed release tarball.
COPY --from=datasets en_core_web_sm-3.3.0.tar.gz /tmp/en_core_web_sm.tar.gz
RUN pip install --no-cache-dir /tmp/en_core_web_sm.tar.gz && rm /tmp/en_core_web_sm.tar.gz

# Full product datasets (upstream setup.sh -d all layout: data/ under the repo root).
COPY --from=datasets items_shuffle.json items_ins_v2.json items_human_ins.json /webshop/data/

# Point the app at the FULL dataset (upstream defaults to the 1000-item sample and
# documents editing these two constants to run on all products).
RUN sed -i \
      -e "s|items_ins_v2_1000.json|items_ins_v2.json|" \
      -e "s|items_shuffle_1000.json|items_shuffle.json|" \
      web_agent_site/utils.py \
 && grep -q "data/items_shuffle.json" web_agent_site/utils.py

# Build the lucene search index at image-build time (upstream setup.sh does this once at
# install). convert writes resources{,_100,_1k,_100k}/documents.jsonl; we index only the
# full `resources` set — `indexes` is the directory the server uses when serving all
# products (run_indexing.sh's other three index dirs serve the sampled configs we don't run).
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
