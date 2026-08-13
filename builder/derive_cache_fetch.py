"""The derived-inputs cache's blob transfer engine.

Called by builder/stage-lib/derive-cache.sh, which still owns the protocol
around it — the oras manifest fetch, the digest lock, the hit/miss decision
table, the push. This module owns exactly one job: given a manifest, put its
layers on disk, verified, resuming whatever a previous attempt left behind.

WHY THIS IS NOT `oras pull`. `oras pull` is one transfer: interrupt it at 99%
and it starts over. Runs 31460767854 and 31464177083 failed all six attempts
against wikipedia's 95 GB entry with

  copy failed: stream error: stream ID N; PROTOCOL_ERROR; received from peer

while making real progress every time — one attempt moved 19 GB correctly and
threw all of it away. With interruptions arriving every 10-15 minutes and an
uninterrupted pull needing ~19, whole-transfer retries can fail forever.

WHY THE UNIT IS THE BYTE. Blob granularity was the first fix and was not
enough: run 31497034118 saw vwa/classifieds (9 layers of 8.59 GB) and
webarena/gitlab burn all eight passes on part-00 and finish nothing, because a
pass can only skip what a previous pass FINISHED. Resumption coarser than the
interruption period does not converge, it just fails more slowly. So progress
is measured in bytes: an interruption costs the bytes in flight and nothing
else.

WHY THIS IS PYTHON. It was bash, and the bash worked, but the job had grown
past what the language holds safely: HTTP range resumption, redirects that
must drop credentials, token acquisition, a background progress watcher that
had to be reaped by PID, digest verification, and a backoff state machine.
Two things in particular are gone with the port. The progress reporter was a
subprocess watching the file's size from outside — the only signal available
to it — and is now three lines inside the read loop that already knows how
many bytes it has. And the retry policy could not see WHY a fetch failed:
dcache__transfer ran the fetch in a command substitution, so nothing it set
survived, and the registry's Retry-After had to be printed into the log and
regex'd back out by the caller. Here a rate limit is an exception carrying a
number.
"""
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


def log(msg):
    """Every line this module writes is prefixed and goes to stderr.

    stderr because stdout of the surrounding shell function is not ours, and
    the prefix because these lines land in the middle of a build log where
    `derive-cache:` is how an operator finds them.
    """
    print(f"derive-cache: {msg}", file=sys.stderr, flush=True)


class Interrupted(Exception):
    """A transfer that stopped early. Retryable by definition — the manifest
    already proved the entry exists, so this is never 'not found'."""


class RateLimited(Interrupted):
    """HTTP 429. Not a broken transfer: nothing is wrong with the link, the
    socket, or the bytes on disk, and the registry has said so."""

    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


class Corrupt(Exception):
    """Bytes that hash to the wrong thing. Never resumed, always discarded —
    a bad prefix that is re-appended every pass is immortal."""


def env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass(frozen=True)
class Layer:
    digest: str
    size: int
    title: str

    @property
    def want(self):
        return self.digest.split(":", 1)[1]


def parse_layers(manifest):
    """Manifest bytes -> the layers that become files.

    Only layers carrying `org.opencontainers.image.title` do, which is oras's
    own rule; matching it is what keeps this equivalent to `oras pull`.

    The title comes from the registry and is used as a PATH. A title of
    `../../etc/cron.d/x` would write outside the destination, so anything that
    is not a bare filename is refused rather than sanitised: every entry this
    fleet pushes is a basename, so one that isn't means something is wrong
    that quietly stripping slashes would hide.
    """
    try:
        m = json.loads(manifest)
    except ValueError as e:
        raise SystemExit(f"derive-cache: manifest is not JSON: {e}")
    out = []
    for layer in m.get("layers", []):
        title = (layer.get("annotations") or {}).get(
            "org.opencontainers.image.title")
        if not title:
            continue
        if "/" in title or title in (".", ".."):
            raise SystemExit(
                f"derive-cache: refusing layer title {title!r}: not a bare filename")
        out.append(Layer(layer["digest"], int(layer["size"]), title))
    return out


