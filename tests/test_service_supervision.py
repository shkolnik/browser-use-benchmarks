"""No image may reintroduce a restarting supervisor, or pipe a service.

The contract (#73) is that any service exiting takes the container with it. Two
ways to lose that silently, both of which are one careless edit away:

  * bringing back supervisord (or any config declaring `autorestart`), which
    restarts a crashed service forever and leaves the container "up" while it
    serves from a degraded state;
  * piping a service for log prefixing — `nginx | sed …` makes the shell's $!
    the LAST command of the pipeline, so the supervisor watches sed, reports
    sed's exit status, and the whole contract keeps passing while holding
    nothing.

Static checks over the real image scripts, not fixtures: the thing being
guarded is what this repo ships.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IMAGES = REPO / "images"

# gitlab is the deliberate exception: its services are supervised by runit,
# which comes from GitLab's own omnibus image. Replacing that means fighting
# Chef, so it implements the same contract with runit `finish` hooks instead.
RUNIT_IMAGES = {"webarena/gitlab"}


def _shell_scripts():
    return sorted(p for p in IMAGES.glob("*/*/*.sh"))


def test_there_are_scripts_to_check():
    assert len(_shell_scripts()) >= 6, "image script discovery found nothing"


def test_no_image_ships_a_supervisord_config():
    stray = [str(p.relative_to(REPO)) for p in IMAGES.rglob("supervisord.conf")]
    assert not stray, (
        f"supervisord config(s) came back: {stray}. Under supervisord a crashed "
        f"service is restarted forever (autorestart) or abandoned in FATAL while "
        f"supervisord keeps running — both leave a degraded container reporting "
        f"healthy, which is what #73 removed.")


def _code_lines(script):
    """Non-comment lines. Several scripts legitimately DISCUSS supervisord —
    reddit's derive-backup.sh records that upstream's Cmd was `supervisord -n`,
    which is provenance, not an invocation."""
    return [(n, ln) for n, ln in enumerate(script.read_text().splitlines(), 1)
            if ln.strip() and not ln.lstrip().startswith("#")]


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: str(p.relative_to(IMAGES)))
def test_no_script_starts_a_supervisor_daemon(script):
    for n, line in _code_lines(script):
        for banned in ("supervisord", "supervisorctl"):
            assert banned not in line, (
                f"{script.relative_to(REPO)}:{n} invokes {banned!r}; services are "
                f"started by run-services.sh (svc_start) so that a dead one fails "
                f"the container")


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: str(p.relative_to(IMAGES)))
def test_no_service_is_started_through_a_pipe(script):
    """`svc_start x -- cmd | tee log` would make the supervisor watch tee."""
    for n, line in enumerate(script.read_text().splitlines(), 1):
        if "svc_start" not in line or line.lstrip().startswith("#"):
            continue
        # A pipe inside quotes (nginx -g "daemon off;") is fine; a shell pipe is
        # not. Strip quoted spans before looking.
        bare = re.sub(r"'[^']*'|\"[^\"]*\"", "", line)
        assert "|" not in bare, (
            f"{script.relative_to(REPO)}:{n} pipes a service. $! becomes the "
            f"pipeline's last command, so the supervisor would watch that "
            f"instead of the service. Use `svc_start --log FILE`, a redirect.")


def _image_dirs_with_services():
    """Images whose entrypoint starts more than one thing."""
    out = []
    for entry in sorted(IMAGES.glob("*/*/entrypoint.sh")):
        rel = f"{entry.parent.parent.name}/{entry.parent.name}"
        text = entry.read_text()
        if "svc_start" in text or "runsvdir" in text or rel in RUNIT_IMAGES:
            out.append(pytest.param(entry, rel, id=rel))
    return out


MULTI = _image_dirs_with_services()


def test_the_multi_process_images_were_found():
    found = {rel for _, rel in (p.values for p in MULTI)}
    assert {"vwa/classifieds", "webarena/reddit", "webarena/shopping",
            "webarena/shopping-admin", "webarena/gitlab"} <= found, (
        f"expected every multi-process image to be discovered, got {found}")


@pytest.mark.parametrize("entry,rel", MULTI)
def test_every_multi_process_image_supervises_its_services(entry, rel):
    body = entry.read_text()
    if rel in RUNIT_IMAGES:
        # runit's equivalent: the finish hook installed for every service.
        assert "finish" in body or "install-finish" in body, (
            f"{rel} is runit-supervised but its entrypoint installs no finish "
            f"hook, so a crashed service would restart forever unnoticed")
        return
    assert ". /run-services.sh" in body, (
        f"{rel} starts services without sourcing the shared supervisor")
    assert "svc_supervise" in body, (
        f"{rel} sources the supervisor but never calls svc_supervise, so the "
        f"entrypoint would exit and take the container down immediately, or "
        f"worse, sit doing nothing while services die behind it")
