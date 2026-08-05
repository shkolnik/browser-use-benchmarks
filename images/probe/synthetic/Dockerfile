# syntax=docker/dockerfile:1
# Small committed default: 2 chunks in a single COPY layer. For the real
# registry-size probe, generate N chunks with generate-dataset.sh (which also
# emits Dockerfile.generated — one COPY per chunk, for layer-per-chunk
# behavior) and build with `docker build -f Dockerfile.generated`.
FROM busybox
COPY --from=datasets probe-chunk-*.bin /data/
CMD ls -l /data
