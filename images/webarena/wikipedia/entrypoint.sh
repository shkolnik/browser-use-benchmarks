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
# The cost is real and worth stating: this writes ~15 GiB into the container's
# writable layer and takes a couple of minutes on first start, which is why the
# Dockerfile's HEALTHCHECK has a long start period. It is skipped when the part
# is already there at the right size, so restarting a container is free.
for first in /zim/*.part00; do
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

echo "serving http://${HTTP_HOST}:${HTTP_PORT}/ (listening on 80 in-container)"
# Hand back to the base image's own chain, with the ZIM path this image's CMD
# supplies still in "$@". dumb-init is what reaps kiwix-serve's children.
exec /usr/bin/dumb-init -- /usr/local/bin/start.sh "$@"
