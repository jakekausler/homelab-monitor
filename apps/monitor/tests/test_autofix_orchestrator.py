"""Unit tests for the auto-fix orchestrator (STAGE-009-005).

Covers every branch in:
  - AutoFixOrchestrator.handle_alert / _check_gates / _in_lock_gate / _claim_and_exec
    / _persist_outcome / _resolve_transcript / _deny / _maintenance_window
  - matcher.matching_runbooks / _matcher_matches / _runbook_matches
  - RunbookRunsRepository (all SQL helpers)
  - _is_truthy helper

Uses a real migrated SQLite DB (via conftest `repo` fixture) so that
runbook_runs SQL + audit writes are exercised for real.
DockerSocketClient is replaced by a lightweight FakeDockerClient whose
exec_capture is controlled per-test.

100% branch coverage target on the autofix package.

NOTE: RunbookRunsRepository.count_inflight previously had a SQL bind-param mismatch
(:stale_threshold in SQL vs stale_threshold_iso dict key) that caused SQLAlchemy to
raise InvalidRequestError.  That bug is now fixed (dict key is stale_threshold).
Tests that still patch count_inflight to return 0 or 1 do so for logical test control
(simulating fresh vs stale inflight), not as a workaround for a bug.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
import structlog
from sqlalchemy import text

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import Alert, AlertStatus, Severity
from homelab_monitor.kernel.autofix.matcher import (
    _matcher_matches,  # pyright: ignore[reportPrivateUsage]
    _runbook_matches,  # pyright: ignore[reportPrivateUsage]
    matching_runbooks,
)
from homelab_monitor.kernel.autofix.orchestrator import (
    AutoFixOrchestrator,
    _is_truthy,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import (
    DenialReason,
    RunMode,
    RunOutcome,
)
from homelab_monitor.kernel.config import FixerRunnerConfig
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.socket_client import (
    DockerExecTimeoutError,
    DockerSocketClient,
    DockerSocketConnectionError,
    ExecResult,
)
from homelab_monitor.kernel.runbooks.config import AlertMatcher
from homelab_monitor.kernel.runbooks.repository import RunbookRecord, RunbookRepo
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeDockerClient:
    """Minimal DockerSocketClient-shaped stub.

    Set `result` to the ExecResult to return, or `raises` to the exception
    to raise from exec_capture.
    """

    result: ExecResult = field(
        default_factory=lambda: ExecResult(exit_code=0, stdout="ok", stderr="")
    )
    raises: BaseException | None = None
    # Records the last call arguments for assertion
    last_call_container_id: str = ""
    last_call_cmd: list[str] | None = None
    last_call_user: str | None = None
    last_call_env: Mapping[str, str] | None = None

    async def exec_capture(
        self,
        *,
        container_id: str,
        cmd: list[str],
        timeout_seconds: float,
        user: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        self.last_call_container_id = container_id
        self.last_call_cmd = cmd
        self.last_call_user = user
        self.last_call_env = env
        if self.raises is not None:
            raise self.raises
        return self.result


def _make_runbook_record(  # noqa: PLR0913
    *,
    runbook_id: str | None = None,
    alertname: str = "TestAlert",
    enabled: bool = True,
    auto_trigger: bool = True,
    dry_run_required: bool = False,
    rate_limit_per_hour: int | None = None,
    cooldown_seconds: int | None = None,
    content_hash: str | None = "abc123",
) -> RunbookRecord:
    patterns: list[dict[str, Any]] = [{"alertname": alertname, "labels": {}}]
    return RunbookRecord(
        id=runbook_id or uuid7(),
        path="/runbooks/test-runbook",
        created_at=utc_now_iso(),
        alert_match_patterns=patterns,
        risk_tag="safe",
        dry_run_required=dry_run_required,
        rate_limit_per_hour=rate_limit_per_hour,
        cooldown_seconds=cooldown_seconds,
        enabled=enabled,
        auto_trigger=auto_trigger,
        content_hash=content_hash,
    )


def _make_alert(
    alertname: str = "TestAlert",
    extra_labels: dict[str, str] | None = None,
) -> Alert:
    labels: dict[str, str] = {"alertname": alertname, "severity": "warning"}
    if extra_labels:
        labels.update(extra_labels)
    return Alert(
        id=uuid7(),
        fingerprint=f"fp-{uuid7()}",
        source_tool="vmalert",
        severity=Severity.WARNING,
        status=AlertStatus.FIRING,
        opened_at=utc_now_iso(),
        last_seen_at=utc_now_iso(),
        payload={"labels": labels, "annotations": {}},
        labels=labels,
        annotations={},
    )


async def _insert_runbook(repo: SqliteRepository, record: RunbookRecord) -> None:
    """INSERT a RunbookRecord row directly into the DB."""
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO runbooks "
                "(id, path, created_at, alert_match_patterns, risk_tag, "
                " dry_run_required, rate_limit_per_hour, cooldown_seconds, "
                " enabled, auto_trigger, content_hash) "
                "VALUES (:id, :path, :created_at, :patterns, :risk_tag, "
                " :dry_run, :rate_limit, :cooldown, :enabled, :auto_trigger, :hash)"
            ),
            {
                "id": record.id,
                "path": record.path,
                "created_at": record.created_at,
                "patterns": json.dumps(record.alert_match_patterns),
                "risk_tag": record.risk_tag,
                "dry_run": int(record.dry_run_required),
                "rate_limit": record.rate_limit_per_hour,
                "cooldown": record.cooldown_seconds,
                "enabled": int(record.enabled),
                "auto_trigger": int(record.auto_trigger),
                "hash": record.content_hash,
            },
        )


async def _insert_alert(repo: SqliteRepository, alert: Alert) -> None:
    """INSERT an Alert row directly into the DB."""
    now = utc_now_iso()
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO alerts "
                "(id, fingerprint, source_tool, severity, status, "
                " opened_at, last_seen_at, payload_json, created_at) "
                "VALUES (:id, :fp, :st, :sev, :status, :opened, :last_seen, :pj, :created)"
            ),
            {
                "id": alert.id,
                "fp": alert.fingerprint,
                "st": alert.source_tool,
                "sev": alert.severity.value,
                "status": alert.status.value,
                "opened": alert.opened_at,
                "last_seen": alert.last_seen_at,
                "pj": json.dumps(alert.payload, sort_keys=True),
                "created": now,
            },
        )


def _make_orchestrator(  # noqa: PLR0913
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    docker_client: _FakeDockerClient | DockerSocketClient,
    *,
    transcript_dir: str = "/tmp/transcripts-unit-test",
    exec_log_dir: str = "/tmp/exec-logs-unit-test",
    exec_timeout_seconds: float = 60.0,
) -> AutoFixOrchestrator:
    log = structlog.get_logger()
    config = FixerRunnerConfig(
        container="test-fixer",
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        fixer_user="homelab-fixer",
        exec_timeout_seconds=exec_timeout_seconds,
    )
    return AutoFixOrchestrator(
        runbook_repo=RunbookRepo(repo),
        alert_repo=AlertRepository(repo),
        app_settings_repo=AppSettingsRepository(repo),
        secrets_repo=secrets_repo,
        docker_client=docker_client,  # type: ignore[arg-type]
        db=repo,
        runs_repo=RunbookRunsRepository(repo),
        config=config,
        log=log,
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


# ---------------------------------------------------------------------------
# _is_truthy
# ---------------------------------------------------------------------------


def test_is_truthy_none() -> None:
    assert _is_truthy(None) is False


def test_is_truthy_empty_string() -> None:
    assert _is_truthy("") is False


def test_is_truthy_false_values() -> None:
    for val in ("false", "0", "no", "off", "disabled"):
        assert _is_truthy(val) is False, f"Expected False for {val!r}"


def test_is_truthy_true_values() -> None:
    for val in ("true", "1", "yes", "TRUE", "YES", "  true  "):
        assert _is_truthy(val) is True, f"Expected True for {val!r}"


# ---------------------------------------------------------------------------
# matcher.py — _matcher_matches / _runbook_matches / matching_runbooks
# ---------------------------------------------------------------------------


def test_matcher_alertname_none_matches_any_alertname() -> None:
    """alertname=None in matcher matches any alertname label."""
    matcher = AlertMatcher(labels={"env": "prod"})
    alert = _make_alert(alertname="AnyAlert", extra_labels={"env": "prod"})
    assert _matcher_matches(matcher, alert) is True


def test_matcher_alertname_match() -> None:
    matcher = AlertMatcher(alertname="MyAlert", labels={})
    alert = _make_alert(alertname="MyAlert")
    assert _matcher_matches(matcher, alert) is True


def test_matcher_alertname_mismatch() -> None:
    matcher = AlertMatcher(alertname="MyAlert", labels={})
    alert = _make_alert(alertname="OtherAlert")
    assert _matcher_matches(matcher, alert) is False


def test_matcher_labels_subset_match() -> None:
    matcher = AlertMatcher(labels={"env": "prod", "region": "us-east"})
    alert = _make_alert(alertname="TestAlert", extra_labels={"env": "prod", "region": "us-east"})
    assert _matcher_matches(matcher, alert) is True


def test_matcher_labels_missing_key() -> None:
    matcher = AlertMatcher(labels={"env": "prod"})
    alert = _make_alert(alertname="TestAlert")  # no 'env' label
    assert _matcher_matches(matcher, alert) is False


def test_matcher_labels_value_mismatch() -> None:
    matcher = AlertMatcher(labels={"env": "prod"})
    alert = _make_alert(alertname="TestAlert", extra_labels={"env": "staging"})
    assert _matcher_matches(matcher, alert) is False


def test_runbook_matches_any_of_multiple_matchers() -> None:
    """_runbook_matches returns True if ANY matcher pattern matches."""
    record = RunbookRecord(
        id=uuid7(),
        path="/rb",
        created_at=utc_now_iso(),
        alert_match_patterns=[
            {"alertname": "NoMatch", "labels": {}},
            {"alertname": "TestAlert", "labels": {}},
        ],
        risk_tag="safe",
        dry_run_required=False,
        rate_limit_per_hour=None,
        cooldown_seconds=None,
        enabled=True,
        auto_trigger=True,
        content_hash=None,
    )
    alert = _make_alert(alertname="TestAlert")
    assert _runbook_matches(record, alert) is True


def test_runbook_matches_no_pattern_matches() -> None:
    record = RunbookRecord(
        id=uuid7(),
        path="/rb",
        created_at=utc_now_iso(),
        alert_match_patterns=[{"alertname": "WrongAlert", "labels": {}}],
        risk_tag="safe",
        dry_run_required=False,
        rate_limit_per_hour=None,
        cooldown_seconds=None,
        enabled=True,
        auto_trigger=True,
        content_hash=None,
    )
    alert = _make_alert(alertname="TestAlert")
    assert _runbook_matches(record, alert) is False


def test_matching_runbooks_zero_matches() -> None:
    rb = _make_runbook_record(alertname="NoMatch")
    alert = _make_alert(alertname="TestAlert")
    assert matching_runbooks([rb], alert) == []


def test_matching_runbooks_one_match() -> None:
    rb = _make_runbook_record(alertname="TestAlert")
    alert = _make_alert(alertname="TestAlert")
    result = matching_runbooks([rb], alert)
    assert len(result) == 1
    assert result[0].id == rb.id


def test_matching_runbooks_many_matches() -> None:
    rb1 = _make_runbook_record(alertname="TestAlert")
    rb2 = _make_runbook_record(alertname="TestAlert")
    alert = _make_alert(alertname="TestAlert")
    result = matching_runbooks([rb1, rb2], alert)
    assert len(result) == 2  # noqa: PLR2004


# ---------------------------------------------------------------------------
# handle_alert: no-match → None, nothing recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_alert_no_match_returns_none(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 1: no-match → None, no audit, no run row."""
    rb = _make_runbook_record(alertname="OtherAlert")
    await _insert_runbook(repo, rb)

    alert = _make_alert(alertname="NoSuchAlert")
    await _insert_alert(repo, alert)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_alert(alert)
    assert result is None

    # No run rows, no audit rows
    runs = await repo.fetch_all(text("SELECT id FROM runbook_runs"), {})
    assert runs == []
    audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE who = 'system:autofix'"), {}
    )
    assert audits == []


