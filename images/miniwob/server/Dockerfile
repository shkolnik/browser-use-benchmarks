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

EXPOSE 8399
# --chmod so the mode is a property of the BUILD, not of whoever checked the
# repo out: a COPY that inherited a 644 script died with "Permission denied"
# before any of its own validation could run.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
# Not nginx directly: HTTP_HOST/HTTP_PORT are validated before anything serves.
ENTRYPOINT ["/entrypoint.sh"]

# localhost:8399 is the IN-CONTAINER address — the healthcheck runs inside the
# container, so it uses the listen port, never the published one. curl is
# present in nginx:alpine (verified by running the image); wget is busybox's and
# is not available fleet-wide, so curl is the fleet's one probe.
#
# A real task page, not `/`: nginx answers `/` with a directory listing or a 404
# depending on config, and the failure this gate exists to catch is a tree that
# extracted to the wrong depth — which only a real content path can see.
HEALTHCHECK --start-period=10s --interval=10s --timeout=5s --retries=3 \
  CMD curl -fsS --max-time 4 http://localhost:8399/miniwob/click-button.html >/dev/null || exit 1

LABEL org.opencontainers.image.source="https://github.com/shkolnik/browser-use-benchmarks"
