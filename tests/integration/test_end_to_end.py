import shutil
import subprocess
import hashlib
from pathlib import Path
import pytest
from builder.discover import ImageRef
from builder.manifest import Dataset, Manifest
from builder import docker as bdocker
from builder.download import ensure_dataset

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or subprocess.run(
        ["docker", "info"], capture_output=True).returncode != 0,
    reason="docker unavailable")

def test_download_build_push_roundtrip(tmp_path):
    payload = b"hello dataset\n"
    src = tmp_path / "srv"; src.mkdir(); (src / "d.bin").write_bytes(payload)
    ds = Dataset("d.bin", hashlib.sha256(payload).hexdigest(),
                 [f"file://{src}/d.bin"])
    dsdir = tmp_path / "datasets"
    # file:// fetch via curl exercises the real fetch path
    ensure_dataset(ds, dsdir)

    imgdir = tmp_path / "images" / "itest" / "svc"; imgdir.mkdir(parents=True)
    (imgdir / "Dockerfile").write_text(
        "FROM busybox\nCOPY --from=datasets d.bin /d.bin\nCMD cat /d.bin\n")
    (imgdir / "image.toml").write_text("")
    ref = ImageRef("itest", "svc", imgdir)

    reg = subprocess.run(["docker", "run", "-d", "-p", "127.0.0.1:5000:5000",
                          "registry:2"], capture_output=True, text=True, check=True).stdout.strip()
    try:
        version = "itest.1"
        bdocker.run(bdocker.build_cmd(ref, Manifest(), "127.0.0.1:5000", dsdir, version,
                                      Path(__file__).resolve().parents[2]))
        for cmd in bdocker.push_cmds(ref, "127.0.0.1:5000", version):
            bdocker.run(cmd)
        out = subprocess.run(
            ["docker", "run", "--rm", f"127.0.0.1:5000/itest-svc:{version}"],
            capture_output=True, text=True, check=True).stdout
        assert out == payload.decode()
    finally:
        subprocess.run(["docker", "rm", "-f", reg], capture_output=True)
        subprocess.run(["docker", "rmi", "-f",
                        f"127.0.0.1:5000/itest-svc:{version}",
                        "127.0.0.1:5000/itest-svc:latest"], capture_output=True)