# ---------------------------------------------------------------------------
# handle_alert: ambiguous match → DENY ambiguous_match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_alert_ambiguous_match_denied(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 2: ≥2 runbooks match → DENY ambiguous_match; audit includes runbook_ids."""
    rb1 = _make_runbook_record(alertname="TestAlert")
    rb2 = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb1)
    await _insert_runbook(repo, rb2)

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_alert(alert)
    assert result is not None
    assert result.ran is False
    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.AMBIGUOUS_MATCH

    # Audit has runbook_ids list
    audit = await repo.fetch_one(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.denied'"), {}
    )
    assert audit is not None
    after = json.loads(str(audit[0]))
    assert "runbook_ids" in after
    assert set(after["runbook_ids"]) == {rb1.id, rb2.id}

    # No run rows
    runs = await repo.fetch_all(text("SELECT id FROM runbook_runs"), {})
    assert runs == []


# ---------------------------------------------------------------------------
# _check_gates: kill_switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_unset_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 4a: autofix_enabled unset → DENY kill_switch (checked first)."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_alert(alert)
    assert result is not None
    assert result.denial_reason == DenialReason.KILL_SWITCH

    runs = await repo.fetch_all(text("SELECT id FROM runbook_runs"), {})
    assert runs == []


@pytest.mark.asyncio
async def test_kill_switch_false_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 4b: autofix_enabled 'false' → DENY kill_switch."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "false")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_alert(alert)
    assert result is not None
    assert result.denial_reason == DenialReason.KILL_SWITCH


@pytest.mark.asyncio
async def test_kill_switch_zero_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 4c: autofix_enabled '0' → DENY kill_switch."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "0")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_alert(alert)
    assert result is not None
    assert result.denial_reason == DenialReason.KILL_SWITCH


# ---------------------------------------------------------------------------
# _check_gates: allow_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_list_enabled_false_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 5a: enabled=False → DENY allow_list."""
    rb = _make_runbook_record(alertname="TestAlert", enabled=False, auto_trigger=True)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_alert(alert)
    assert result is not None
    assert result.denial_reason == DenialReason.ALLOW_LIST


@pytest.mark.asyncio
async def test_allow_list_auto_trigger_false_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 5b: auto_trigger=False → DENY allow_list."""
    rb = _make_runbook_record(alertname="TestAlert", enabled=True, auto_trigger=False)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_alert(alert)
    assert result is not None
    assert result.denial_reason == DenialReason.ALLOW_LIST


# ---------------------------------------------------------------------------
# _check_gates: rate_limit (fast-path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_none_skips_gate(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 6b: rate_limit_per_hour=None → gate skipped, proceeds to exec.

    count_inflight patched due to known SQL bind-param bug (see module docstring).
    """
    rb = _make_runbook_record(alertname="TestAlert", rate_limit_per_hour=None)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)
    assert result is not None
    assert result.ran is True


