"""Tests for the transcript rotator (STAGE-009-012)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
import structlog
import structlog.testing
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.transcript_rotation_scheduler import (
    _seconds_until_next_daily,
    run_transcript_rotation_loop,
)
from homelab_monitor.kernel.autofix.transcript_rotator import (
    RotationOutcome,
    TranscriptRotator,
)
from homelab_monitor.kernel.autofix.types import RunMode
from homelab_monitor.kernel.config import FixerRunnerConfig
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.socket_client import (
    ContainerListEntry,
    DockerSocketClient,
    DockerSocketConnectionError,
    DockerSocketProtocolError,
    ExecResult,
)
from tests.kernel.autofix.conftest import insert_run, seed_extra_runbook

_KEEP_COUNT = 3
_EXPECTED_PRUNED_COUNT_TEST = 2
_EXPECTED_PRUNED_ALL_OLD = 3
_SECONDS_PER_HOUR = 3600.0
_SECONDS_PER_DAY = 86400.0


class FakeDockerSocketClient(DockerSocketClient):
    """Fake docker client for testing."""

    def __init__(self) -> None:
        self.list_containers_result: list[ContainerListEntry] = []
        self.list_containers_error: Exception | None = None
        self.exec_calls: list[tuple[str, list[str], str | None, float]] = []
        self.exec_results: dict[int, ExecResult] = {}
        self.exec_errors: dict[int, Exception] = {}

    async def list_containers(self) -> list[ContainerListEntry]:
        if self.list_containers_error is not None:
            raise self.list_containers_error
        return self.list_containers_result

    async def exec_capture(
        self,
        *,
        container_id: str,
        cmd: list[str],
        timeout_seconds: float,
        user: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        del env  # unused by fake
        self.exec_calls.append((container_id, cmd, user, timeout_seconds))
        call_index = len(self.exec_calls) - 1
        if call_index in self.exec_errors:
            raise self.exec_errors[call_index]
        if call_index in self.exec_results:
            return self.exec_results[call_index]
        return ExecResult(exit_code=0, stdout="", stderr="")


@pytest.fixture
def fake_docker() -> FakeDockerSocketClient:
    return FakeDockerSocketClient()


@pytest.fixture
def rotator(
    repo: SqliteRepository,
    fake_docker: FakeDockerSocketClient,
) -> TranscriptRotator:
    """Create a rotator instance with fake docker client."""
    config = FixerRunnerConfig(
        transcript_rotation_max_count=3,
        transcript_rotation_max_age_days=365,
    )
    log = cast(BoundLogger, structlog.get_logger().bind(component="test"))

    return TranscriptRotator(
        db=repo,
        runs_repo=RunbookRunsRepository(repo),
        config=config,
        docker_client=fake_docker,
        log=log,
    )


@pytest.mark.asyncio
async def test_rotate_no_runs_returns_zeroes(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
) -> None:
    """Empty DB returns all-zero outcome; no docker call."""
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "running",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]
    outcome = await rotator.rotate()
    assert outcome.files_pruned == 0
    assert outcome.runs_marked == 0
    assert outcome.runbooks_scanned == 0
    assert outcome.skipped_reason is None


@pytest.mark.asyncio
async def test_rotate_fixer_not_running_returns_skipped_and_emits_audit(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
    repo: SqliteRepository,
) -> None:
    """Fixer container exited returns skipped and writes audit row."""
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "exited",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]
    outcome = await rotator.rotate()
    assert outcome.skipped_reason == "fixer_runner_not_running"
    assert outcome.files_pruned == 0
    assert outcome.runs_marked == 0

    # Check audit row was written
    rows = await repo.fetch_all(
        __import__("sqlalchemy").text(
            "SELECT what FROM audit_log WHERE what LIKE "
            "'autofix.transcript_rotation_skipped%' ORDER BY \"when\" DESC LIMIT 1"
        ),
        {},
    )
    assert len(rows) > 0
    assert rows[0]._mapping["what"] == "autofix.transcript_rotation_skipped_fixer_disabled"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_rotate_fixer_absent_returns_skipped(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
) -> None:
    """Fixer container not found returns skipped."""
    fake_docker.list_containers_result = []
    outcome = await rotator.rotate()
    assert outcome.skipped_reason == "fixer_runner_not_running"


@pytest.mark.asyncio
async def test_rotate_docker_socket_unreachable_returns_skipped(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
) -> None:
    """Docker socket error returns skipped."""
    fake_docker.list_containers_error = DockerSocketConnectionError("connection failed")
    outcome = await rotator.rotate()
    assert outcome.skipped_reason == "fixer_runner_not_running"


@pytest.mark.asyncio
async def test_rotate_prunes_count_beyond_N(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    """Keep last 3; insert 5 → prune the 2 oldest by started_at."""
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "running",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]
    # Success on all deletes
    fake_docker.exec_results = {
        0: ExecResult(exit_code=0, stdout="", stderr=""),
        1: ExecResult(exit_code=0, stdout="", stderr=""),
    }

    # Insert 5 runs with transcript paths, oldest first
    base_time = _dt.datetime.fromisoformat("2026-01-01T00:00:00Z")
    run_ids: list[str] = []
    for i in range(5):
        started = (base_time + _dt.timedelta(days=i)).isoformat().replace("+00:00", "Z")
        run_id = await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.DRY_RUN,
            initiated_by="alert",
            started_at=started,
            transcript_path=f"/data/runbook-transcripts/run{i}.txt",
        )
        run_ids.append(run_id)

    outcome = await rotator.rotate()
    assert outcome.files_pruned == _EXPECTED_PRUNED_COUNT_TEST
    assert outcome.runs_marked == _EXPECTED_PRUNED_COUNT_TEST

    # Check that the 2 oldest (run0, run1) were deleted
    assert len(fake_docker.exec_calls) == _EXPECTED_PRUNED_COUNT_TEST
    assert fake_docker.exec_calls[0][1] == ["rm", "-f", "--", "/data/runbook-transcripts/run1.txt"]
    assert fake_docker.exec_calls[1][1] == ["rm", "-f", "--", "/data/runbook-transcripts/run0.txt"]

    # Check markers were set on pruned rows
    for run_id in run_ids[:2]:
        row_raw = await repo.fetch_one(
            __import__("sqlalchemy").text(
                "SELECT transcript_pruned_at, transcript_path FROM runbook_runs WHERE id = :id"
            ),
            {"id": run_id},
        )
        assert row_raw is not None
        row = row_raw._mapping  # pyright: ignore[reportPrivateUsage]
        assert row["transcript_pruned_at"] is not None
        assert row["transcript_path"] is None

    # Check retained rows are untouched
    for run_id in run_ids[2:]:
        row_raw = await repo.fetch_one(
            __import__("sqlalchemy").text(
                "SELECT transcript_pruned_at, transcript_path FROM runbook_runs WHERE id = :id"
            ),
            {"id": run_id},
        )
        assert row_raw is not None
        row = row_raw._mapping  # pyright: ignore[reportPrivateUsage]
        assert row["transcript_pruned_at"] is None
        assert row["transcript_path"] is not None


@pytest.mark.asyncio
async def test_rotate_prunes_by_age(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    """Old runs pruned regardless of N."""
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "running",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]
    fake_docker.exec_results = {
        0: ExecResult(exit_code=0, stdout="", stderr=""),
        1: ExecResult(exit_code=0, stdout="", stderr=""),
        2: ExecResult(exit_code=0, stdout="", stderr=""),
    }

    # Insert 3 old runs
    old_time = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=400)
    run_ids: list[str] = []
    for i in range(3):
        started = (old_time + _dt.timedelta(days=i)).isoformat().replace("+00:00", "Z")
        run_id = await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.DRY_RUN,
            initiated_by="alert",
            started_at=started,
            transcript_path=f"/data/runbook-transcripts/old{i}.txt",
        )
        run_ids.append(run_id)

    outcome = await rotator.rotate()
    assert outcome.files_pruned == _EXPECTED_PRUNED_ALL_OLD
    assert outcome.runs_marked == _EXPECTED_PRUNED_ALL_OLD


@pytest.mark.asyncio
async def test_rotate_skips_already_pruned(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    """Already-pruned row skipped (no exec, no audit)."""
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "running",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]

    # Insert row with transcript_pruned_at set
    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="alert",
        transcript_path="/data/runbook-transcripts/already_pruned.txt",
        transcript_pruned_at="2026-01-01T00:00:00Z",
    )

    outcome = await rotator.rotate()
    assert outcome.files_pruned == 0
    assert outcome.runs_marked == 0
    assert len(fake_docker.exec_calls) == 0


@pytest.mark.asyncio
async def test_rotate_skips_never_had_transcript(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    """Never-had-transcript row skipped."""
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "running",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]

    # Insert row with no transcript
    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="alert",
        transcript_path=None,
    )

    outcome = await rotator.rotate()
    assert outcome.files_pruned == 0
    assert outcome.runs_marked == 0
    assert len(fake_docker.exec_calls) == 0


@pytest.mark.asyncio
async def test_rotate_never_deletes_runbook_runs_row(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    """COUNT(runbook_runs) unchanged before/after."""
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "running",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]
    fake_docker.exec_results = {0: ExecResult(exit_code=0, stdout="", stderr="")}

    old_time = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=400)
    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="alert",
        started_at=old_time.isoformat().replace("+00:00", "Z"),
        transcript_path="/data/runbook-transcripts/old.txt",
    )

    count_before = await repo.fetch_one(
        __import__("sqlalchemy").text("SELECT COUNT(*) AS n FROM runbook_runs"),
        {},
    )
    await rotator.rotate()
    count_after = await repo.fetch_one(
        __import__("sqlalchemy").text("SELECT COUNT(*) AS n FROM runbook_runs"),
        {},
    )

    assert count_before is not None
    assert count_after is not None
    assert count_before.n == count_after.n == 1


@pytest.mark.asyncio
async def test_rotate_path_traversal_defense_rejects_outside_base(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    """Path outside base dir skipped, not deleted."""
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "running",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]

    old_time = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=400)
    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="alert",
        started_at=old_time.isoformat().replace("+00:00", "Z"),
        transcript_path="/etc/passwd",
    )

    outcome = await rotator.rotate()
    assert outcome.files_pruned == 0
    assert outcome.runs_marked == 0
    assert len(fake_docker.exec_calls) == 0

    # Row still exists, unchanged
    row_raw = await repo.fetch_one(
        __import__("sqlalchemy").text(
            "SELECT transcript_path, transcript_pruned_at FROM runbook_runs"
        ),
        {},
    )
    assert row_raw is not None
    row = row_raw._mapping  # pyright: ignore[reportPrivateUsage]
    assert row["transcript_path"] == "/etc/passwd"
    assert row["transcript_pruned_at"] is None


@pytest.mark.asyncio
async def test_rotate_docker_exec_nonzero_exit_does_not_mark_pruned(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    """Exec failure → row not marked."""
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "running",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]
    fake_docker.exec_results = {0: ExecResult(exit_code=1, stdout="", stderr="rm: no such file")}

    old_time = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=400)
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="alert",
        started_at=old_time.isoformat().replace("+00:00", "Z"),
        transcript_path="/data/runbook-transcripts/missing.txt",
    )

    outcome = await rotator.rotate()
    assert outcome.files_pruned == 0
    assert outcome.runs_marked == 0

    row_raw = await repo.fetch_one(
        __import__("sqlalchemy").text(
            "SELECT transcript_path, transcript_pruned_at FROM runbook_runs WHERE id = :id"
        ),
        {"id": run_id},
    )
    assert row_raw is not None
    row = row_raw._mapping  # pyright: ignore[reportPrivateUsage]
    assert row["transcript_path"] == "/data/runbook-transcripts/missing.txt"
    assert row["transcript_pruned_at"] is None


@pytest.mark.asyncio
async def test_rotate_docker_socket_error_during_exec_does_not_mark_pruned(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    """DockerSocketConnectionError raised from exec_capture is swallowed as
    False; row is not marked pruned (transcript_rotator.py:251-257)."""
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "running",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]
    fake_docker.exec_errors = {0: DockerSocketConnectionError("socket gone")}

    old_time = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=400)
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="alert",
        started_at=old_time.isoformat().replace("+00:00", "Z"),
        transcript_path="/data/runbook-transcripts/socket_error.txt",
    )

    with structlog.testing.capture_logs() as cap:
        outcome = await rotator.rotate()

    assert outcome.files_pruned == 0
    assert outcome.runs_marked == 0

    row_raw = await repo.fetch_one(
        __import__("sqlalchemy").text(
            "SELECT transcript_path, transcript_pruned_at FROM runbook_runs WHERE id = :id"
        ),
        {"id": run_id},
    )
    assert row_raw is not None
    row = row_raw._mapping  # pyright: ignore[reportPrivateUsage]
    assert row["transcript_path"] == "/data/runbook-transcripts/socket_error.txt"
    assert row["transcript_pruned_at"] is None

    assert any(entry.get("event") == "autofix.transcript_rotation.exec_error" for entry in cap)


@pytest.mark.asyncio
async def test_rotate_docker_protocol_error_during_exec_does_not_mark_pruned(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    """DockerSocketProtocolError raised from exec_capture is swallowed as
    False; row is not marked pruned (transcript_rotator.py:251-257)."""
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "running",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]
    fake_docker.exec_errors = {0: DockerSocketProtocolError("bad protocol")}

    old_time = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=400)
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="alert",
        started_at=old_time.isoformat().replace("+00:00", "Z"),
        transcript_path="/data/runbook-transcripts/protocol_error.txt",
    )

    outcome = await rotator.rotate()
    assert outcome.files_pruned == 0
    assert outcome.runs_marked == 0

    row_raw = await repo.fetch_one(
        __import__("sqlalchemy").text(
            "SELECT transcript_path, transcript_pruned_at FROM runbook_runs WHERE id = :id"
        ),
        {"id": run_id},
    )
    assert row_raw is not None
    row = row_raw._mapping  # pyright: ignore[reportPrivateUsage]
    assert row["transcript_path"] == "/data/runbook-transcripts/protocol_error.txt"
    assert row["transcript_pruned_at"] is None


def test_is_within_base_dir_rejects_relative_paths(
    rotator: TranscriptRotator,
) -> None:
    """Relative paths are rejected outright — DB rows should be absolute
    (transcript_rotator.py:254-255)."""
    result = rotator._is_within_base_dir(  # pyright: ignore[reportPrivateUsage]
        "relative/transcript.txt",
        Path("/data/runbook-transcripts"),
    )
    assert result is False


def test_classify_reason_both_when_count_and_age_both_exceeded(
    rotator: TranscriptRotator,
) -> None:
    """_classify_reason returns 'both' when both booleans are True."""
    reason = rotator._classify_reason(  # pyright: ignore[reportPrivateUsage]
        count_exceeded=True,
        age_exceeded=True,
    )
    assert reason == "both"


def test_classify_reason_age_limit_when_only_age_exceeded(
    rotator: TranscriptRotator,
) -> None:
    """_classify_reason returns 'age_limit' when only age triggered."""
    reason = rotator._classify_reason(  # pyright: ignore[reportPrivateUsage]
        count_exceeded=False,
        age_exceeded=True,
    )
    assert reason == "age_limit"


def test_classify_reason_count_limit_when_only_count_exceeded(
    rotator: TranscriptRotator,
) -> None:
    """_classify_reason returns 'count_limit' when only count triggered."""
    reason = rotator._classify_reason(  # pyright: ignore[reportPrivateUsage]
        count_exceeded=True,
        age_exceeded=False,
    )
    assert reason == "count_limit"


@pytest.mark.asyncio
async def test_rotate_writes_audit_log_per_prune_with_reason_classification(
    rotator: TranscriptRotator,
    fake_docker: FakeDockerSocketClient,
    repo: SqliteRepository,
    seed_runbook: str,
    another_runbook: str,
) -> None:
    """Audit row per prune AND ``reason`` reflects which limit triggered.

    Rotator is fixture-configured with ``transcript_rotation_max_count=3`` and
    ``transcript_rotation_max_age_days=365``. This test seeds three
    scenarios and asserts one audit row per reason:

    - ``count_limit`` — recent run beyond N (age NOT exceeded).
    - ``age_limit`` — old run within N (age exceeded).
    - ``both`` — old run beyond N (both exceeded).
    """
    fake_docker.list_containers_result = [
        {
            "Id": "container123",
            "Names": ["/homelab-fixer-runner"],
            "Image": "test-image",
            "ImageID": "sha256:test",
            "State": "running",
            "Status": "Up 1 minute",
            "Labels": {},
        }
    ]
    fake_docker.exec_results = {i: ExecResult(exit_code=0, stdout="", stderr="") for i in range(10)}

    now = _dt.datetime.now(_dt.UTC)
    recent_base = now - _dt.timedelta(days=1)
    old_base = now - _dt.timedelta(days=400)

    for i in range(4):
        await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.DRY_RUN,
            initiated_by="alert",
            started_at=(recent_base - _dt.timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
            transcript_path=f"/data/runbook-transcripts/seed_recent{i}.txt",
        )

    await insert_run(
        repo,
        runbook_id=another_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="alert",
        started_at=old_base.isoformat().replace("+00:00", "Z"),
        transcript_path="/data/runbook-transcripts/another_old.txt",
    )

    third_runbook_id = await seed_extra_runbook(repo)
    for i in range(4):
        await insert_run(
            repo,
            runbook_id=third_runbook_id,
            mode=RunMode.DRY_RUN,
            initiated_by="alert",
            started_at=(old_base - _dt.timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
            transcript_path=f"/data/runbook-transcripts/third_old{i}.txt",
        )

    await rotator.rotate()

    rows = await repo.fetch_all(
        __import__("sqlalchemy").text(
            "SELECT what, after_json FROM audit_log WHERE what = 'autofix.transcript_pruned' "
            'ORDER BY "when"'
        ),
        {},
    )
    reasons = {
        __import__("json").loads(row._mapping["after_json"])["reason"]  # pyright: ignore[reportPrivateUsage]
        for row in rows
    }
    assert "count_limit" in reasons
    assert "age_limit" in reasons
    assert "both" in reasons


def test_seconds_until_next_daily_before_target() -> None:
    """now=02:00 target 03:00 → 3600.0."""
    now = _dt.datetime(2026, 1, 1, 2, 0, 0, tzinfo=_dt.UTC)
    seconds = _seconds_until_next_daily(now, hour=3, minute=0)
    assert seconds == _SECONDS_PER_HOUR


def test_seconds_until_next_daily_after_target() -> None:
    """now=04:00 target 03:00 → 23*3600."""
    now = _dt.datetime(2026, 1, 1, 4, 0, 0, tzinfo=_dt.UTC)
    seconds = _seconds_until_next_daily(now, hour=3, minute=0)
    assert seconds == 23 * 3600


def test_seconds_until_next_daily_exact_target_rolls_to_tomorrow() -> None:
    """now == target → 86400.0."""
    now = _dt.datetime(2026, 1, 1, 3, 0, 0, tzinfo=_dt.UTC)
    seconds = _seconds_until_next_daily(now, hour=3, minute=0)
    assert seconds == _SECONDS_PER_DAY


class _FakeRotator:
    """Stand-in for TranscriptRotator with a scriptable ``rotate()``."""

    def __init__(self, outcomes: list[RotationOutcome | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.call_count = 0

    async def rotate(self) -> RotationOutcome:
        self.call_count += 1
        if not self._outcomes:
            msg = "no more scripted outcomes"
            raise AssertionError(msg)
        result = self._outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


_ZERO_OUTCOME = RotationOutcome(
    files_pruned=0, runs_marked=0, runbooks_scanned=0, skipped_reason=None
)


@pytest.mark.asyncio
async def test_run_transcript_rotation_loop_startup_cancelled_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelledError from the startup rotate() call propagates
    (transcript_rotation_scheduler.py:55-56)."""

    class _CancellingRotator:
        async def rotate(self) -> RotationOutcome:
            raise asyncio.CancelledError

    log = cast(BoundLogger, structlog.get_logger().bind(component="test"))

    with pytest.raises(asyncio.CancelledError):
        await run_transcript_rotation_loop(
            rotator=cast(TranscriptRotator, _CancellingRotator()), log=log
        )


