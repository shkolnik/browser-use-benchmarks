import hashlib
import subprocess
import time
from pathlib import Path
from builder.manifest import Dataset

class FetchError(Exception):
    pass

PROGRESS_INTERVAL_S = 30

def sha256_of(path: Path, log=None) -> str:
    """Hash the file, reporting every PROGRESS_INTERVAL_S seconds when given a log.

    Every pinned dataset is verified on every run, cache hit included — so a
    map build that downloads nothing still reads ~180G here, and it is the one
    long phase that cannot be skipped by having the data already. curl prints
    its own meter during a fetch, which is why the download beside this is
    visible and this was not.
    """
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    started = last = time.monotonic()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            done += len(chunk)
            now = time.monotonic()
            if log and now - last >= PROGRESS_INTERVAL_S:
                last = now
                log(f"{path.name}: verifying, {done / 2**30:.1f}G of "
                    f"{total / 2**30:.1f}G at "
                    f"{done / (now - started) / 2**20:.0f}MB/s")
    return h.hexdigest()

def fetch_curl(url: str, dest: Path) -> None:
    # -C - resumes a partial dest from a previous attempt; --fail turns HTTP
    # errors into exit codes instead of saving an error page as the dataset.
    proc = subprocess.run(
        ["curl", "--fail", "--location", "--continue-at", "-",
         "--connect-timeout", "30", "--output", str(dest), url])
    if proc.returncode != 0:
        raise FetchError(f"curl exited {proc.returncode} for {url}")

def default_datasets_dir(repo_root: Path) -> Path:
    return repo_root / "datasets"

def _quarantine(path: Path, target_name: str, log) -> Path:
    """Rename path aside as <target_name>.quarantine, replacing any prior quarantine file."""
    q = path.with_name(target_name + ".quarantine")
    path.replace(q)
    log(f"quarantined bad file to {q}")
    return q

def ensure_dataset(ds: Dataset, datasets_dir: Path, fetch=fetch_curl,
                   attempts_per_url: int = 3, log=print) -> Path:
    datasets_dir.mkdir(parents=True, exist_ok=True)
    final = datasets_dir / ds.filename
    if final.exists():
        if sha256_of(final, log) == ds.sha256:
            log(f"{ds.filename}: cached and verified, skipping download")
            return final
        log(f"{ds.filename}: cached copy fails verification")
        _quarantine(final, ds.filename, log)

    part = datasets_dir / (ds.filename + ".part")
    errors = []
    for url in ds.urls:
        for attempt in range(1, attempts_per_url + 1):
            try:
                log(f"{ds.filename}: fetching {url} (attempt {attempt}/{attempts_per_url})")
                fetch(url, part)
            except FetchError as e:
                errors.append(f"{url}: {e}")
                continue

            if not part.exists() or sha256_of(part, log) != ds.sha256:
                size = part.stat().st_size if part.exists() else 0
                if part.exists():
                    _quarantine(part, ds.filename, log)
                raise SystemExit(
                    f"error: sha256 mismatch for {ds.filename} from {url} "
                    f"({size} bytes) — quarantined, not passed to docker")

            size = part.stat().st_size
            part.replace(final)
            log(f"{ds.filename}: verified ({size} bytes)")
            return final

    raise SystemExit(
        f"error: all mirrors exhausted for {ds.filename}: " + "; ".join(errors))

def run_download(refs, datasets_dir: Path, fetch=fetch_curl,
                 prepare_inputs: bool = False) -> None:
    """Fetch declared datasets.

    Default: everything EXCEPT prepare_input datasets — those are fetched
    on demand by the prepare script, and only when its derived-inputs cache
    misses. prepare_inputs=True selects exactly the complement, which is how
    a derive script asks for its own upstream tar.
    """
    from builder.manifest import load_manifest
    for ref in refs:
        for ds in load_manifest(ref.path).datasets:
            if ds.prepare_input != prepare_inputs:
                continue
            ensure_dataset(ds, datasets_dir, fetch=fetch)