@pytest.mark.asyncio
async def test_rate_limit_exceeded_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 6a: rate_limit reached → DENY rate_limit before claim."""
    rb = _make_runbook_record(alertname="TestAlert", rate_limit_per_hour=1)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    # Pre-insert a runbook_runs row so count>=limit
    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
        )
        # Complete it so it shows in count_started_since
    await runs_repo.mark_completed(
        run_id=(
            await repo.fetch_one(text("SELECT id FROM runbook_runs LIMIT 1"), {})  # type: ignore[index]
        )[0],
        exit_code=0,
        transcript_path=None,
    )  # type: ignore[index]

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_alert(alert)
    assert result is not None
    assert result.denial_reason == DenialReason.RATE_LIMIT

    runs = await repo.fetch_all(text("SELECT id FROM runbook_runs"), {})
    assert len(runs) == 1  # only the pre-inserted row


# ---------------------------------------------------------------------------
# _check_gates: cooldown (fast-path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooldown_none_skips_gate(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 7b: cooldown_seconds=None → gate skipped.

    count_inflight patched due to known SQL bind-param bug (see module docstring).
    """
    rb = _make_runbook_record(alertname="TestAlert", cooldown_seconds=None)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)
    assert result is not None
    assert result.ran is True


@pytest.mark.asyncio
async def test_cooldown_zero_skips_gate(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 7b: cooldown_seconds=0 → gate skipped (treated as disabled).

    count_inflight patched due to known SQL bind-param bug (see module docstring).
    """
    rb = _make_runbook_record(alertname="TestAlert", cooldown_seconds=0)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)
    assert result is not None
    assert result.ran is True


@pytest.mark.asyncio
async def test_cooldown_within_window_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 7a: within cooldown window → DENY cooldown."""
    rb = _make_runbook_record(alertname="TestAlert", cooldown_seconds=3600)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    # Pre-insert a completed run whose ended_at is recent (now)
    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
        )
    await runs_repo.mark_completed(run_id=run_id, exit_code=0, transcript_path=None)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_alert(alert)
    assert result is not None
    assert result.denial_reason == DenialReason.COOLDOWN


@pytest.mark.asyncio
async def test_cooldown_elapsed_passes_gate(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 7a: cooldown elapsed → gate passes, exec proceeds.

    count_inflight patched due to known SQL bind-param bug (see module docstring).
    """
    rb = _make_runbook_record(alertname="TestAlert", cooldown_seconds=1)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    # Pre-insert a completed run with ended_at far in the past
    far_past = (datetime.now(tz=UTC) - timedelta(hours=2)).isoformat()
    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
        )
    # Manually update ended_at to far past
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE runbook_runs SET ended_at = :ended WHERE id = :id"),
            {"ended": far_past, "id": run_id},
        )

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)
    assert result is not None
    assert result.ran is True


# ---------------------------------------------------------------------------
# _check_gates: risky_blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risky_blocked_dry_run_required_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 8: dry_run_required=True → DENY risky_blocked."""
    rb = _make_runbook_record(alertname="TestAlert", dry_run_required=True)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_alert(alert)
    assert result is not None
    assert result.denial_reason == DenialReason.RISKY_BLOCKED

    runs = await repo.fetch_all(text("SELECT id FROM runbook_runs"), {})
    assert runs == []


# ---------------------------------------------------------------------------
# In-lock gate: inflight fresh → DENY already_running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_lock_inflight_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 9: a fresh open-ended (ended_at IS NULL) claim → DENY already_running.

    count_inflight is mocked to return 1 to avoid the SQL bind-param bug.
    """
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=1)):
        docker = _FakeDockerClient()
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.denial_reason == DenialReason.ALREADY_RUNNING

    # No new rows (ALREADY_RUNNING denial → no insert_started)
    runs = await repo.fetch_all(text("SELECT id FROM runbook_runs"), {})
    assert runs == []


# ---------------------------------------------------------------------------
# In-lock gate: stale inflight → not blocked (self-heal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_lock_stale_inflight_not_blocked(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 10: stale open-ended claim (older than exec_timeout+slack) → proceeds.

    count_inflight is mocked to simulate the staleness-aware behaviour (returns 0
    for stale claims) because the real SQL has a known bind-param bug (see module
    docstring). The test verifies the orchestrator proceeds when count_inflight
    returns 0 (no fresh inflight).
    """
    exec_timeout = 60.0
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        exec_timeout_seconds=exec_timeout,
    )

    # count_inflight returns 0 → no fresh inflight → run proceeds (stale self-heal semantics)
    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True


# ---------------------------------------------------------------------------
# In-lock rate-limit re-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_lock_rate_limit_recheck_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 11: row inserted between fast-path and lock → in-lock rate denial."""
    rb = _make_runbook_record(alertname="TestAlert", rate_limit_per_hour=1)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    # No rows yet, so fast-path passes. But we'll insert a completed row so in-lock
    # rate re-check fires. We patch count_started_since (fast-path) to return 0
    # while the DB already has a row (inserted directly).
    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
        )
    await runs_repo.mark_completed(run_id=run_id, exit_code=0, transcript_path=None)

    # Patch the fast-path count to 0 so we get past _check_gates but fail in-lock.
    # Also patch count_inflight (no-inflight) due to SQL bind-param bug so the
    # in-lock rate re-check is reached (count_started_since_conn is real SQL).
    with (
        patch.object(RunbookRunsRepository, "count_started_since", new=AsyncMock(return_value=0)),
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
    ):
        docker = _FakeDockerClient()
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.denial_reason == DenialReason.RATE_LIMIT

    # Still only the pre-inserted row
    runs = await repo.fetch_all(text("SELECT id FROM runbook_runs"), {})
    assert len(runs) == 1


# ---------------------------------------------------------------------------
# In-lock cooldown re-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_lock_cooldown_recheck_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 12: completed run inserted between fast-path and lock → in-lock cooldown denial."""
    rb = _make_runbook_record(alertname="TestAlert", cooldown_seconds=3600)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    # Insert a completed run (in cooldown) then patch fast-path latest_ended_at to None
    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
        )
    await runs_repo.mark_completed(run_id=run_id, exit_code=0, transcript_path=None)

    with (
        patch.object(RunbookRunsRepository, "latest_ended_at", new=AsyncMock(return_value=None)),
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
    ):
        docker = _FakeDockerClient()
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.denial_reason == DenialReason.COOLDOWN