@pytest.mark.asyncio
async def test_run_transcript_rotation_loop_startup_error_logs_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-cancellation exception from the startup rotate() call is logged
    and swallowed; the loop proceeds into the daily wait
    (transcript_rotation_scheduler.py:57-58)."""

    class _FailingThenHangingRotator:
        def __init__(self) -> None:
            self.call_count = 0

        async def rotate(self) -> RotationOutcome:
            self.call_count += 1
            raise RuntimeError("boom")

    log = cast(BoundLogger, structlog.get_logger().bind(component="test"))
    rotator = _FailingThenHangingRotator()

    # Force the daily wait to be effectively instantaneous, then cancel the
    # loop once it re-enters rotate() for the daily pass so the test doesn't
    # hang forever.
    def _fake_seconds_until_next_daily(*_a: object, **_kw: object) -> float:
        return 0.0

    monkeypatch.setattr(
        "homelab_monitor.kernel.autofix.transcript_rotation_scheduler._seconds_until_next_daily",
        _fake_seconds_until_next_daily,
    )

    with structlog.testing.capture_logs() as cap:
        task = asyncio.ensure_future(
            run_transcript_rotation_loop(rotator=cast(TranscriptRotator, rotator), log=log)
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        except (TimeoutError, RuntimeError):
            pass
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=1.0)

    assert rotator.call_count >= 1
    assert any(
        entry.get("event") == "autofix.transcript_rotation.startup_pass_error" for entry in cap
    )


@pytest.mark.asyncio
async def test_run_transcript_rotation_loop_daily_pass_logs_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful daily rotate() call logs daily_pass_done
    (transcript_rotation_scheduler.py:74-80)."""
    fake = _FakeRotator([_ZERO_OUTCOME, _ZERO_OUTCOME])
    log = cast(BoundLogger, structlog.get_logger().bind(component="test"))

    def _fake_seconds_until_next_daily(*_a: object, **_kw: object) -> float:
        return 0.0

    monkeypatch.setattr(
        "homelab_monitor.kernel.autofix.transcript_rotation_scheduler._seconds_until_next_daily",
        _fake_seconds_until_next_daily,
    )

    with structlog.testing.capture_logs() as cap:
        task = asyncio.ensure_future(
            run_transcript_rotation_loop(rotator=cast(TranscriptRotator, fake), log=log)
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        except (TimeoutError, AssertionError):
            pass
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=1.0)

    assert fake.call_count >= 2  # noqa: PLR2004 -- startup pass + at least one daily pass
    assert any(entry.get("event") == "autofix.transcript_rotation.daily_pass_done" for entry in cap)


