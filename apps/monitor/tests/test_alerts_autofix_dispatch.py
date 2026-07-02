"""Unit tests for the _dispatch_autofix seam helper (alerts.py lines 147-165).

Covers four branches:
  1. alert is None → early return (line 157)
  2. orchestrator is not an AutoFixOrchestrator → isinstance False → exit branch (line 162)
  3. exception inside try → swallowed + log.exception called (line 165)
  4. orchestrator IS an AutoFixOrchestrator → handle_alert awaited (line 163, isinstance True)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import Alert, AlertStatus, Severity
from homelab_monitor.kernel.api.routers.alerts import (
    _dispatch_autofix,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.autofix.approvals_repository import RunbookRunApprovalsRepository
from homelab_monitor.kernel.autofix.orchestrator import AutoFixOrchestrator
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.config import FixerRunnerConfig
from homelab_monitor.kernel.db.repositories.app_settings_repository import AppSettingsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.runbooks.repository import RunbookRepo
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository


def _make_alert() -> Alert:
    """Return a minimal valid Alert instance for test use."""
    return Alert(
        id="test-alert-id-001",
        fingerprint="abc123",
        source_tool="alertmanager",
        severity=Severity.WARNING,
        status=AlertStatus.FIRING,
        opened_at="2026-06-29T00:00:00Z",
        last_seen_at="2026-06-29T00:00:00Z",
        resolved_at=None,
        ack_at=None,
        ack_by=None,
        runbook_id=None,
        payload={"labels": {"alertname": "TestAlert", "severity": "warning"}, "annotations": {}},
        labels={"alertname": "TestAlert", "severity": "warning"},
        annotations={},
    )


def _make_alert_repo(*, returns: Alert | None = None, raises: Exception | None = None) -> MagicMock:
    """Return a stub AlertRepository whose get_alert_by_id is controlled."""
    repo = MagicMock()
    if raises is not None:
        exc = raises
        repo.get_alert_by_id = AsyncMock(side_effect=exc)
    else:
        repo.get_alert_by_id = AsyncMock(return_value=returns)
    return repo


def _make_log() -> MagicMock:
    """Return a structlog-style stub logger."""
    log = MagicMock()
    log.exception = MagicMock()
    return log


@pytest.mark.asyncio
async def test_dispatch_autofix_alert_not_found_returns_silently() -> None:
    """Line 157: when get_alert_by_id returns None, function exits without calling handle_alert."""
    orchestrator = MagicMock()  # NOT an AutoFixOrchestrator — safe either way
    alert_repo = _make_alert_repo(returns=None)
    log = _make_log()

    # Must not raise
    await _dispatch_autofix(orchestrator, alert_repo, "missing-id", log)

    alert_repo.get_alert_by_id.assert_awaited_once_with("missing-id")
    # handle_alert must NOT have been called (orchestrator is a MagicMock)
    orchestrator.handle_alert.assert_not_called()
    log.exception.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_autofix_non_orchestrator_does_not_call_handle_alert() -> None:
    """Line 162 exit: isinstance check is False → handle_alert not called, no exception."""
    # plain object() — definitely not an AutoFixOrchestrator
    orchestrator = object()
    alert = _make_alert()
    alert_repo = _make_alert_repo(returns=alert)
    log = _make_log()

    # Must not raise
    await _dispatch_autofix(orchestrator, alert_repo, alert.id, log)

    alert_repo.get_alert_by_id.assert_awaited_once_with(alert.id)
    log.exception.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_autofix_exception_swallowed_and_logged() -> None:
    """Line 165: exception in get_alert_by_id is swallowed; log.exception called with event key."""
    boom = RuntimeError("db is on fire")
    alert_repo = _make_alert_repo(raises=boom)
    orchestrator = object()
    log = _make_log()

    # Must NOT propagate — that is the contract of this helper
    await _dispatch_autofix(orchestrator, alert_repo, "any-id", log)

    log.exception.assert_called_once_with("autofix_dispatch_failed", alert_id="any-id")


@pytest.mark.asyncio
async def test_dispatch_autofix_real_orchestrator_handle_alert_called(
    repo: SqliteRepository,
) -> None:
    """Line 163: isinstance(orchestrator, AutoFixOrchestrator) is True → handle_alert awaited.

    Uses a REAL AutoFixOrchestrator so isinstance passes.  handle_alert is replaced
    with an AsyncMock so the test stays a unit test (no docker, no DB exec needed).
    """
    master_key = bytes(range(32))
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    config = FixerRunnerConfig(
        container="test-fixer",
        transcript_dir="/tmp/transcripts-dispatch-test",
        exec_log_dir="/tmp/exec-logs-dispatch-test",
        fixer_user="homelab-fixer",
        exec_timeout_seconds=60.0,
    )
    real_orch = AutoFixOrchestrator(
        runbook_repo=RunbookRepo(repo),
        alert_repo=AlertRepository(repo),
        app_settings_repo=AppSettingsRepository(repo),
        secrets_repo=secrets_repo,
        docker_client=MagicMock(),  # type: ignore[arg-type]
        db=repo,
        runs_repo=RunbookRunsRepository(repo),
        approvals_repo=RunbookRunApprovalsRepository(repo),
        config=config,
        log=structlog.get_logger(),
    )

    # Replace handle_alert with AsyncMock — instance is still a real AutoFixOrchestrator,
    # so isinstance check on line 162 is True and line 163 executes.
    handle_mock = AsyncMock(return_value=None)
    real_orch.handle_alert = handle_mock  # type: ignore[method-assign]

    alert = _make_alert()
    alert_repo = _make_alert_repo(returns=alert)
    log = _make_log()

    await _dispatch_autofix(real_orch, alert_repo, alert.id, log)

    handle_mock.assert_awaited_once_with(alert)
    log.exception.assert_not_called()