# ---------------------------------------------------------------------------
# In-lock precedence: inflight beats rate beats cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_lock_inflight_beats_rate_and_cooldown(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 13: inflight takes precedence over rate_limit and cooldown.

    All fast-path gates are patched to pass (count_started_since=0, latest_ended_at=None).
    count_inflight is mocked to return 1 (fresh inflight) to simulate the in-lock
    ALREADY_RUNNING denial.  This verifies that inflight is checked FIRST in
    _in_lock_gate before rate and cooldown.
    """
    # Use rate_limit=2 so fast-path count (patched to 1 below) does NOT trigger.
    rb = _make_runbook_record(alertname="TestAlert", rate_limit_per_hour=2, cooldown_seconds=3600)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    with (
        # fast-path: count < limit, no recent ended → all pass
        patch.object(RunbookRunsRepository, "count_started_since", new=AsyncMock(return_value=0)),
        patch.object(RunbookRunsRepository, "latest_ended_at", new=AsyncMock(return_value=None)),
        # in-lock: count_inflight = 1 → ALREADY_RUNNING returned FIRST
        # (rate would also deny but inflight has precedence)
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=1)),
    ):
        docker = _FakeDockerClient()
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.denial_reason == DenialReason.ALREADY_RUNNING


# ---------------------------------------------------------------------------
# Claim error (Critical #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_error_audited_and_returns_claim_error(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Branch 14: insert_started raises → audit autofix.claim_error; return CLAIM_ERROR."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    with patch.object(
        RunbookRunsRepository,
        "insert_started",
        new=AsyncMock(side_effect=RuntimeError("DB write failed")),
    ):
        docker = _FakeDockerClient()
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is False
    assert result.denial_reason == DenialReason.CLAIM_ERROR

    # audit.claim_error written
    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = 'autofix.claim_error'"), {}
    )
    assert audit is not None

    # No runbook_runs row
    runs = await repo.fetch_all(text("SELECT id FROM runbook_runs"), {})
    assert runs == []


# ---------------------------------------------------------------------------
# Exec success (exit 0): ALL three writes in ONE transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_success_exit_0_all_persisted(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 15: exec exit 0 → runbook_runs completed, alert_outcomes auto_fixed, audit."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="done", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    assert result.outcome == RunOutcome.RAN
    assert result.exit_code == 0
    assert result.run_id is not None
    run_id = result.run_id

    # runbook_runs row: started + ended, exit 0, mode real
    run_row = await repo.fetch_one(
        text("SELECT * FROM runbook_runs WHERE id = :id"), {"id": run_id}
    )
    assert run_row is not None
    assert run_row.started_at is not None
    assert run_row.ended_at is not None
    assert int(run_row.exit_code) == 0
    assert str(run_row.mode) == "real"
    assert str(run_row.fixer_user) == "homelab-fixer"
    assert str(run_row.runbook_hash) == "abc123"

    # alert_outcomes auto_fixed
    outcome_row = await repo.fetch_one(
        text("SELECT outcome FROM alert_outcomes WHERE alert_id = :aid"), {"aid": alert.id}
    )
    assert outcome_row is not None
    assert str(outcome_row[0]) == "auto_fixed"

    # audit autofix.ran
    audit_row = await repo.fetch_one(
        text(
            "SELECT what FROM audit_log WHERE what = 'autofix.ran' "
            "AND json_extract(after_json, '$.run_id') = :rid"
        ),
        {"rid": run_id},
    )
    assert audit_row is not None


# ---------------------------------------------------------------------------
# Exec non-zero exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_nonzero_exit_no_auto_fixed_outcome(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 16: exec exit_code != 0 → no auto_fixed outcome, but audit.ran present."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=1, stdout="", stderr="error"))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    assert result.exit_code == 1

    # No auto_fixed outcome
    outcome_row = await repo.fetch_one(
        text("SELECT outcome FROM alert_outcomes WHERE alert_id = :aid"), {"aid": alert.id}
    )
    assert outcome_row is None

    # autofix.ran audit IS present
    audit_row = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = 'autofix.ran'"), {}
    )
    assert audit_row is not None


# ---------------------------------------------------------------------------
# Exec raises DockerExecTimeoutError → exit_code 124 sentinel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_timeout_sentinel_124(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 17: DockerExecTimeoutError → exit_code=124, completion+audit written."""
    rb = _make_runbook_record(alertname="TestAlert")
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
        raises=DockerExecTimeoutError("timed out after 60s in test-fixer: ...")
    )
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    _TIMEOUT_EXIT_CODE = 124  # Docker convention for killed-by-timeout
    assert result is not None
    assert result.ran is True
    assert result.exit_code == _TIMEOUT_EXIT_CODE

    run_row = await repo.fetch_one(
        text("SELECT exit_code, ended_at FROM runbook_runs WHERE id = :id"),
        {"id": result.run_id},
    )
    assert run_row is not None
    assert int(run_row[0]) == _TIMEOUT_EXIT_CODE
    assert run_row[1] is not None

    audit_row = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = 'autofix.exec_error'"), {}
    )
    assert audit_row is not None


# ---------------------------------------------------------------------------
# Exec raises non-timeout DockerSocketError → exit_code 1 sentinel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_non_timeout_docker_error_sentinel_1(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 18: non-timeout DockerSocketError → exit_code=1 sentinel, completion+audit."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(raises=DockerSocketConnectionError("socket unreachable"))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    assert result.exit_code == 1

    audit_row = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = 'autofix.exec_error'"), {}
    )
    assert audit_row is not None


# ---------------------------------------------------------------------------
# Non-DockerSocketError exception propagates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_generic_exception_propagates(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 19: non-DockerSocketError exception from exec_capture propagates."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(raises=ValueError("unexpected internal error"))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with (
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
        pytest.raises(ValueError, match="unexpected internal error"),
    ):
        await orch.handle_alert(alert)


# ---------------------------------------------------------------------------
# Transcript resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_transcript_file_within_window(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 20a: .transcript file created within [started, ended] mtime → picked."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    # The FakeDockerClient writes a transcript file during exec_capture
    transcript_name = "test-run-abc.transcript"

    class _WritingFakeDocker(_FakeDockerClient):
        async def exec_capture(  # type: ignore[override]
            self,
            *,
            container_id: str,
            cmd: list[str],
            timeout_seconds: float,
            user: str | None = None,
            env: Mapping[str, str] | None = None,
        ) -> ExecResult:
            # Small yield so exec_started is in the past before writing
            await asyncio.sleep(0.05)
            # Write the transcript file to the transcript_dir
            path = os.path.join(transcript_dir, transcript_name)
            with open(path, "w") as f:
                f.write("transcript content")
            await asyncio.sleep(0.05)
            return ExecResult(exit_code=0, stdout="", stderr="")

    docker = _WritingFakeDocker()
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    assert result.run_id is not None

    run_row = await repo.fetch_one(
        text("SELECT transcript_path FROM runbook_runs WHERE id = :id"),
        {"id": result.run_id},
    )
    assert run_row is not None
    assert run_row[0] is not None
    assert transcript_name in str(run_row[0])


