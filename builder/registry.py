"""What the registry already holds, and which commit it was built from.

`build-images` rebuilds an image when its own directory — or a shared build
input — changed since the last green build of that image. The rule is right;
what it needs is a trustworthy "last green build", and that has to be a fact
about the IMAGE, not about a workflow run. A run conclusion is a verdict on
eleven independent builds at once: one image failing makes the run red, so a
commit at which ten images published perfectly never becomes an anchor, and
the next push re-derives all eleven from a stale base.

The published image answers the same question without that coupling. It exists
in GHCR only if its build, its smoke test and its push all succeeded, and it
carries `org.opencontainers.image.revision` naming the commit it was built
from. Existence and label together are the whole record — one object, written
once, that cannot disagree with a second copy of itself.

Reading it back is a manifest fetch and a config-blob fetch. The token and
redirect handling are `derive_cache_fetch`'s, which already talks to this
registry with this repo's credentials.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from builder.derive_cache_fetch import _DropAuthOnRedirect, fetch_token, scheme

REVISION_LABEL = "org.opencontainers.image.revision"

# Both OCI and Docker spellings, index and single manifest. A registry serves
# whichever the pusher wrote, and which one that is depends on the builder's
# configuration rather than on anything this repo decides, so ask for all four
# and branch on what comes back.
_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


class Unavailable(Exception):
    """The registry could not be asked. Distinct from a 404, which is an
    answer: 'this image is not published'. Not knowing is not the same as
    knowing there is nothing there, and only one of the two is worth
    reporting to an operator who is wondering why everything rebuilt."""


def split_ref(ref: str) -> tuple[str, str, str]:
    """`ghcr.io/owner/name:tag` -> (registry, repo path, tag).

    The tag is optional and the split is not a plain `rsplit(":")`: a registry
    may carry a port, so a colon only introduces a tag when it appears after
    the last slash.
    """
    name, sep, tag = ref.rpartition(":")
    if not sep or "/" in tag:
        name, tag = ref, "latest"
    registry, _, repo_path = name.partition("/")
    return registry, repo_path, tag


def _get_json(opener, url, token, accept=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if accept:
        req.add_header("Accept", accept)
    try:
        with opener.open(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            # 401/403 alongside 404 deliberately: a package that has never been
            # pushed is not merely absent from GHCR, it does not exist to
            # authorize, and the registry says so with an auth error rather
            # than a not-found. Both mean "nothing published here".
            return None
        raise Unavailable(f"{url}: HTTP {e.code}")
    except (OSError, ValueError) as e:
        raise Unavailable(f"{url}: {e}")


def published_revision(ref: str, opener=None) -> str | None:
    """The commit `ref` was built from, or None if nothing is published there.

    Raises `Unavailable` if the registry could not be reached or answered
    something unusable — never conflated with None, because the caller's
    response to the two is the same (build the image) but what it should tell
    the operator is not.
    """
    registry, repo_path, tag = split_ref(ref)
    opener = opener or urllib.request.build_opener(_DropAuthOnRedirect)
    base = f"{scheme(registry)}://{registry}/v2/{repo_path}/"
    token = fetch_token(registry, repo_path)

    manifest = _get_json(opener, f"{base}manifests/{urllib.parse.quote(tag)}",
                         token, _ACCEPT)
    if manifest is None:
        return None

    # An index points at per-platform manifests; the config with our label is
    # one level further down. Prefer linux/amd64 — the only platform this fleet
    # builds — and fall back to the first entry rather than failing, so a
    # single-platform index without a populated platform field still resolves.
    #
    # Entries whose platform is `unknown/unknown` are excluded from that
    # fallback: that is how a provenance attestation rides in an image index,
    # and its config carries none of the image's labels. Preferring amd64
    # already steps over it, but the fallback would walk straight into it.
    if "manifests" in manifest and "config" not in manifest:
        entries = [e for e in manifest.get("manifests") or []
                   if (e.get("platform") or {}).get("architecture") != "unknown"]
        if not entries:
            raise Unavailable(f"{ref}: index lists no image manifests")
        chosen = next(
            (e for e in entries
             if (e.get("platform") or {}).get("architecture") == "amd64"
             and (e.get("platform") or {}).get("os") == "linux"),
            entries[0])
        manifest = _get_json(opener, f"{base}manifests/{chosen['digest']}",
                             token, _ACCEPT)
        if manifest is None:
            return None

    try:
        config_digest = manifest["config"]["digest"]
    except (KeyError, TypeError):
        raise Unavailable(f"{ref}: manifest has no config descriptor")

    config = _get_json(opener, f"{base}blobs/{config_digest}", token)
    if config is None:
        raise Unavailable(f"{ref}: config blob {config_digest} is missing")
    labels = (config.get("config") or {}).get("Labels") or {}
    return labels.get(REVISION_LABEL)
