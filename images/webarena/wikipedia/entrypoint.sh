#!/bin/sh
# Every image in this fleet takes the same two variables, and none of them
# defaults: HTTP_HOST and HTTP_PORT say how CLIENTS reach this container. They
# are NOT the address the server binds — the base image's start.sh hardcodes
# `kiwix-serve --port=80`, so this container always listens on 80 and compose
# publishes that on 8888 (WebArena's port for this service).
#
# Unlike the Magento images, nothing here is rewritten from these values:
# kiwix-serve emits only RELATIVE links. Verified 2026-08-08 against the real
# archive — the welcome page, an article and a search-results page contain no
# self-referencing absolute URL, only the XHTML namespace and a kiwix.org
# credit. They are still required, because "which address do clients use" is a
# question every image in this fleet answers the same way, and an image that
# quietly accepts no answer is the one that later ships somebody else's links.
set -eu

missing=
[ -n "${HTTP_HOST:-}" ] || missing="$missing HTTP_HOST"
[ -n "${HTTP_PORT:-}" ] || missing="$missing HTTP_PORT"
if [ -n "$missing" ]; then
  cat >&2 <<EOF
error:$missing not set — this image has no default hostname on purpose.

  HTTP_HOST and HTTP_PORT are how clients reach this container: the PUBLISHED
  side of the port mapping. This image listens on 80 INSIDE the container, so
  the published port is normally a different number.

  Run it like:
    docker run -e HTTP_HOST=localhost -e HTTP_PORT=8888 -p 8888:80 <image>
EOF
  exit 1
fi
case "$HTTP_PORT" in
  ''|*[!0-9]*) echo "error: HTTP_PORT must be a number, got '$HTTP_PORT'" >&2; exit 1 ;;
esac

# Reassemble any part that had to ship as sub-files.
#
# One part of this archive is a single Xapian index, 14.91 GiB, and search only
# works when an index sits entirely inside one part (see README.md). A part is a
# file and a file lives in exactly one layer, so that part is larger than any
# layer a registry accepts. It therefore ships as `<part>.partNN` chunks that are
# concatenated here, before kiwix-serve opens the archive. libzim never sees the
# chunks: it resolves `<stem>.zimaa`, `.zimab`, ... and `.partNN` matches none of
# those names.
#
# This one stays in PARTS_DIR beside its chunks: it is an intermediate, and the
# only thing that needs to reach ZIM_DIR is the finished archive.
PARTS_DIR=/zim-parts
for first in "$PARTS_DIR"/*.part00; do
    [ -e "$first" ] || break
    part="${first%.part00}"
    # Deliberately NOT `set -- "$part".part??`: "$@" still holds the ZIM path
    # from CMD, which the exec at the bottom passes to kiwix-serve.
    want=0
    count=0
    for chunk in "$part".part??; do
        want=$((want + $(stat -c %s "$chunk")))
        count=$((count + 1))
    done
    if [ -f "$part" ] && [ "$(stat -c %s "$part")" -eq "$want" ]; then
        echo "reassemble: $part already present at $want bytes"
        continue
    fi
    echo "reassemble: building $part from $count sub-files ($((want / 1073741824)) GiB)..."
    # Not `>>`: a partial file from a killed start would otherwise be extended
    # rather than replaced, and the size check below would pass on garbage.
    rm -f "$part"
    cat "$part".part?? > "$part"
    got=$(stat -c %s "$part")
    if [ "$got" != "$want" ]; then
        echo "error: $part is $got bytes, expected $want — out of disk?" >&2
        exit 1
    fi
    echo "reassemble: $part complete"
done

# Reassemble the WHOLE archive from its parts, when it is not already there.
#
# libzim can open a split archive — it resolves `<stem>.zimaa`, `.zimab`, ... from
# a `<stem>.zim` path with no file at it — and that is how this image used to
# serve. Articles are perfect that way. Full-text search is not: measured on this
# archive, `/search` answers 404 "Fulltext search unavailable" and `/suggest`
# silently degrades to an alphabetical prefix scan, and it does so EVEN WITH both
# Xapian indexes lying whole inside a single part. See README.md for what was
# ruled out. A whole file is the only layout measured to serve search, so the
# parts exist purely to get under the registry's per-layer ceiling and are joined
# back here before kiwix-serve ever opens them.
#
# The cost is a second full copy: ~89 GiB and **632 s measured** on the real
# archive — ~144 MiB/s, not the ~640 MiB/s a single part reaches, because this
# reads and writes the same disk at once. The parts sit in read-only image
# layers, so deleting them afterwards reclaims nothing.
#
# WHERE that copy lands is the whole question of how often it is paid. The parts
# ship in the image at PARTS_DIR; the joined archive is written to the DIRECTORY
# OF THE CMD PATH, which is deliberately a different, empty directory:
#
#   - leave it alone and it is the container's writable layer — paid once per
#     CONTAINER. `docker restart` is free, `docker run` again is not.
#   - mount a named volume there and it is paid ONCE for the life of the volume,
#     however many containers come and go. `ZIM_JOIN_ONLY=1` does the join and
#     exits, so it can be done once right after `docker pull` rather than on the
#     first boot somebody is waiting for.
#
# The volume must be mounted on the archive's directory and NOT on PARTS_DIR:
# Docker seeds an empty named volume from the image's contents at that path, so
# a volume over the parts would copy 89 GiB before the join wrote another 89 GiB
# into the same volume. Keeping them apart is what makes that impossible.
archive=${1:-}
if [ -n "$archive" ] && [ ! -f "$archive" ]; then
    stem=$(basename "$archive")
    want=0
    count=0
    for part in "$PARTS_DIR/$stem"??; do
        [ -e "$part" ] || break
        want=$((want + $(stat -c %s "$part")))
        count=$((count + 1))
    done
    if [ "$count" -eq 0 ]; then
        echo "error: no $archive, and no $PARTS_DIR/$stem'aa','ab',... to build it from" >&2
        exit 1
    fi
    mkdir -p "$(dirname "$archive")"
    echo "reassemble: joining $count parts into $archive ($((want / 1073741824)) GiB)..."
    # Not `>>`: a partial file from a killed start would be extended rather than
    # replaced, and the size check below would then pass on garbage.
    rm -f "$archive"
    cat "$PARTS_DIR/$stem"?? > "$archive"
    got=$(stat -c %s "$archive")
    if [ "$got" != "$want" ]; then
        echo "error: $archive is $got bytes, expected $want — out of disk?" >&2
        exit 1
    fi
    echo "reassemble: $archive complete"
elif [ -n "$archive" ]; then
    echo "reassemble: $archive already whole at $(stat -c %s "$archive") bytes — skipping"
fi

# Populate-and-exit, so the 632 s can be spent once right after `docker pull`
# into a named volume instead of on a boot someone is waiting for.
if [ -n "${ZIM_JOIN_ONLY:-}" ]; then
    echo "ZIM_JOIN_ONLY set — archive is ready, exiting without serving"
    exit 0
fi

echo "serving http://${HTTP_HOST}:${HTTP_PORT}/ (listening on 80 in-container)"
# Hand back to the base image's own chain, with the ZIM path this image's CMD
# supplies still in "$@". dumb-init is what reaps kiwix-serve's children.
exec /usr/bin/dumb-init -- /usr/local/bin/start.sh "$@"