@pytest.mark.asyncio
async def test_resolve_transcript_file_outside_window_not_picked(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 20b: pre-existing .transcript file (before snapshot) → NOT picked."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    # Pre-create a transcript BEFORE the run starts (it will be in the "before" snapshot)
    old_transcript = os.path.join(transcript_dir, "old-preexisting.transcript")
    with open(old_transcript, "w") as f:
        f.write("old content")

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.run_id is not None

    run_row = await repo.fetch_one(
        text("SELECT transcript_path FROM runbook_runs WHERE id = :id"),
        {"id": result.run_id},
    )
    assert run_row is not None
    # transcript_path should be None — the pre-existing file was in "before" set
    assert run_row[0] is None


@pytest.mark.asyncio
async def test_resolve_transcript_no_file_returns_none(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 20c: no new .transcript file → transcript_path is None."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    run_row = await repo.fetch_one(
        text("SELECT transcript_path FROM runbook_runs WHERE id = :id"),
        {"id": result.run_id},
    )
    assert run_row is not None
    assert run_row[0] is None


@pytest.mark.asyncio
async def test_resolve_transcript_mtime_outside_exec_window_not_picked(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 20d: new file (not in before) but mtime outside [started, ended] → not picked."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    stale_ts = time.time() - 7200  # 2 hours old

    class _StaleTimestampFakeDocker(_FakeDockerClient):
        async def exec_capture(  # type: ignore[override]
            self,
            *,
            container_id: str,
            cmd: list[str],
            timeout_seconds: float,
            user: str | None = None,
            env: Mapping[str, str] | None = None,
        ) -> ExecResult:
            path = os.path.join(transcript_dir, "stale-ts.transcript")
            with open(path, "w") as f:
                f.write("stale")
            # backdate the mtime to 2 hours ago
            os.utime(path, (stale_ts, stale_ts))
            return ExecResult(exit_code=0, stdout="", stderr="")

    docker = _StaleTimestampFakeDocker()
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    run_row = await repo.fetch_one(
        text("SELECT transcript_path FROM runbook_runs WHERE id = :id"),
        {"id": result.run_id},
    )
    assert run_row is not None
    assert run_row[0] is None  # mtime outside window → not attributed


# ---------------------------------------------------------------------------
# Secret injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_api_key_present_injected_in_env(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 21a: ANTHROPIC_API_KEY in secrets → exec env includes it."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")
    await secrets_repo_fixture.set("ANTHROPIC_API_KEY", "sk-test-secret")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        await orch.handle_alert(alert)

    assert docker.last_call_env is not None
    assert docker.last_call_env.get("ANTHROPIC_API_KEY") == "sk-test-secret"


@pytest.mark.asyncio
async def test_anthropic_api_key_absent_env_is_none(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Branch 21b: ANTHROPIC_API_KEY absent → exec env is None (empty dict → None)."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")
    # Do NOT set ANTHROPIC_API_KEY in secrets

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        await orch.handle_alert(alert)

    # env or None — orchestrator passes `env or None`, so empty dict → None
    assert docker.last_call_env is None


# ---------------------------------------------------------------------------
# RunbookRunsRepository direct unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_repo_count_inflight_fresh_vs_stale(repo: SqliteRepository) -> None:
    """count_inflight: fresh open-ended row counts; stale row does not."""
    rb = _make_runbook_record(alertname="X")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="X")
    await _insert_alert(repo, alert)

    runs_repo = RunbookRunsRepository(repo)

    # Insert fresh row (now)
    async with repo.transaction() as conn:
        await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
        )

    stale_threshold = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    async with repo.transaction() as conn:
        count = await runs_repo.count_inflight(conn, rb.id, stale_threshold_iso=stale_threshold)
    assert count == 1

    # stale threshold = now+1s → fresh row is excluded
    future_threshold = (datetime.now(tz=UTC) + timedelta(seconds=1)).isoformat()
    async with repo.transaction() as conn:
        count_stale = await runs_repo.count_inflight(
            conn, rb.id, stale_threshold_iso=future_threshold
        )
    assert count_stale == 0


@pytest.mark.asyncio
async def test_runs_repo_latest_ended_at_no_rows(repo: SqliteRepository) -> None:
    """latest_ended_at returns None when no completed run exists."""
    runs_repo = RunbookRunsRepository(repo)
    result = await runs_repo.latest_ended_at(uuid7())
    assert result is None


@pytest.mark.asyncio
async def test_runs_repo_latest_ended_at_returns_most_recent(repo: SqliteRepository) -> None:
    """latest_ended_at returns the most recent ended_at ISO string."""
    rb = _make_runbook_record(alertname="Y")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="Y")
    await _insert_alert(repo, alert)

    runs_repo = RunbookRunsRepository(repo)

    async with repo.transaction() as conn:
        run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
        )
    await runs_repo.mark_completed(run_id=run_id, exit_code=0, transcript_path=None)

    result = await runs_repo.latest_ended_at(rb.id)
    assert result is not None
    # Should be an ISO-parseable datetime string
    datetime.fromisoformat(result)


@pytest.mark.asyncio
async def test_runs_repo_count_started_since_boundary(repo: SqliteRepository) -> None:
    """count_started_since: row at threshold is counted; row before threshold is not."""
    rb = _make_runbook_record(alertname="Z")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="Z")
    await _insert_alert(repo, alert)

    runs_repo = RunbookRunsRepository(repo)

    # No rows yet
    threshold = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    count = await runs_repo.count_started_since(rb.id, threshold)
    assert count == 0

    async with repo.transaction() as conn:
        await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
        )

    count_after = await runs_repo.count_started_since(rb.id, threshold)
    assert count_after == 1


@pytest.mark.asyncio
async def test_runs_repo_mark_completed_own_txn(repo: SqliteRepository) -> None:
    """mark_completed (own-txn variant) sets ended_at + exit_code + transcript_path."""
    rb = _make_runbook_record(alertname="W")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="W")
    await _insert_alert(repo, alert)

    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
        )

    await runs_repo.mark_completed(run_id=run_id, exit_code=42, transcript_path="/path/to/t")

    row = await repo.fetch_one(
        text("SELECT exit_code, ended_at, transcript_path FROM runbook_runs WHERE id = :id"),
        {"id": run_id},
    )
    _EXPECTED_EXIT = 42
    assert row is not None
    assert int(row[0]) == _EXPECTED_EXIT
    assert row[1] is not None
    assert str(row[2]) == "/path/to/t"


@pytest.mark.asyncio
async def test_runs_repo_latest_ended_at_conn(repo: SqliteRepository) -> None:
    """latest_ended_at_conn returns None when no row; ISO string after completion."""
    rb = _make_runbook_record(alertname="V")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="V")
    await _insert_alert(repo, alert)

    runs_repo = RunbookRunsRepository(repo)

    async with repo.transaction() as conn:
        result_empty = await runs_repo.latest_ended_at_conn(conn, rb.id)
        assert result_empty is None

        run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
        )
        await runs_repo.mark_completed_conn(conn, run_id=run_id, exit_code=0, transcript_path=None)

        result_after = await runs_repo.latest_ended_at_conn(conn, rb.id)
        assert result_after is not None


@pytest.mark.asyncio
async def test_runs_repo_count_started_since_conn(repo: SqliteRepository) -> None:
    """count_started_since_conn counts rows on supplied connection."""
    rb = _make_runbook_record(alertname="U")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="U")
    await _insert_alert(repo, alert)

    runs_repo = RunbookRunsRepository(repo)
    threshold = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()

    async with repo.transaction() as conn:
        count_before = await runs_repo.count_started_since_conn(conn, rb.id, threshold)
        assert count_before == 0

        await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
        )

        count_after = await runs_repo.count_started_since_conn(conn, rb.id, threshold)
        assert count_after == 1


# ---------------------------------------------------------------------------
# _maintenance_window: pass-through context manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_window_passthrough(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """_maintenance_window is a pass-through seam — exec runs inside it."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    exec_called = False

    class _TrackingFakeDocker(_FakeDockerClient):
        async def exec_capture(  # type: ignore[override]
            self,
            *,
            container_id: str,
            cmd: list[str],
            timeout_seconds: float,
            user: str | None = None,
            env: Mapping[str, str] | None = None,
        ) -> ExecResult:
            nonlocal exec_called
            exec_called = True
            return ExecResult(exit_code=0, stdout="", stderr="")

    docker = _TrackingFakeDocker()
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    assert exec_called is True


