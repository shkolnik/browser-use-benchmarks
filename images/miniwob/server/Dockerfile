# syntax=docker/dockerfile:1
FROM nginx:alpine
COPY --from=datasets miniwob-plusplus.tar.gz /tmp/miniwob-plusplus.tar.gz
# The tag archive's task HTML lives at <top-dir>/miniwob/html/{miniwob,common,core,...}/.
# Stripping 3 path components (top-dir, "miniwob", "html") while restricting extraction
# to that one subtree lands miniwob/, common/, core/ as siblings directly under the
# nginx root, matching the relative asset paths ("../core/...", "../common/...") the
# task HTML files use.
RUN mkdir -p /usr/share/nginx/miniwob \
 && top="$(tar -tzf /tmp/miniwob-plusplus.tar.gz | head -1)" \
 && tar -xzf /tmp/miniwob-plusplus.tar.gz --strip-components=3 \
      -C /usr/share/nginx/miniwob "${top}miniwob/html" \
 && rm /tmp/miniwob-plusplus.tar.gz
COPY nginx.conf /etc/nginx/conf.d/default.conf
