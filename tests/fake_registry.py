"""A real HTTP registry, in-process, for the derived-cache transfer tests.

The transfer engine used to be shell driving `curl`, and its tests faked curl
itself: a shell script on PATH that wrote bytes into the output file and
printed a status. That fake could only ever model what we thought curl did.
Now that the engine speaks HTTP directly, the tests can serve real HTTP —
ranges, redirects, 429s with Retry-After, a connection dropped mid-body — and
assert on what the engine does with the wire behaviour rather than with our
impression of it.

Every failure mode here is one that actually happened to this fleet: the
periodic mid-transfer cutoff (runs 31460767854, 31464177083), the rate limit
under fleet concurrency (run 31654975042), and a range request answered with a
whole body.
"""
import hashlib
import http.server
import json
import threading


class Behaviour:
    """What the registry should do to the next N blob requests.

    Defaults to a well-behaved registry; each field is one way to misbehave.
    """

    def __init__(self, rate_limit=0, retry_after=None, cut_after=None,
                 ignore_range=False, corrupt=False, redirect_to=None):
        self.rate_limit = rate_limit        # refuse this many requests with 429
        self.retry_after = retry_after      # value of the Retry-After header
        self.cut_after = cut_after          # send this many bytes, then hang up
        self.ignore_range = ignore_range    # answer 200 + whole body to a Range
        self.corrupt = corrupt              # serve bytes that hash to nothing
        self.redirect_to = redirect_to      # 307 the blob to another host
        self.requests = []                  # (path, Range header) per request


class FakeRegistry:
    """Serves `blobs` (title -> bytes) at /v2/<repo>/blobs/sha256:<digest>."""

    def __init__(self, blobs, behaviour=None, repo="x", manifests=None):
        self.blobs = {hashlib.sha256(b).hexdigest(): b for b in blobs.values()}
        # Tag -> manifest bytes. Manifests addressed BY DIGEST are served from
        # `blobs` like anything else, which is what lets an index and the
        # per-platform manifest it points at be registered the same way.
        self.manifests = dict(manifests or {})
        self.behaviour = behaviour or Behaviour()
        self.repo = repo
        self.tokens_issued = 0
        self.auth_seen = []

        registry = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                b = registry.behaviour
                registry.auth_seen.append(self.headers.get("Authorization"))
                if self.path.startswith("/token"):
                    registry.tokens_issued += 1
                    body = json.dumps({"token": "faketoken"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if "/manifests/" in self.path and "sha256:" not in self.path:
                    tag = self.path.rsplit("/", 1)[-1]
                    body = registry.manifests.get(tag)
                    if body is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/vnd.oci.image.manifest.v1+json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                digest = self.path.rsplit("sha256:", 1)[-1]
                rng = self.headers.get("Range")
                b.requests.append((self.path, rng))
                if digest not in registry.blobs:
                    self.send_error(404)
                    return

                if b.rate_limit > 0:
                    b.rate_limit -= 1
                    self.send_response(429)
                    if b.retry_after is not None:
                        self.send_header("Retry-After", str(b.retry_after))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if b.redirect_to:
                    self.send_response(307)
                    self.send_header("Location", b.redirect_to + self.path)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                body = registry.blobs[digest]
                if b.corrupt:
                    body = b"\0" * len(body)

                start = 0
                if rng and not b.ignore_range:
                    start = int(rng.split("=")[1].split("-")[0])
                    if start >= len(body):
                        self.send_response(416)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header(
                        "Content-Range",
                        f"bytes {start}-{len(body) - 1}/{len(body)}")
                else:
                    self.send_response(200)

                rest = body[start:]
                if b.cut_after is not None:
                    # Declare the full length and then send less: exactly what
                    # a connection torn down mid-body looks like from the
                    # client, and the case whole-transfer retries could never
                    # make progress against.
                    self.send_header("Content-Length", str(len(rest)))
                    self.end_headers()
                    self.wfile.write(rest[:b.cut_after])
                    self.close_connection = True
                    return
                self.send_header("Content-Length", str(len(rest)))
                self.end_headers()
                self.wfile.write(rest)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    @property
    def ref(self):
        return f"{self.host}/{self.repo}:t"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def manifest_for(files, artifact=True):
    """The oras manifest for `files` (title -> bytes)."""
    m = {"layers": [
        {"digest": "sha256:" + hashlib.sha256(b).hexdigest(),
         "size": len(b),
         "annotations": {"org.opencontainers.image.title": t}}
        for t, b in files.items()]}
    if artifact:
        m["artifactType"] = "application/vnd.beep.derived.v1"
    return json.dumps(m).encode()