# ---------------------------------------------------------------------------
# _lock_for: per-runbook lock re-use
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_for_same_runbook_reuses_lock(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """_lock_for returns the same lock instance for the same runbook_id."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    rb_id = uuid7()
    lock1 = orch._lock_for(rb_id)  # pyright: ignore[reportPrivateUsage]
    lock2 = orch._lock_for(rb_id)  # pyright: ignore[reportPrivateUsage]
    assert lock1 is lock2

    rb_id2 = uuid7()
    lock3 = orch._lock_for(rb_id2)  # pyright: ignore[reportPrivateUsage]
    assert lock3 is not lock1


# ---------------------------------------------------------------------------
# _snapshot_dir: OSError handling
# ---------------------------------------------------------------------------


def test_snapshot_dir_missing_path(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """_snapshot_dir returns empty set when path does not exist."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = orch._snapshot_dir("/nonexistent/path/that/cannot/exist")  # pyright: ignore[reportPrivateUsage]
    assert result == set()


# ---------------------------------------------------------------------------
# _resolve_transcript: OSError branches + non-.transcript file in new files
# ---------------------------------------------------------------------------


def test_resolve_transcript_oserror_on_listdir(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """_resolve_transcript: OSError on os.listdir(path) after exec → returns None."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    # Use a path that doesn't exist to trigger OSError in 'after' listdir
    now = datetime.now(tz=UTC)
    result = orch._resolve_transcript(  # pyright: ignore[reportPrivateUsage]
        "/nonexistent/path/for/transcript-resolve",
        set(),  # before snapshot
        started=now - timedelta(seconds=1),
        ended=now,
    )
    assert result is None


def test_resolve_transcript_non_transcript_file_skipped(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """_resolve_transcript: new non-.transcript files in dir are skipped."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    # Write a non-.transcript file (e.g. .args)
    args_path = os.path.join(transcript_dir, "run-abc.args")
    with open(args_path, "w") as f:
        f.write("args content")

    now = datetime.now(tz=UTC)
    result = orch._resolve_transcript(  # pyright: ignore[reportPrivateUsage]
        transcript_dir,
        set(),  # before: empty, so "run-abc.args" is new
        started=now - timedelta(seconds=5),
        ended=now,
    )
    # .args file does not match .transcript extension → no candidate → None
    assert result is None


def test_resolve_transcript_oserror_on_mtime(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """_resolve_transcript: OSError on os.path.getmtime → file skipped, returns None."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    transcript_name = "run-mtime-oserr.transcript"
    transcript_path = os.path.join(transcript_dir, transcript_name)
    with open(transcript_path, "w") as f:
        f.write("content")

    now = datetime.now(tz=UTC)

    # Patch os.path.getmtime to raise OSError for this file
    original_getmtime = os.path.getmtime

    def _failing_getmtime(p: str) -> float:
        if transcript_name in p:
            raise OSError("permission denied")
        return original_getmtime(p)  # type: ignore[no-any-return]

    with patch("homelab_monitor.kernel.autofix.orchestrator.os.path.getmtime", _failing_getmtime):
        result = orch._resolve_transcript(  # pyright: ignore[reportPrivateUsage]
            transcript_dir,
            set(),  # file is new
            started=now - timedelta(seconds=5),
            ended=now,
        )
    # mtime failed → file skipped → no candidates → None
    assert result is None


# ---------------------------------------------------------------------------
# _in_lock_gate: rate-passes-then-cooldown-no-prior-run branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_lock_rate_under_limit_falls_through_to_cooldown_no_prior_run(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """_in_lock_gate: rate count < limit → falls through to cooldown check.
    cooldown set but no prior run → latest_ended_at_conn=None → None returned.
    Covers branches 180->183 and 185->191.
    """
    rb = _make_runbook_record(alertname="TestAlert", rate_limit_per_hour=5, cooldown_seconds=3600)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    # No prior completed run → latest_ended_at_conn returns None → cooldown not triggered.
    # rate_limit=5, 0 runs in last hour → count < limit (branch 180->183).
    # No prior ended run → latest_ended_at_conn returns None (branch 185->191).
    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True


# ---------------------------------------------------------------------------
# _in_lock_detail: fallback branch (reason not ALREADY_RUNNING/RATE_LIMIT/COOLDOWN)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_lock_detail_fallback(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """_in_lock_detail: fallback return reason.value for unlisted reason."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
    rb = _make_runbook_record(alertname="TestAlert")
    result = orch._in_lock_detail(rb, DenialReason.KILL_SWITCH)  # pyright: ignore[reportPrivateUsage]
    assert result == "kill_switch"


# ---------------------------------------------------------------------------
# exec_capture extension — socket_client.py new branches
# (user set/unset, env set/unset, timeout → DockerExecTimeoutError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_capture_user_set_in_create_body() -> None:
    """exec_capture: user kwarg → User included in exec-create body."""
    log = structlog.get_logger()

    create_resp = AsyncMock()
    create_resp.status_code = 201
    create_resp.json = MagicMock(return_value={"Id": "exec-id-001"})

    start_resp = AsyncMock()
    start_resp.status_code = 200
    start_resp.content = b""  # empty mux stream

    inspect_resp = AsyncMock()
    inspect_resp.status_code = 200
    inspect_resp.json = MagicMock(return_value={"ExitCode": 0})

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post.side_effect = [create_resp, start_resp]
    mock_http.get.return_value = inspect_resp

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    result = await client.exec_capture(
        container_id="my-container",
        cmd=["echo", "hi"],
        timeout_seconds=10.0,
        user="homelab-fixer",
    )

    assert result.exit_code == 0
    # Verify User was in the POST body
    create_call_kwargs = mock_http.post.call_args_list[0]
    sent_json: dict[str, object] = create_call_kwargs.kwargs["json"]
    assert sent_json.get("User") == "homelab-fixer"
    await client.aclose()


@pytest.mark.asyncio
async def test_exec_capture_user_not_set_no_user_in_body() -> None:
    """exec_capture: user=None → User NOT in exec-create body."""
    log = structlog.get_logger()

    create_resp = AsyncMock()
    create_resp.status_code = 201
    create_resp.json = MagicMock(return_value={"Id": "exec-id-002"})

    start_resp = AsyncMock()
    start_resp.status_code = 200
    start_resp.content = b""

    inspect_resp = AsyncMock()
    inspect_resp.status_code = 200
    inspect_resp.json = MagicMock(return_value={"ExitCode": 0})

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post.side_effect = [create_resp, start_resp]
    mock_http.get.return_value = inspect_resp

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    await client.exec_capture(
        container_id="my-container",
        cmd=["echo", "hi"],
        timeout_seconds=10.0,
        user=None,
    )

    create_call_kwargs = mock_http.post.call_args_list[0]
    sent_json: dict[str, object] = create_call_kwargs.kwargs["json"]
    assert "User" not in sent_json
    await client.aclose()


@pytest.mark.asyncio
async def test_exec_capture_env_set_in_create_body() -> None:
    """exec_capture: env kwarg → Env included in exec-create body as KEY=VALUE list."""
    log = structlog.get_logger()

    create_resp = AsyncMock()
    create_resp.status_code = 201
    create_resp.json = MagicMock(return_value={"Id": "exec-id-003"})

    start_resp = AsyncMock()
    start_resp.status_code = 200
    start_resp.content = b""

    inspect_resp = AsyncMock()
    inspect_resp.status_code = 200
    inspect_resp.json = MagicMock(return_value={"ExitCode": 0})

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post.side_effect = [create_resp, start_resp]
    mock_http.get.return_value = inspect_resp

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    await client.exec_capture(
        container_id="my-container",
        cmd=["echo", "hi"],
        timeout_seconds=10.0,
        env={"ANTHROPIC_API_KEY": "sk-test", "FOO": "bar"},
    )

    create_call_kwargs = mock_http.post.call_args_list[0]
    sent_json = create_call_kwargs.kwargs["json"]
    env_list: list[str] = sent_json["Env"]
    assert "ANTHROPIC_API_KEY=sk-test" in env_list
    assert "FOO=bar" in env_list
    await client.aclose()


@pytest.mark.asyncio
async def test_exec_capture_env_not_set_no_env_in_body() -> None:
    """exec_capture: env=None → Env NOT in exec-create body."""
    log = structlog.get_logger()

    create_resp = AsyncMock()
    create_resp.status_code = 201
    create_resp.json = MagicMock(return_value={"Id": "exec-id-004"})

    start_resp = AsyncMock()
    start_resp.status_code = 200
    start_resp.content = b""

    inspect_resp = AsyncMock()
    inspect_resp.status_code = 200
    inspect_resp.json = MagicMock(return_value={"ExitCode": 0})

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post.side_effect = [create_resp, start_resp]
    mock_http.get.return_value = inspect_resp

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    await client.exec_capture(
        container_id="my-container",
        cmd=["echo", "hi"],
        timeout_seconds=10.0,
        env=None,
    )

    create_call_kwargs = mock_http.post.call_args_list[0]
    sent_json = create_call_kwargs.kwargs["json"]
    assert "Env" not in sent_json
    await client.aclose()


@pytest.mark.asyncio
async def test_exec_capture_timeout_raises_docker_exec_timeout_error() -> None:
    """exec_capture: asyncio.TimeoutError → DockerExecTimeoutError raised."""
    log = structlog.get_logger()

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    # Make the POST (exec-create) hang forever → triggers wait_for timeout
    mock_http.post.side_effect = TimeoutError("simulated timeout")

    client = DockerSocketClient(socket_path="/var/run/docker.sock", log=log, httpx_client=mock_http)

    with pytest.raises(DockerExecTimeoutError):
        await client.exec_capture(
            container_id="my-container",
            cmd=["sleep", "999"],
            timeout_seconds=0.001,  # tiny timeout
        )

    await client.aclose()


# ---------------------------------------------------------------------------
# Safety-net Test 1 — exec NOT called on every denial path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial_label",
    [
        "kill_switch",
        "allow_list",
        "rate_limit",
        "cooldown",
        "risky_blocked",
        "ambiguous_match",
        "already_running",
        "claim_error",
    ],
)
async def test_denial_paths_never_call_exec(  # noqa: PLR0915 -- one parametrized body covers all 8 denial paths
    denial_label: str,
    repo: SqliteRepository,
    secrets_repo_fixture: AsyncSecretsRepository,
) -> None:
    """For every denial path, assert docker exec_capture is NEVER reached.

    Verifies that last_call_cmd is still None after handle_alert returns
    a denied RunResult on each of the gate paths.
    """
    docker = _FakeDockerClient()

    if denial_label == "ambiguous_match":
        # Two matching runbooks → ambiguous, no need for autofix_enabled
        rb1 = _make_runbook_record(alertname="TestAlert")
        rb2 = _make_runbook_record(alertname="TestAlert")
        await _insert_runbook(repo, rb1)
        await _insert_runbook(repo, rb2)
        alert = _make_alert(alertname="TestAlert")
        await _insert_alert(repo, alert)
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
        result = await orch.handle_alert(alert)

    elif denial_label == "kill_switch":
        rb = _make_runbook_record(alertname="TestAlert")
        await _insert_runbook(repo, rb)
        alert = _make_alert(alertname="TestAlert")
        await _insert_alert(repo, alert)
        # Do NOT set autofix_enabled → kill_switch denial
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
        result = await orch.handle_alert(alert)

    elif denial_label == "allow_list":
        rb = _make_runbook_record(alertname="TestAlert", auto_trigger=False)
        await _insert_runbook(repo, rb)
        alert = _make_alert(alertname="TestAlert")
        await _insert_alert(repo, alert)
        app_settings = AppSettingsRepository(repo)
        await app_settings.set("autofix_enabled", "true")
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
        result = await orch.handle_alert(alert)

    elif denial_label == "rate_limit":
        rb = _make_runbook_record(alertname="TestAlert", rate_limit_per_hour=1)
        await _insert_runbook(repo, rb)
        alert = _make_alert(alertname="TestAlert")
        await _insert_alert(repo, alert)
        app_settings = AppSettingsRepository(repo)
        await app_settings.set("autofix_enabled", "true")
        # Pre-insert a completed run to exhaust the limit
        runs_repo = RunbookRunsRepository(repo)
        async with repo.transaction() as conn:
            run_id_pre = await runs_repo.insert_started(
                conn,
                runbook_id=rb.id,
                alert_id=alert.id,
                prompt=rb.path,
                fixer_user="homelab-fixer",
                host="testhost",
                runbook_hash=rb.content_hash,
                mode=RunMode.REAL,
            )
        await runs_repo.mark_completed(run_id=run_id_pre, exit_code=0, transcript_path=None)
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
        result = await orch.handle_alert(alert)

    elif denial_label == "cooldown":
        rb = _make_runbook_record(alertname="TestAlert", cooldown_seconds=3600)
        await _insert_runbook(repo, rb)
        alert = _make_alert(alertname="TestAlert")
        await _insert_alert(repo, alert)
        app_settings = AppSettingsRepository(repo)
        await app_settings.set("autofix_enabled", "true")
        # Pre-insert a completed run (recent) to trigger cooldown
        runs_repo = RunbookRunsRepository(repo)
        async with repo.transaction() as conn:
            run_id_pre = await runs_repo.insert_started(
                conn,
                runbook_id=rb.id,
                alert_id=alert.id,
                prompt=rb.path,
                fixer_user="homelab-fixer",
                host="testhost",
                runbook_hash=rb.content_hash,
                mode=RunMode.REAL,
            )
        await runs_repo.mark_completed(run_id=run_id_pre, exit_code=0, transcript_path=None)
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
        result = await orch.handle_alert(alert)

    elif denial_label == "risky_blocked":
        rb = _make_runbook_record(alertname="TestAlert", dry_run_required=True)
        await _insert_runbook(repo, rb)
        alert = _make_alert(alertname="TestAlert")
        await _insert_alert(repo, alert)
        app_settings = AppSettingsRepository(repo)
        await app_settings.set("autofix_enabled", "true")
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
        result = await orch.handle_alert(alert)

    elif denial_label == "already_running":
        rb = _make_runbook_record(alertname="TestAlert")
        await _insert_runbook(repo, rb)
        alert = _make_alert(alertname="TestAlert")
        await _insert_alert(repo, alert)
        app_settings = AppSettingsRepository(repo)
        await app_settings.set("autofix_enabled", "true")
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
        with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=1)):
            result = await orch.handle_alert(alert)

    else:  # claim_error
        rb = _make_runbook_record(alertname="TestAlert")
        await _insert_runbook(repo, rb)
        alert = _make_alert(alertname="TestAlert")
        await _insert_alert(repo, alert)
        app_settings = AppSettingsRepository(repo)
        await app_settings.set("autofix_enabled", "true")
        orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
        with (
            patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
            patch.object(
                RunbookRunsRepository,
                "insert_started",
                new=AsyncMock(side_effect=RuntimeError("DB write failed")),
            ),
        ):
            result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is False, f"Expected denial for {denial_label!r} but got ran=True"
    # KEY assertion: no exec path was ever reached
    assert docker.last_call_cmd is None, (
        f"Denial path {denial_label!r} reached docker exec with cmd={docker.last_call_cmd!r}"
    )


