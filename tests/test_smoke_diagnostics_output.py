"""A failed smoke must leave READABLE evidence, not just run the right commands.

The existing diagnostics tests assert on the commands the dump path invokes.
That is what let #76 through: on the gitlab failures every command ran, and
the CI log still showed `=== docker compose logs ... ===` and `=== listeners
inside the container ===` with nothing under either. Proving a probe RUNS is
not proving its output arrives. These tests assert on what reaches the log.

Two distinct causes, both pinned here:

* Displacement. Python block-buffers stdout when it is a pipe (CI always is)
  while a child writing to the inherited fd is not buffered at all, so headers
  printed before a child appear in the log AFTER its output. Reproduced
  directly, 2026-08-08.
* Silence. A probe that produced nothing looked identical to one that could
  not run — `ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null` prints nothing
  at all in an image carrying neither tool, which reads as "nothing is
  listening": the opposite of the truth.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

from builder import docker as docker_mod

REPO = Path(__file__).resolve().parent.parent


class _Result:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def test_a_section_carries_its_header_then_its_content():
    # Captured rather than inherited, so the section's content is ours to place
    # immediately under its own header — no flush discipline required.
    out = []
    docker_mod._dump(["docker", "true"], "a section", log=out.append,
                     runner=lambda cmd, **kw: _Result(stdout="nginx: bind() failed\n"))
    assert out[0] == "=== a section ==="
    assert "nginx: bind() failed" in out[1], (
        "the probe's output must reach the log, not just its exit code")


def test_an_empty_section_says_why_it_is_empty():
    out = []
    docker_mod._dump(["docker", "compose", "exec", "gitlab", "ss"],
                     "listeners inside the container",
                     log=out.append,
                     runner=lambda cmd, **kw: _Result(returncode=127))
    body = "\n".join(out)
    assert "no output" in body and "127" in body, (
        "an empty diagnostic section must carry the exit code — silence and "
        f"failure are otherwise indistinguishable. Got: {body!r}")


def test_stderr_is_folded_in_rather_than_discarded():
    out = []
    docker_mod._dump(["docker", "compose", "exec", "gitlab", "ss"], "listeners",
                     log=out.append,
                     runner=lambda cmd, **kw: _Result(
                         stderr="Error: No such container\n", returncode=1))
    assert "No such container" in "\n".join(out), (
        "the reason a probe failed is on stderr; dropping it is how the gitlab "
        "failures became undiagnosable")


def test_a_probe_that_raises_still_does_not_replace_the_real_failure():
    out = []
    docker_mod._dump(["docker", "x"], "listeners", log=out.append,
                     runner=lambda cmd, **kw: (_ for _ in ()).throw(OSError("gone")))
    assert any("could not collect" in line for line in out)


def test_listener_probe_reports_an_image_with_neither_tool():
    """The fallback must SAY so instead of printing nothing."""
    calls = []
    docker_mod.dump_service_diagnostics(
        Path("/x/compose.yml"), "gitlab",
        runner=lambda cmd, **kw: calls.append(cmd) or _Result())
    exec_cmd = " ".join(next(c for c in calls if "exec" in c))
    assert "neither ss nor netstat" in exec_cmd, (
        "an image with no ss and no netstat must produce a sentence, not an "
        "empty section that reads as 'nothing is listening'")
    assert "2>/dev/null" not in exec_cmd, (
        "discarding the probes' stderr is what made the empty section mute")


def test_bin_build_line_buffers_stdout_so_child_output_stays_in_order(tmp_path):
    """The root cause, reproduced end to end against the real entry point.

    Without `sys.stdout.reconfigure(line_buffering=True)` in bin/build, a
    header printed before a subprocess lands in the log AFTER that
    subprocess's output whenever stdout is a pipe. This runs a script that
    imports bin/build's preamble the same way the CLI does, through a pipe,
    and checks the interleaving.
    """
    script = tmp_path / "prog.py"
    script.write_text(textwrap.dedent(f"""
        import subprocess, sys
        sys.argv = ["build"]
        # Execute bin/build's preamble, stopping before it dispatches to the
        # CLI — we want its buffering setup, applied exactly as it ships.
        src = open({str(REPO / 'bin' / 'build')!r}).read()
        src = src.split("from builder.cli import main")[0]
        exec(compile(src, "bin/build", "exec"),
             {{"__name__": "__main__", "__file__": {str(REPO / 'bin' / 'build')!r}}})
        print("=== header ===")
        subprocess.run(["echo", "CONTENT"])
    """))
    out = subprocess.run([sys.executable, str(script)],
                         capture_output=True, text=True, check=True).stdout
    assert out.index("=== header ===") < out.index("CONTENT"), (
        "bin/build must line-buffer stdout or every diagnostic header in CI "
        f"appears detached from its content. Got:\n{out}")
