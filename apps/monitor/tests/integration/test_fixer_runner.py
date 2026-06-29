"""Integration test: the fixer-runner image (STAGE-009-003).

Builds the fixer-runner image with CLAUDE_BINARY_SOURCE=fake, starts the idle
keepalive container, and validates the auto-fix runtime contract:

  (a) The orchestrator exec shape (STAGE-009-005) works non-interactively:
        docker exec -i -u homelab-fixer -e ANTHROPIC_API_KEY=<...> \\
          <container> claude -p <runbook-folder> --dangerously-skip-permissions \\
          < /dev/null
      asserts the fake claude captured the expected argv, wrote a transcript into
      the RW-mounted dir, that transcript is owned by HM_FIXER_UID:HM_FIXER_GID
      (#3 identity), and ANTHROPIC_API_KEY is empty under fake (no real key).

  (b) The kill switch (#7): a long-running in-flight exec is terminated when the
      container is `docker kill`ed.

Driven via the docker CLI through subprocess (matches
test_vector_template_validate.py and the repo's docker usage). Rig-gated via
require_docker() so it SKIPS FAST when the docker daemon is unavailable.

Run via:
    make integration
    pytest -m integration apps/monitor/tests/integration/test_fixer_runner.py
"""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from .helpers.rig_health import require_docker

# Build context = the fixer-runner directory (repo root resolved from this file).
_FIXER_RUNNER_DIR = (
    Path(__file__).parent.parent.parent.parent.parent  # repo root
    / "deploy"
    / "compose"
    / "fixer-runner"
)

_FIXER_UID = 1002
_FIXER_GID = 1002
_RUNBOOK_FOLDER = "/data/runbook-transcripts/example-runbook"
_KILL_TIMEOUT_S = 30.0


