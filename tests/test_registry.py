"""Reading a published image's build revision back out of a registry.

Served over real HTTP by `fake_registry`, for the same reason the transfer
tests are: the thing under test is what a registry actually answers, and a
mocked `urlopen` can only return what we already believed.
"""
import hashlib
import json
import urllib.error
import urllib.request

import pytest

from builder.registry import (REVISION_LABEL, Unavailable, published_revision,
                              split_ref)
from tests.fake_registry import FakeRegistry

SHA = "0123456789abcdef0123456789abcdef01234567"


def _blob(obj):
    return json.dumps(obj).encode()


def _config(labels):
    return _blob({"config": {"Labels": labels}})


def _image(config_bytes):
    """A single-platform manifest referencing `config_bytes`."""
    return _blob({
        "config": {
            "digest": "sha256:" + hashlib.sha256(config_bytes).hexdigest(),
            "size": len(config_bytes),
        },
        "layers": [],
    })


def _index(manifest_bytes, platform=None):
    entry = {"digest": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
             "size": len(manifest_bytes)}
    if platform:
        entry["platform"] = platform
    return _blob({"manifests": [entry]})


def test_split_ref_defaults_to_latest():
    assert split_ref("ghcr.io/shkolnik/miniwob-server") == (
        "ghcr.io", "shkolnik/miniwob-server", "latest")


def test_split_ref_reads_an_explicit_tag():
    assert split_ref("ghcr.io/shkolnik/miniwob-server:20260805.abc1234") == (
        "ghcr.io", "shkolnik/miniwob-server", "20260805.abc1234")


def test_split_ref_does_not_mistake_a_port_for_a_tag():
    # `localhost:5000/x` has a colon and no tag; splitting on the last colon
    # would name the repository "localhost" and the tag "5000/x".
    assert split_ref("localhost:5000/miniwob-server") == (
        "localhost:5000", "miniwob-server", "latest")


def test_reads_the_revision_label_off_a_published_image():
    cfg = _config({REVISION_LABEL: SHA})
    man = _image(cfg)
    with FakeRegistry({"cfg": cfg}, manifests={"latest": man}) as reg:
        assert published_revision(f"{reg.host}/{reg.repo}:latest") == SHA


def test_follows_an_index_to_the_amd64_manifest():
    other = _image(_config({REVISION_LABEL: "wrong"}))
    cfg = _config({REVISION_LABEL: SHA})
    man = _image(cfg)
    idx = _blob({"manifests": [
        {"digest": "sha256:" + hashlib.sha256(other).hexdigest(),
         "size": len(other),
         "platform": {"architecture": "arm64", "os": "linux"}},
        {"digest": "sha256:" + hashlib.sha256(man).hexdigest(),
         "size": len(man),
         "platform": {"architecture": "amd64", "os": "linux"}},
    ]})
    with FakeRegistry({"cfg": cfg, "man": man, "other": other},
                      manifests={"latest": idx}) as reg:
        assert published_revision(f"{reg.host}/{reg.repo}:latest") == SHA


def test_falls_back_to_the_only_entry_of_a_platformless_index():
    cfg = _config({REVISION_LABEL: SHA})
    man = _image(cfg)
    with FakeRegistry({"cfg": cfg, "man": man},
                      manifests={"latest": _index(man)}) as reg:
        assert published_revision(f"{reg.host}/{reg.repo}:latest") == SHA


def test_unpublished_image_is_none_not_an_error():
    # The 404 path is the common one on a new image, and it has to be an
    # ordinary answer: an exception here would fail discovery for exactly the
    # image that most needs building.
    with FakeRegistry({}, manifests={}) as reg:
        assert published_revision(f"{reg.host}/{reg.repo}:latest") is None


def test_image_published_without_the_label_is_none():
    cfg = _config({"org.opencontainers.image.source": "https://example/x"})
    man = _image(cfg)
    with FakeRegistry({"cfg": cfg}, manifests={"latest": man}) as reg:
        assert published_revision(f"{reg.host}/{reg.repo}:latest") is None


def test_unreachable_registry_raises_rather_than_reporting_unpublished():
    # An outage that read as "nothing is published" would rebuild the whole
    # fleet with no explanation. Not knowing has to be distinguishable from
    # knowing there is nothing there.
    with pytest.raises(Unavailable):
        published_revision("127.0.0.1:1/x/y:latest")


def test_missing_config_blob_is_unavailable_not_unpublished():
    # A manifest that resolves to a config the registry cannot serve is a
    # broken repository, not an unbuilt image.
    cfg = _config({REVISION_LABEL: SHA})
    with FakeRegistry({}, manifests={"latest": _image(cfg)}) as reg:
        with pytest.raises(Unavailable):
            published_revision(f"{reg.host}/{reg.repo}:latest")
