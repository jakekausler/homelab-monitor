"""Real drift-detection regressions for the auto-fix orchestrator.

Complements the storage-only ``runbook_hash`` capture tests elsewhere.
These tests INSTANTIATE the orchestrator, insert an approval via
``handle_alert``, mutate the runbook, then call ``execute_approved`` and
assert the drift denial.

Kept in a dedicated file so the drift regression is easy to find by name.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text

from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import DenialReason, RunOutcome
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.socket_client import ExecResult
from homelab_monitor.kernel.runbooks.repository import RunbookRepo
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository
from tests.test_autofix_orchestrator import (
    _FakeDockerClient,  # pyright: ignore[reportPrivateUsage]
    _insert_alert,  # pyright: ignore[reportPrivateUsage]
    _insert_runbook,  # pyright: ignore[reportPrivateUsage]
    _make_alert,  # pyright: ignore[reportPrivateUsage]
    _make_orchestrator,  # pyright: ignore[reportPrivateUsage]
    _make_runbook_record,  # pyright: ignore[reportPrivateUsage]
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def master_key_bytes() -> bytes:
    return bytes(range(32))


@pytest_asyncio.fixture
async def secrets_repo_fixture(
    repo: SqliteRepository, master_key_bytes: bytes
) -> AsyncSecretsRepository:
    return AsyncSecretsRepository(repo, master_key_bytes)


@pytest.mark.asyncio
async def test_drift_execute_approved_rejects_when_hash_changed(
    repo: SqliteRepository,
    secrets_repo_fixture: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Runbook content_hash mutated after dry-run → execute_approved returns
    DenialReason.RUNBOOK_CHANGED and does NOT exec.
    """
    rb = _make_runbook_record(
        alertname="TestAlert",
        dry_run_required=True,
        content_hash="hash-v1",
        runbook_dir=tmp_path / "runbook",
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(
        result=ExecResult(exit_code=0, stdout="plan", stderr=""),
        transcript_to_write=f"{transcript_dir}/dry-{uuid7()}.transcript",
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        dry_result = await orch.handle_alert(alert)
    assert dry_result is not None
    approval_id = dry_result.approval_id
    assert approval_id is not None

    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE runbooks SET content_hash = :hash WHERE id = :id"),
            {"id": rb.id, "hash": "hash-v2"},
        )

    docker.last_call_cmd = None

    result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result is not None
    assert result.ran is False
    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.RUNBOOK_CHANGED
    assert docker.last_call_cmd is None


@pytest.mark.asyncio
async def test_drift_execute_approved_rejects_when_runbook_missing(
    repo: SqliteRepository,
    secrets_repo_fixture: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Runbook deleted between dry-run and approve → execute_approved returns
    DenialReason.RUNBOOK_MISSING and does NOT exec.
    """
    rb = _make_runbook_record(
        alertname="TestAlert",
        dry_run_required=True,
        content_hash="hash-v1",
        runbook_dir=tmp_path / "runbook",
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(
        result=ExecResult(exit_code=0, stdout="plan", stderr=""),
        transcript_to_write=f"{transcript_dir}/dry-{uuid7()}.transcript",
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        dry_result = await orch.handle_alert(alert)
    assert dry_result is not None
    approval_id = dry_result.approval_id
    assert approval_id is not None

    docker.last_call_cmd = None

    with patch.object(RunbookRepo, "get_runbook", new=AsyncMock(return_value=None)):
        result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result.ran is False
    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.RUNBOOK_MISSING
    assert docker.last_call_cmd is None