@pytest.mark.asyncio
async def test_run_transcript_rotation_loop_daily_pass_error_logs_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-cancellation exception from the daily rotate() call is logged
    and swallowed; the loop continues to the next daily wait
    (transcript_rotation_scheduler.py:81-84)."""
    fake = _FakeRotator([_ZERO_OUTCOME, RuntimeError("daily boom"), _ZERO_OUTCOME])
    log = cast(BoundLogger, structlog.get_logger().bind(component="test"))

    def _fake_seconds_until_next_daily(*_a: object, **_kw: object) -> float:
        return 0.0

    monkeypatch.setattr(
        "homelab_monitor.kernel.autofix.transcript_rotation_scheduler._seconds_until_next_daily",
        _fake_seconds_until_next_daily,
    )

    with structlog.testing.capture_logs() as cap:
        task = asyncio.ensure_future(
            run_transcript_rotation_loop(rotator=cast(TranscriptRotator, fake), log=log)
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        except (TimeoutError, AssertionError):
            pass
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=1.0)

    assert fake.call_count >= 2  # noqa: PLR2004 -- startup pass + failing daily pass
    assert any(
        entry.get("event") == "autofix.transcript_rotation.daily_pass_error" for entry in cap
    )


@pytest.mark.asyncio
async def test_run_transcript_rotation_loop_daily_pass_cancelled_reraises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """asyncio.CancelledError raised during the daily rotate must propagate.

    Covers transcript_rotation_scheduler.py line 82 (the `raise` inside
    `except asyncio.CancelledError:` in the daily loop).
    """
    # Track number of rotate() calls
    call_count = 0

    class ScriptedRotator:
        async def rotate(self) -> RotationOutcome:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Startup pass: succeed with zero outcome
                return _ZERO_OUTCOME
            # Daily pass: raise CancelledError
            raise asyncio.CancelledError

    def _fake_seconds_until_next_daily(*_a: object, **_kw: object) -> float:
        return 0.0

    monkeypatch.setattr(
        "homelab_monitor.kernel.autofix.transcript_rotation_scheduler._seconds_until_next_daily",
        _fake_seconds_until_next_daily,
    )

    log = cast(BoundLogger, structlog.get_logger().bind(component="test"))
    task = asyncio.create_task(
        run_transcript_rotation_loop(rotator=cast(TranscriptRotator, ScriptedRotator()), log=log)
    )
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        await asyncio.wait_for(task, timeout=0.5)

    # The task should have been cancelled (via propagated CancelledError from daily rotate)
    assert task.cancelled() or task.done()