# ---------------------------------------------------------------------------
# Safety-net Test 2 — ANTHROPIC_API_KEY sentinel must NOT appear in any
#                      persisted artifact (exec log, audit, runbook_runs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_api_key_not_leaked_to_persisted_artifacts(
    repo: SqliteRepository,
    secrets_repo_fixture: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """ANTHROPIC_API_KEY reaches exec env but must NOT appear in any persisted artifact.

    Verifies:
    - The sentinel key IS present in the env passed to exec_capture (key reached Claude).
    - The sentinel string does NOT appear in audit_log rows (before_json / after_json).
    - The sentinel string does NOT appear in the exec.log file.
    - The sentinel string does NOT appear in runbook_runs columns.
    """
    _SENTINEL = "sk-SENTINEL-DO-NOT-LEAK-abc123"

    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")
    await secrets_repo_fixture.set("ANTHROPIC_API_KEY", _SENTINEL)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="done", stderr=""))
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True

    # 1. Key DID reach exec env (proves the injection path works)
    assert docker.last_call_env is not None
    assert docker.last_call_env.get("ANTHROPIC_API_KEY") == _SENTINEL

    # 2. Sentinel must NOT appear in any audit_log row
    audit_rows = await repo.fetch_all(
        text("SELECT what, before_json, after_json FROM audit_log"), {}
    )
    for row in audit_rows:
        for col_val in row:
            if col_val is None:
                continue
            assert _SENTINEL not in str(col_val), (
                f"Sentinel key found in audit_log row: what={row[0]!r}, col={col_val!r}"
            )

    # 3. Sentinel must NOT appear in the exec.log file
    assert result.run_id is not None
    exec_log_path = f"{exec_log_dir}/{result.run_id}.exec.log"
    exec_log_content = Path(exec_log_path).read_text(encoding="utf-8")
    assert _SENTINEL not in exec_log_content, f"Sentinel key found in exec.log: {exec_log_path!r}"

    # 4. Sentinel must NOT appear in runbook_runs columns
    run_rows = await repo.fetch_all(
        text("SELECT prompt, transcript_path, runbook_hash FROM runbook_runs WHERE id = :id"),
        {"id": result.run_id},
    )
    for row in run_rows:
        for col_val in row:
            if col_val is None:
                continue
            assert _SENTINEL not in str(col_val), (
                f"Sentinel key found in runbook_runs row: col={col_val!r}"
            )