def scheme(registry):
    """localhost is implicitly insecure — the same rule Docker itself applies,
    and what lets the round-trip test run against a plain-HTTP `registry:2`."""
    host = registry.split(":", 1)[0]
    return "http" if host in ("localhost", "127.0.0.1") else "https"


def registry_creds(registry):
    """`(user, password)` from the Docker config `docker login` already wrote,
    so this library needs no credentials of its own; on the runner that is the
    GITHUB_TOKEN the `login to ghcr` step logs in with.

    Returns None when the entry is missing or held by a credential helper. An
    anonymous token is tried instead — enough for a public package, and for a
    private one the fetch then fails loudly rather than writing a 401 body
    into a blob file.
    """
    import base64
    path = Path(os.environ.get("DOCKER_CONFIG") or
                os.path.expanduser("~/.docker")) / "config.json"
    try:
        cfg = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    entry = (cfg.get("auths") or {}).get(registry)
    if not entry:
        return None
    if "auth" in entry:
        try:
            user, _, pw = base64.b64decode(entry["auth"]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return None
        if user:
            return (user, pw)
    if entry.get("username"):
        return (entry["username"], entry.get("password", ""))
    return None


def fetch_token(registry, repo_path, timeout=60):
    """A pull-scope bearer token, or None.

    Bounded, because this runs before every blob fetch and a hung token
    request stalls the pull just as completely as a hung blob does — with none
    of the progress reporting, since no file is growing to watch.

    A failure here is not fatal: an anonymous pull of a public package needs
    no token at all, so this returns None and lets the blob request be the
    thing that succeeds or fails.
    """
    url = (f"{scheme(registry)}://{registry}/token"
           f"?service={urllib.parse.quote(registry)}"
           f"&scope=repository:{urllib.parse.quote(repo_path)}:pull")
    req = urllib.request.Request(url)
    creds = registry_creds(registry)
    if creds:
        import base64
        basic = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {basic}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    return d.get("token") or d.get("access_token") or None


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Strip Authorization when a redirect crosses to another host.

    A registry answers a blob GET with a redirect to storage, and the redirect
    URL carries its own signature. curl drops the header here and is right to:
    forwarding a registry bearer token to an arbitrary redirect target hands
    our credential to whoever the registry pointed us at. urllib does NOT drop
    it on its own — headers added to the Request travel the whole chain — so
    this is a security behaviour we would silently lose in the port.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.headers = {k: v for k, v in new.headers.items()
                           if k.lower() != "authorization"}
            new.unredirected_hdrs = {
                k: v for k, v in getattr(new, "unredirected_hdrs", {}).items()
                if k.lower() != "authorization"}
        return new


def retry_after_seconds(headers):
    """The Retry-After delay in seconds, or None.

    RFC 9110 allows either a delay in seconds or an HTTP-date. Only the
    numeric form is honoured: parsing a date means three legal formats and the
    clock skew between us and the registry, and getting it wrong yields a wait
    that is either useless or enormous. A date-valued header returns None,
    which is not a failure — the caller's own backoff is the floor under every
    wait, so the unparsed case degrades to exactly the behaviour we would have
    had without reading the header at all.
    """
    v = (headers.get("Retry-After") or "").strip()
    return int(v) if v.isdigit() else None


def fetch_blob(opener, url, token, layer, out, log_=log):
    """One blob, resumed from whatever is already in `out`.

    The resume is a byte range and the check is the whole digest, which is the
    only safe combination: a Range request can be answered by a different
    backend than served the first half, a leftover file can be anything, and
    appending to the wrong prefix produces a file of exactly the right size
    and the wrong content.
    """
    have = out.stat().st_size if out.exists() else 0
    if have >= layer.size:
        # At or past the full length and still not verified (the caller checks
        # before calling), so the bytes are wrong, not incomplete. Resuming
        # would ask for a range past the end and 416; start clean instead.
        out.unlink()
        have = 0

    if have:
        log_(f"resuming {layer.title} at {have}/{layer.size} bytes")
    else:
        log_(f"fetching {layer.title} ({layer.size} bytes)")

    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if have:
        req.add_header("Range", f"bytes={have}-")

    # A connect/read timeout is what makes the resume REACHABLE. Without one,
    # a connection that is blackholed rather than reset — what a link failing
    # over mid-flow does to a socket that already exists — leaves the read
    # blocked while the kernel still thinks the socket is open, for however
    # long tcp_retries2 takes (~15 min, unbounded if the peer keeps the window
    # alive). Nothing has failed, so nothing retries: the pass counter never
    # advances and the job looks hung rather than slow.
    stall = env_int("DCACHE_STALL_SECS", 60)
    try:
        resp = opener.open(req, timeout=stall)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            ra = retry_after_seconds(e.headers)
            raise RateLimited(
                f"{layer.title} was rate-limited by the registry (HTTP 429), "
                f"retry-after={ra if ra is not None else 'none'}", ra)
        if e.code == 416:
            # The server would not honour the range, so this file can never
            # complete. Drop it rather than loop on it.
            out.unlink(missing_ok=True)
            raise Interrupted(
                f"range request refused (HTTP {e.code}) — restarting "
                f"{layer.title} from zero next pass")
        raise Interrupted(f"{layer.title}: HTTP {e.code}")
    except OSError as e:
        raise Interrupted(f"{layer.title}: {e}")

    # A 200 to a Range request means the server ignored it and is sending the
    # whole blob from zero. Appending that to what we have would produce a
    # file of the right length and garbage content — caught by the digest, but
    # only after transferring it. Truncate and take the transfer as a restart.
    if have and resp.status == 200:
        log_(f"{layer.title}: server ignored the range, restarting from zero")
        out.unlink(missing_ok=True)
        have = 0

    every = env_int("DCACHE_PROGRESS_SECS", 30)
    min_bps = env_int("DCACHE_MIN_BPS", 102400)
    started = last_report = time.monotonic()
    got = 0
    try:
        with resp, out.open("ab") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                now = time.monotonic()
                if every and now - last_report >= every:
                    rate = got / max(now - started, 1e-9)
                    done = have + got
                    log_(f"{layer.title} {done}/{layer.size} bytes "
                         f"({done * 100 // layer.size}%, {rate / 1024:.0f} KB/s)")
                    last_report = now
                    # Falling under 100 KB/s for a sustained stretch is not a
                    # slow link — a healthy transfer here runs orders of
                    # magnitude above it — it is a dead one, and the right
                    # response is to drop the socket and resume, which now
                    # costs only the bytes in flight.
                    if min_bps and rate < min_bps and now - started >= stall:
                        raise Interrupted(
                            f"{layer.title} stalled below {min_bps} B/s")
    except OSError as e:
        raise Interrupted(f"{layer.title}: {e}")

    end = out.stat().st_size
    if end != layer.size:
        raise Interrupted(f"{layer.title} ended at {end} of {layer.size} bytes")
    if sha256_of(out) != layer.want:
        out.unlink()
        raise Corrupt(f"{layer.title} completed but its sha256 does not match "
                      f"{layer.digest} — discarded, not resumed")
    log_(f"{layer.title} complete ({layer.size} bytes)")


def sha256_of(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def backoff_wait(wait, retry_after=None, rng=random):
    """Seconds to wait before the next pass.

    The registry's Retry-After is a FLOOR, not a replacement: honouring a 5s
    ask from a limiter we have already annoyed five times running would put us
    straight back into it. The cap applies to both, so a registry cannot park
    a job for an hour by asking it to.

    Jitter is not decoration. Every VM in a fleet run boots within seconds of
    the others and hits the same limiter at the same moment, so a fixed
    schedule has them retrying in lockstep forever — each wave re-tripping the
    limit for the whole fleet. Spreading each wait breaks up the convoy.
    """
    cap = env_int("DCACHE_RETRY_MAX_SLEEP", 300)
    jitter = env_int("DCACHE_RETRY_JITTER", 25)
    if retry_after is not None:
        wait = max(wait, retry_after)
    wait = min(wait, cap)
    if wait > 0 and jitter > 0:
        wait = wait * rng.randint(100 - jitter, 100 + jitter) // 100
        wait = max(wait, 1)
    return wait


def fetch_layers(ref, dest, manifest, sleep=time.sleep, log_=log, opener=None):
    """Every layer of `ref` onto disk under `dest`, verified. True on success.

    Passes, not attempts: each pass walks the whole layer set, skips what
    already verifies, and resumes what does not. What makes this converge is
    that a pass keeps whatever it moved, so progress is monotone regardless of
    how blob size compares to the interruption period.
    """
    layers = parse_layers(manifest)
    if not layers:
        raise SystemExit(
            f"derive-cache: {ref} carries no file layers — nothing to pull. "
            "An artifact with no titled layer would extract to an empty "
            "directory and report a hit, which is the false-hit bug in a "
            "different costume.")

    # A tagged ref's repository is everything before the LAST colon; blobs are
    # addressed by digest against the repository, not the tag. Safe for
    # `localhost:5000/x:t` because the port's colon is not the last one.
    repo = ref.rsplit(":", 1)[0]
    registry, _, repo_path = repo.partition("/")
    base = f"{scheme(registry)}://{registry}/v2/{repo_path}/blobs/"
    opener = opener or urllib.request.build_opener(_DropAuthOnRedirect)

    passes = env_int("DCACHE_PULL_TRIES", 10)
    step = env_int("DCACHE_RETRY_SLEEP", 30)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    def on_disk():
        return sum((dest / l.title).stat().st_size
                   for l in layers if (dest / l.title).exists())

    mark, fruitless = on_disk(), 0
    for p in range(1, passes + 1):
        token = fetch_token(registry, repo_path)
        failure = None
        for layer in layers:
            out = dest / layer.title
            # Size first: it is free, and a transfer killed mid-write leaves a
            # short file, so the common case is rejected without hashing 9.6
            # GB. Then the digest — the ONLY check standing between a
            # half-written file left by a previous run and a benchmark image
            # built on corrupt data.
            if out.exists() and out.stat().st_size == layer.size:
                # Announced because it is SLOW and silent: hashing wikipedia's
                # twelve layers is 95 GB of sha256 per pass, minutes in which
                # the job looks every bit as hung as a blackholed socket does.
                log_(f"verifying {layer.title} ({layer.size} bytes) against its digest")
                if sha256_of(out) == layer.want:
                    log_(f"{layer.title} verified, already complete")
                    continue
            try:
                fetch_blob(opener, base + layer.digest, token, layer, out, log_)
            except (Interrupted, Corrupt) as e:
                log_(str(e))
                # Stop the pass rather than grinding through the rest:
                # whatever is killing transfers is usually still doing it a
                # second later, and the wait before the next pass is the point.
                failure = e
                break
        if failure is None:
            return True
        if p == passes:
            break

        # Backoff doubles only across passes that moved NO bytes. A pass that
        # made progress and then failed resets it, because standing off a
        # resource that is refusing us is not what is happening while bytes
        # are still landing — and penalising that would punish exactly the
        # incremental progress this loop exists to make. On the fleet run,
        # gitlab's part-00 landed at full speed on pass 5.
        now = on_disk()
        fruitless = 1 if now > mark else min(fruitless + 1, 16)
        mark = now
        ra = failure.retry_after if isinstance(failure, RateLimited) else None
        wait = backoff_wait(step * 2 ** (fruitless - 1), ra)
        log_(f"pass {p}/{passes} of {ref} was interrupted — retrying in {wait}s"
             + (f" (registry asked for {ra}s)" if ra is not None else "")
             + "; completed layers are skipped and a partial one resumes"
               " where it stopped")
        sleep(wait)
    return False


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: derive_cache_fetch.py REF DEST  (manifest on stdin)")
    ref, dest = argv
    return 0 if fetch_layers(ref, dest, sys.stdin.buffer.read()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