def _docker(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Run a docker CLI command via subprocess, capturing output."""
    return subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def fixer_image() -> Iterator[str]:
    """Build the fixer-runner image once (fake claude) and remove it after.

    Module-scoped so the (fast, debian-slim) build runs ONCE for all tests.
    """
    require_docker()
    tag = f"homelab-monitor-fixer-runner-test:{uuid.uuid4().hex[:12]}"
    build = _docker(
        "build",
        "--build-arg",
        "CLAUDE_BINARY_SOURCE=fake",
        "--build-arg",
        f"FIXER_UID={_FIXER_UID}",
        "--build-arg",
        f"FIXER_GID={_FIXER_GID}",
        "-t",
        tag,
        str(_FIXER_RUNNER_DIR),
        timeout=300.0,
    )
    if build.returncode != 0:
        pytest.fail(f"fixer-runner image build failed:\n{build.stdout}\n{build.stderr}")
    try:
        yield tag
    finally:
        _docker("rmi", "-f", tag, timeout=60.0)


def _run_container(image: str, transcript_dir: Path) -> str:
    """Start the idle-keepalive container with the transcript dir bind-mounted RW.

    Returns the container name. Caller is responsible for teardown.
    """
    name = f"fixer-runner-test-{uuid.uuid4().hex[:12]}"
    run = _docker(
        "run",
        "-d",
        "--name",
        name,
        "-v",
        f"{transcript_dir}:/data/runbook-transcripts",
        image,
        timeout=60.0,
    )
    if run.returncode != 0:
        pytest.fail(f"fixer-runner container failed to start:\n{run.stdout}\n{run.stderr}")
    return name


@pytest.fixture
def running_container(fixer_image: str, tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Start a fresh keepalive container per test; force-remove it on teardown.

    Yields (container_name, host_transcript_dir). The transcript dir is a
    pytest tmp_path the test owns; the fake claude writes its .args/.transcript
    files there.
    """
    require_docker()
    transcript_dir = tmp_path / "runbook-transcripts"
    transcript_dir.mkdir()
    # World-writable so the in-container FIXER_UID (which need not exist on the
    # host) can write into the bind-mount regardless of host ownership.
    transcript_dir.chmod(0o777)
    name = _run_container(fixer_image, transcript_dir)
    try:
        yield name, transcript_dir
    finally:
        _docker("rm", "-f", name, timeout=60.0)


@pytest.mark.integration
def test_exec_shape_writes_owned_transcript_without_api_key(
    running_container: tuple[str, Path],
) -> None:
    """(a) The non-interactive exec shape captures argv, writes an owned transcript.

    Validates: argv passthrough (-p / folder / --dangerously-skip-permissions),
    a transcript file appears in the RW bind-mount, it is owned by
    HM_FIXER_UID:HM_FIXER_GID (#3 identity), and ANTHROPIC_API_KEY is empty
    under the fake source (no real key).
    """
    name, transcript_dir = running_container

    # The orchestrator (005) exec shape, stdin from /dev/null (sh -c so the
    # redirect happens inside the container; -e passes an EMPTY key like CI).
    exec_result = _docker(
        "exec",
        "-i",
        "-u",
        "homelab-fixer",
        "-e",
        "ANTHROPIC_API_KEY=",
        name,
        "sh",
        "-c",
        f"claude -p {_RUNBOOK_FOLDER} --dangerously-skip-permissions < /dev/null",
        timeout=60.0,
    )
    assert exec_result.returncode == 0, (
        f"claude exec failed: {exec_result.stdout}\n{exec_result.stderr}"
    )

    # The fake claude wrote <prefix>.args and <prefix>.transcript into the dir.
    args_files = list(transcript_dir.glob("fake-claude-*.args"))
    transcript_files = list(transcript_dir.glob("fake-claude-*.transcript"))
    assert len(args_files) == 1, f"expected exactly 1 .args file, got {args_files}"
    assert len(transcript_files) == 1, (
        f"expected exactly 1 .transcript file, got {transcript_files}"
    )

    # argv passthrough.
    argv_lines = args_files[0].read_text(encoding="utf-8").splitlines()
    assert "-p" in argv_lines
    assert _RUNBOOK_FOLDER in argv_lines
    assert "--dangerously-skip-permissions" in argv_lines

    # #3 identity: the transcript file is owned by HM_FIXER_UID:HM_FIXER_GID.
    stat = transcript_files[0].stat()
    assert stat.st_uid == _FIXER_UID, (
        f"transcript owned by uid {stat.st_uid}, expected {_FIXER_UID}"
    )
    assert stat.st_gid == _FIXER_GID, (
        f"transcript owned by gid {stat.st_gid}, expected {_FIXER_GID}"
    )

    # No real key under fake.
    transcript_text = transcript_files[0].read_text(encoding="utf-8")
    assert "anthropic_api_key_present=0" in transcript_text, (
        f"expected no API key under fake source; transcript:\n{transcript_text}"
    )


@pytest.mark.integration
def test_docker_kill_terminates_inflight_exec(
    running_container: tuple[str, Path],
) -> None:
    """(b) #7 kill switch: docker kill stops the container + a long-running exec."""
    name, _transcript_dir = running_container

    # Start a long-running exec (FAKE_CLAUDE_SLEEP=600) in the background. We use
    # Popen so we don't block; the container kill is what must terminate it.
    proc = subprocess.Popen(
        [
            "docker",
            "exec",
            "-i",
            "-u",
            "homelab-fixer",
            "-e",
            "FAKE_CLAUDE_SLEEP=600",
            name,
            "sh",
            "-c",
            f"claude -p {_RUNBOOK_FOLDER} --dangerously-skip-permissions < /dev/null",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Confirm the container is up and the exec is in flight.
        time.sleep(2.0)
        inspect = _docker("inspect", "-f", "{{.State.Running}}", name, timeout=15.0)
        assert inspect.returncode == 0 and inspect.stdout.strip() == "true", (
            f"container not running before kill: {inspect.stdout}\n{inspect.stderr}"
        )
        assert proc.poll() is None, "in-flight exec exited before kill (should be sleeping)"

        # #7: kill the container. This must terminate the container AND the
        # in-flight exec process.
        kill = _docker("kill", name, timeout=30.0)
        assert kill.returncode == 0, f"docker kill failed: {kill.stdout}\n{kill.stderr}"

        # The exec subprocess must return within the timeout (it cannot keep
        # sleeping once its container is gone).
        try:
            proc.wait(timeout=_KILL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pytest.fail("in-flight exec did not terminate after docker kill")

        # The container must no longer be running.
        deadline = time.time() + _KILL_TIMEOUT_S
        while time.time() < deadline:
            check = _docker("inspect", "-f", "{{.State.Running}}", name, timeout=15.0)
            # After kill the container exists but is not running; once removed,
            # inspect returns non-zero. Either way it must not be "true".
            if check.returncode != 0 or check.stdout.strip() != "true":
                break
            time.sleep(1.0)
        else:
            pytest.fail("container still running after docker kill + timeout")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10.0)