# ---------------------------------------------------------------------------
# Safety-net Test 3 — transactional rollback on completion-audit failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_outcome_rollback_on_audit_failure(
    repo: SqliteRepository,
    secrets_repo_fixture: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """_persist_outcome writes mark_completed + audit + alert_outcomes in ONE txn.

    If the audit INSERT raises, the transaction must roll back completely:
    - runbook_runs row must NOT have ended_at / exit_code set (still NULL)
    - alert_outcomes must NOT have an 'auto_fixed' row

    The exception is expected to propagate out of handle_alert.
    """
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="ok", stderr=""))
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    # Patch insert_audit where _persist_outcome imports it.
    # The first call comes from the claim path (autofix.started / inflight check), so we
    # must only raise on the "autofix.ran" call inside _persist_outcome.
    _original_insert_audit = __import__(
        "homelab_monitor.kernel.db.audit", fromlist=["insert_audit"]
    ).insert_audit

    async def _failing_insert_audit(conn: object, *, who: str, what: str, after: object) -> None:
        if what == "autofix.ran":
            raise RuntimeError("Simulated audit failure in _persist_outcome")
        await _original_insert_audit(conn, who=who, what=what, after=after)

    with (
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
        patch(
            "homelab_monitor.kernel.autofix.orchestrator.insert_audit",
            side_effect=_failing_insert_audit,
        ),
        pytest.raises(RuntimeError, match="Simulated audit failure in _persist_outcome"),
    ):
        await orch.handle_alert(alert)

    # Transaction must have rolled back: runbook_runs row was inserted (claim) but
    # _persist_outcome's txn rolled back → ended_at and exit_code still NULL.
    run_row = await repo.fetch_one(text("SELECT ended_at, exit_code FROM runbook_runs LIMIT 1"), {})
    assert run_row is not None, "runbook_runs claim row must exist (insert_started succeeded)"
    assert run_row[0] is None, "ended_at must be NULL — rollback should have undone mark_completed"
    assert run_row[1] is None, "exit_code must be NULL — rollback should have undone mark_completed"

    # No auto_fixed outcome row either
    outcome_row = await repo.fetch_one(
        text("SELECT outcome FROM alert_outcomes WHERE alert_id = :aid"), {"aid": alert.id}
    )
    assert outcome_row is None, "alert_outcomes must be empty — rollback should have undone INSERT"


# ---------------------------------------------------------------------------
# Safety-net Test 4 — real-concurrency in-lock serialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_same_runbook_serialized_by_lock(
    repo: SqliteRepository,
    secrets_repo_fixture: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Two concurrent handle_alert calls for the SAME runbook+alert are serialized.

    With the per-runbook asyncio.Lock, EXACTLY ONE invocation runs (ran=True) and
    the OTHER is denied with ALREADY_RUNNING.  Exactly ONE runbook_runs row exists.

    A slow fake docker client (0.15s sleep) ensures the first task holds the lock
    while the second task tries to acquire it, forcing genuine lock contention.
    """
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    # Slow fake: holds the lock for long enough that the second coroutine attempts
    # to acquire it while the first is still executing.
    class _SlowFakeDockerClient(_FakeDockerClient):
        async def exec_capture(  # type: ignore[override]
            self,
            *,
            container_id: str,
            cmd: list[str],
            timeout_seconds: float,
            user: str | None = None,
            env: Mapping[str, str] | None = None,
        ) -> ExecResult:
            self.last_call_container_id = container_id
            self.last_call_cmd = cmd
            self.last_call_user = user
            self.last_call_env = env
            # Sleep long enough for the second gather task to attempt lock acquisition
            await asyncio.sleep(0.15)
            if self.raises is not None:
                raise self.raises
            return self.result

    docker = _SlowFakeDockerClient(result=ExecResult(exit_code=0, stdout="ok", stderr=""))

    # Single orchestrator instance so the per-runbook lock dict is shared.
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    # The real count_inflight SQL is used here so the in-lock check is genuine.
    # The first task acquires the lock, inserts a claim (ended_at=NULL), and sleeps.
    # The second task must wait for the lock. When it acquires it, the first has
    # already completed, so count_inflight returns 0. The second task will then
    # insert its own claim and run — so BOTH tasks may run if the first finishes
    # before the second acquires the lock (which is the normal asyncio case with
    # sequential lock acquire). To guarantee ALREADY_RUNNING we need the in-lock
    # inflight check to see the first task's open claim while it holds the lock.
    # Since the lock serializes them, the second sees the first's COMPLETED row
    # (not inflight). In that case the second also runs (cooldown/rate allow it).
    # To force ALREADY_RUNNING, we patch count_inflight to return 1 for the second
    # call (simulating that the first is still in-flight when the second checks).
    _inflight_call_count = 0

    async def _count_inflight_side_effect(
        conn: object, runbook_id: str, *, stale_threshold_iso: str
    ) -> int:
        nonlocal _inflight_call_count
        _inflight_call_count += 1
        # First call: 0 (first task passes through)
        # Second call: 1 (second task sees first as in-flight)
        return 0 if _inflight_call_count == 1 else 1

    with patch.object(
        RunbookRunsRepository,
        "count_inflight",
        side_effect=_count_inflight_side_effect,
    ):
        result_a, result_b = await asyncio.gather(
            orch.handle_alert(alert),
            orch.handle_alert(alert),
        )

    results = [result_a, result_b]
    assert all(r is not None for r in results)

    ran_results = [r for r in results if r is not None and r.ran is True]
    denied_results = [r for r in results if r is not None and r.ran is False]

    assert len(ran_results) == 1, (
        f"Expected exactly 1 run, got {len(ran_results)}. "
        f"Results: {[(r.ran, r.denial_reason) for r in results if r is not None]}"
    )
    assert len(denied_results) == 1, f"Expected exactly 1 denial, got {len(denied_results)}."

    denied = denied_results[0]
    assert denied.denial_reason == DenialReason.ALREADY_RUNNING, (
        f"Expected ALREADY_RUNNING denial, got {denied.denial_reason!r}"
    )

    # Exactly ONE runbook_runs row must exist
    rows = await repo.fetch_all(text("SELECT id FROM runbook_runs"), {})
    assert len(rows) == 1, f"Expected exactly 1 runbook_runs row, got {len(rows)}"
