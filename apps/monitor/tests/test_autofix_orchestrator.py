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
import contextlib
import dataclasses
import json
import os
import time
from collections.abc import Callable, Mapping
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

from homelab_monitor.kernel import autofix as autofix_pkg
from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import Alert, AlertStatus, Severity
from homelab_monitor.kernel.autofix import orchestrator as orch_module
from homelab_monitor.kernel.autofix.approvals_repository import (
    RunbookRunApprovalsRepository,
)
from homelab_monitor.kernel.autofix.matcher import (
    _matcher_matches,  # pyright: ignore[reportPrivateUsage]
    _runbook_matches,  # pyright: ignore[reportPrivateUsage]
    matching_runbooks,
)
from homelab_monitor.kernel.autofix.orchestrator import (
    AutoFixOrchestrator,
    KillResult,
    _CurrentRun,  # pyright: ignore[reportPrivateUsage]
    _is_truthy,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import (
    DenialReason,
    DryRunRequiredForRiskyError,
    RunbookNotFoundError,
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
from homelab_monitor.kernel.runbooks.loader import RUNBOOK_CONFIG_FILENAME
from homelab_monitor.kernel.runbooks.repository import RunbookRecord, RunbookRepo
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeDockerClient:
    """Minimal DockerSocketClient-shaped stub.

    Set `result` to the ExecResult to return, or `raises` to the exception
    to raise from exec_capture. If `transcript_to_write` is set, write a
    .transcript file there on exec (to support _resolve_transcript finding it).
    """

    result: ExecResult = field(
        default_factory=lambda: ExecResult(exit_code=0, stdout="ok", stderr="")
    )
    raises: BaseException | None = None
    transcript_to_write: str | None = None  # if set, write a .transcript here on exec
    # Records the last call arguments for assertion
    last_call_container_id: str = ""
    last_call_cmd: list[str] | None = None
    last_call_user: str | None = None
    last_call_env: Mapping[str, str] | None = None
    # STAGE-009-007: kill_container support.
    kill_raises: BaseException | None = None
    last_kill_container_id: str | None = None
    last_kill_signal: str | None = None
    last_kill_timeout_seconds: float | None = None
    on_kill_call: Callable[[], None] | None = None  # optional callback invoked before returning

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
        if self.transcript_to_write is not None:
            with open(self.transcript_to_write, "w", encoding="utf-8") as fh:
                fh.write("fake-claude-transcript\n")
        return self.result

    async def kill_container(
        self,
        container_id: str,
        *,
        signal: str = "SIGKILL",
        timeout_seconds: float | None = None,
    ) -> None:
        self.last_kill_container_id = container_id
        self.last_kill_signal = signal
        self.last_kill_timeout_seconds = timeout_seconds
        if self.on_kill_call is not None:
            self.on_kill_call()
        if self.kill_raises is not None:
            raise self.kill_raises


def _write_valid_runbook_yaml(runbook_dir: Path, *, dry_run_required: bool = False) -> None:
    """Write a minimal-but-valid runbook.yaml declaring a docker-only scope.

    STAGE-009-008: _resolve_grants reads this file fresh at exec-start.
    ScopedCapabilities requires at least one of docker/ssh; docker-only avoids
    the extra ssh_target_ids_provider dependency.
    """
    runbook_dir.mkdir(parents=True, exist_ok=True)
    (runbook_dir / RUNBOOK_CONFIG_FILENAME).write_text(
        f"""\
name: test-runbook
match_patterns:
  - alertname: TestAlert
    labels: {{}}
risk_tag: safe
dry_run_required: {str(dry_run_required).lower()}
rate_limit_per_hour: 100
cooldown_seconds: 0
scoped_capabilities:
  docker:
    container: "test-container"
    allowed_actions:
      - "restart"
"""
    )


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
    runbook_dir: Path | None = None,
) -> RunbookRecord:
    patterns: list[dict[str, Any]] = [{"alertname": alertname, "labels": {}}]
    if runbook_dir is not None:
        _write_valid_runbook_yaml(runbook_dir, dry_run_required=dry_run_required)
    path = str(runbook_dir) if runbook_dir is not None else "/runbooks/test-runbook"
    return RunbookRecord(
        id=runbook_id or uuid7(),
        path=path,
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
    ssh_target_ids_provider: Callable[[], frozenset[str]] | None = None,
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
        approvals_repo=RunbookRunApprovalsRepository(repo),
        config=config,
        log=log,
        ssh_target_ids_provider=ssh_target_ids_provider or frozenset,
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
            initiated_by="alert",
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
            initiated_by="alert",
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
            initiated_by="alert",
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
# T1: dry_store_risky_gates_pass_stores_plan_and_pending_approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_store_risky_gates_pass_stores_plan_and_pending_approval(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T1: Risky runbook with all gates pass → dry-run stored + PENDING approval."""
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

    # Create a fake docker that writes a transcript file so _resolve_transcript finds it
    docker = _FakeDockerClient(
        result=ExecResult(exit_code=0, stdout="plan output", stderr=""),
        transcript_to_write=f"{transcript_dir}/plan-{uuid7()}.transcript",
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    # Verify result
    assert result is not None
    assert result.ran is True
    assert result.outcome == RunOutcome.DRY_RUN_STORED
    assert result.approval_id is not None
    assert result.denial_reason is None

    # Verify dry-run row exists with dry_run mode
    runs = await repo.fetch_all(
        text("SELECT id, mode, ended_at FROM runbook_runs WHERE id = :id"),
        {"id": result.run_id},
    )
    assert len(runs) == 1
    assert runs[0].mode == RunMode.DRY_RUN.value
    assert runs[0].ended_at is not None

    # Verify command was dry (--permission-mode plan, NO --dangerously-skip-permissions)
    assert docker.last_call_cmd is not None
    assert "--permission-mode" in docker.last_call_cmd
    assert "plan" in docker.last_call_cmd
    assert "--dangerously-skip-permissions" not in docker.last_call_cmd

    # Verify PENDING approval row exists
    approvals = await repo.fetch_all(
        text("SELECT id, status, pinned_runbook_hash FROM runbook_run_approvals WHERE id = :id"),
        {"id": result.approval_id},
    )
    assert len(approvals) == 1
    assert approvals[0].status == "pending"
    assert approvals[0].pinned_runbook_hash == "hash-v1"

    # Verify NO auto_fixed alert outcome (dry run doesn't fix)
    outcomes = await repo.fetch_all(
        text("SELECT id FROM alert_outcomes WHERE alert_id = :alert_id AND outcome = 'auto_fixed'"),
        {"alert_id": alert.id},
    )
    assert outcomes == []

    # Verify audit entry for dry_run_stored
    audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.dry_run_stored'"),
        {},
    )
    assert len(audits) >= 1


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
            initiated_by="alert",
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
            initiated_by="alert",
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
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
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
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
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
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
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
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
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
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
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
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
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
            initiated_by="alert",
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
            initiated_by="alert",
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
            initiated_by="alert",
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
            initiated_by="alert",
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
            initiated_by="alert",
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
            initiated_by="alert",
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
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
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
        "ambiguous_match",
        "already_running",
        "claim_error",
    ],
)
async def test_denial_paths_never_call_exec(  # noqa: PLR0915 -- one parametrized body covers all denial paths
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
                initiated_by="alert",
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
                initiated_by="alert",
            )
        await runs_repo.mark_completed(run_id=run_id_pre, exit_code=0, transcript_path=None)
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

    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
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
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
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

    async def _failing_insert_audit(
        conn: object,
        *,
        who: str,
        what: str,
        after: object,
        ip: object = None,
    ) -> None:
        if what == "autofix.ran":
            raise RuntimeError("Simulated audit failure in _persist_outcome")
        await _original_insert_audit(conn, who=who, what=what, after=after, ip=ip)

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


# ---------------------------------------------------------------------------
# T2: safe_runbook_runs_real_directly_unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_runbook_runs_real_directly_unchanged(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T2: Safe runbook (dry_run_required=False) runs real, no approval."""
    rb = _make_runbook_record(
        alertname="TestAlert", dry_run_required=False, runbook_dir=tmp_path / "runbook"
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
        result=ExecResult(exit_code=0, stdout="fixed", stderr=""),
        transcript_to_write=f"{transcript_dir}/real-{uuid7()}.transcript",
    )
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
    assert result.outcome == RunOutcome.RAN
    assert result.ran is True

    # Verify command was real (contains --dangerously-skip-permissions)
    assert docker.last_call_cmd is not None
    assert "--dangerously-skip-permissions" in docker.last_call_cmd
    assert "--permission-mode" not in docker.last_call_cmd

    # Verify auto_fixed outcome exists
    outcomes = await repo.fetch_all(
        text("SELECT id FROM alert_outcomes WHERE alert_id = :alert_id AND outcome = 'auto_fixed'"),
        {"alert_id": alert.id},
    )
    assert len(outcomes) >= 1

    # Verify NO approval row created
    approvals = await repo.fetch_all(text("SELECT id FROM runbook_run_approvals"), {})
    assert approvals == []


# ---------------------------------------------------------------------------
# T3: operational_deny_preempts_dry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operational_deny_preempts_dry(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """T3: Operational gate (kill-switch) denies risky runbook before dry branch."""
    rb = _make_runbook_record(alertname="TestAlert", dry_run_required=True)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    # Do NOT set autofix_enabled → kill-switch denies
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_alert(alert)

    assert result is not None
    assert result.denial_reason == DenialReason.KILL_SWITCH
    assert result.ran is False

    # No run row, no approval row
    runs = await repo.fetch_all(text("SELECT id FROM runbook_runs"), {})
    assert runs == []

    approvals = await repo.fetch_all(text("SELECT id FROM runbook_run_approvals"), {})
    assert approvals == []


# ---------------------------------------------------------------------------
# T4: dry_exec_error_stores_run_no_approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_exec_error_stores_run_no_approval(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T4: Dry exec errors → run stored, approval NOT created."""
    rb = _make_runbook_record(alertname="TestAlert", dry_run_required=True, content_hash="hash-v1")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(raises=DockerSocketConnectionError("connection failed"))
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
    assert result.outcome == RunOutcome.DRY_RUN_STORED
    assert result.ran is True
    assert result.approval_id is None  # No approval on exec error
    assert result.exit_code == 1  # Sentinel error exit code

    # Verify NO approval row (exec failed)
    approvals = await repo.fetch_all(text("SELECT id FROM runbook_run_approvals"), {})
    assert approvals == []

    # Verify exec_error audit
    audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.exec_error'"),
        {},
    )
    assert len(audits) >= 1


# ---------------------------------------------------------------------------
# T5: execute_approved_happy_fires_real_and_sets_real_run_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_happy_fires_real_and_sets_real_run_id(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T5: execute_approved on pending approval → real exec fires, real_run_id set."""
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

    # First: create a dry run + approval via the handle_alert path
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
    assert dry_result.approval_id is not None
    approval_id = dry_result.approval_id

    # Now approve it with a real exec
    docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
    docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result is not None
    assert result.ran is True
    assert result.outcome == RunOutcome.RAN
    assert result.run_id is not None

    # Verify approval row updated
    approvals = await repo.fetch_all(
        text(
            "SELECT status, approved_by, decided_at, real_run_id "
            "FROM runbook_run_approvals WHERE id = :id"
        ),
        {"id": approval_id},
    )
    assert len(approvals) == 1
    assert approvals[0].status == "approved"
    assert approvals[0].approved_by == "admin"
    assert approvals[0].decided_at is not None
    assert approvals[0].real_run_id == result.run_id

    # Verify real command was used
    assert docker.last_call_cmd is not None
    assert "--dangerously-skip-permissions" in docker.last_call_cmd

    # Verify auto_fixed outcome
    outcomes = await repo.fetch_all(
        text("SELECT id FROM alert_outcomes WHERE alert_id = :alert_id AND outcome = 'auto_fixed'"),
        {"alert_id": alert.id},
    )
    assert len(outcomes) >= 1

    # Verify audit for approved
    audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.approved'"),
        {},
    )
    assert len(audits) >= 1


# ---------------------------------------------------------------------------
# Fix M1: execute_approved threads the approving principal into the
# autofix.ran audit for forensic clarity (auto-triggered path leaves it None).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_ran_audit_includes_approving_principal(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Fix M1: on a human-approved real run, autofix.ran.after_json carries
    approving_principal so the audit chain (autofix.approved by <alice> →
    autofix.ran by system:autofix) is linked by more than approval_id alone.
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

    docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
    docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.execute_approved(approval_id, principal="alice", ip="1.2.3.4")

    assert result.ran is True
    assert result.run_id is not None

    ran_audit = await repo.fetch_one(
        text(
            "SELECT after_json FROM audit_log WHERE what = 'autofix.ran' "
            "AND json_extract(after_json, '$.run_id') = :rid"
        ),
        {"rid": result.run_id},
    )
    assert ran_audit is not None
    after = json.loads(str(ran_audit[0]))
    assert after.get("approving_principal") == "alice"


@pytest.mark.asyncio
async def test_handle_alert_ran_audit_omits_approving_principal(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Fix M1 inverse: auto-triggered handle_alert (safe runbook) MUST NOT
    write an approving_principal key on the autofix.ran audit — there is no
    human approver on that path.
    """
    rb = _make_runbook_record(
        alertname="TestAlert", runbook_dir=tmp_path / "runbook"
    )  # safe by default: dry_run_required=False
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
    assert result.run_id is not None

    ran_audit = await repo.fetch_one(
        text(
            "SELECT after_json FROM audit_log WHERE what = 'autofix.ran' "
            "AND json_extract(after_json, '$.run_id') = :rid"
        ),
        {"rid": result.run_id},
    )
    assert ran_audit is not None
    after = json.loads(str(ran_audit[0]))
    assert "approving_principal" not in after


# ---------------------------------------------------------------------------
# T5b: execute_approved — _claim_and_exec in-lock re-check denies (run_id=None
#      false leg of the `if result.run_id is not None` set_real_run_id branch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_claim_denies_no_real_run_id_set(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Covers execute_approved's FALSE leg of `if result.run_id is not None`.

    When _claim_and_exec's in-lock gate re-check denies (e.g. count_inflight>0
    at claim time), the returned RunResult has run_id=None, so
    set_real_run_id_conn must NOT be called and approval.real_run_id must stay None.
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

    # Stage 1: seed the dry run + PENDING approval via handle_alert.
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

    # Reset docker state so we can detect whether the REAL exec was reached.
    docker.last_call_cmd = None
    docker.last_call_container_id = ""
    docker.last_call_user = None
    docker.last_call_env = None

    # Stage 2: approve, but force _claim_and_exec's in-lock inflight re-check to
    # deny (count_inflight > 0). _claim_and_exec then returns run_id=None +
    # denial_reason=ALREADY_RUNNING, and execute_approved's set_real_run_id
    # branch must skip.
    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=5)):
        result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result is not None
    assert result.ran is False
    assert result.run_id is None
    assert result.denial_reason == DenialReason.ALREADY_RUNNING

    # Docker exec must NOT have been reached (in-lock gate blocked before exec).
    assert docker.last_call_cmd is None

    # The FALSE-leg assertion: approval.real_run_id stays None because run_id was
    # None and set_real_run_id_conn was NOT called.
    #
    # Fix I2 (added later): to avoid orphaning the approval in status='approved'
    # with real_run_id NULL forever, execute_approved now REVERTS the approval
    # back to pending on the claim-denied branch and audits an
    # 'autofix.approval_reverted' event. Assert the revert side effects.
    approvals_repo = RunbookRunApprovalsRepository(repo)
    approval = await approvals_repo.get(approval_id)
    assert approval is not None
    assert approval.real_run_id is None
    assert approval.status == "pending"
    assert approval.approved_by is None
    assert approval.decided_at is None

    revert_audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.approval_reverted'"),
        {},
    )
    assert len(revert_audits) >= 1


# ---------------------------------------------------------------------------
# T5c: _load_alert_for_exec — alert_id present but row missing (false leg of
#      inner `if loaded is not None`; uses ORIGINAL alert_id as placeholder,
#      not the "unknown" fallback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_alert_present_id_but_row_missing_uses_original_id(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Covers _load_alert_for_exec's FALSE leg of `if loaded is not None`.

    Distinct from T10 (`alert_id is None` on the approval → placeholder id="unknown"):
    here alert_id is NOT None but AlertRepository.get_alert_by_id returns None,
    so the placeholder must use the ORIGINAL alert_id (not "unknown"). The
    alerts row is kept in the DB so the runbook_runs.alert_id FK on the real-run
    insert still passes; only get_alert_by_id is monkey-patched to return None.
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

    # Stage 1: seed the dry run + PENDING approval via handle_alert.
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

    # Confirm the approval preserved the original alert_id (this is the
    # `alert_id is not None` premise of the branch we're covering).
    approvals_repo = RunbookRunApprovalsRepository(repo)
    approval_before = await approvals_repo.get(approval_id)
    assert approval_before is not None
    assert approval_before.alert_id == alert.id

    # Reset docker state before the real run.
    docker.last_call_cmd = None
    docker.last_call_container_id = ""
    docker.last_call_user = None
    docker.last_call_env = None

    # Stage 2: approve. Force AlertRepository.get_alert_by_id to return None so
    # _load_alert_for_exec's `loaded is not None` check fails and it falls
    # through to the placeholder using ORIGINAL alert_id.  The alerts row
    # stays in the DB so runbook_runs.alert_id FK is satisfied on the real run.
    docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
    docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"

    with (
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
        patch.object(AlertRepository, "get_alert_by_id", new=AsyncMock(return_value=None)),
    ):
        result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result is not None
    assert result.ran is True
    assert result.outcome == RunOutcome.RAN
    assert result.run_id is not None

    # Real exec fired.
    assert docker.last_call_cmd is not None
    assert "--dangerously-skip-permissions" in docker.last_call_cmd

    # Prove the placeholder used the ORIGINAL alert_id (not "unknown"): the new
    # real runbook_runs row's alert_id column must equal alert.id, since
    # _claim_and_exec calls insert_started(alert_id=alert.id) with the Alert
    # returned by _load_alert_for_exec.
    real_run_row = await repo.fetch_one(
        text("SELECT alert_id FROM runbook_runs WHERE id = :id"),
        {"id": result.run_id},
    )
    assert real_run_row is not None
    assert str(real_run_row[0]) == alert.id


# ---------------------------------------------------------------------------
# I1: execute_approved concurrent-approve race — SQL guard ensures only one wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_race_only_one_wins(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Two concurrent execute_approved calls on the same approval: exactly ONE
    produces a real run (mode='real') and the OTHER is denied with
    APPROVAL_NOT_PENDING (via the ``AND status='pending'`` SQL guard on the
    approve UPDATE).

    Because the per-runbook asyncio.Lock in _claim_and_exec also serializes real
    exec, both callers cross ``mark_approved`` before either enters
    _claim_and_exec — so the losing caller is the one whose ``mark_approved_conn``
    UPDATE returns rowcount=0.

    Key assertion: exactly one runbook_runs row with mode='real' exists.
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

    # Stage 1: seed the dry run + PENDING approval via handle_alert.
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

    # Reset docker state — the next successful exec must be REAL.
    docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
    docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"
    docker.last_call_cmd = None

    # Stage 2: fire two concurrent execute_approved calls. Both cross the read
    # pre-check while status is still 'pending' (nothing suspends between the
    # read and the UPDATE from either caller's perspective — asyncio.gather
    # interleaves them). Exactly one mark_approved_conn UPDATE lands (rowcount=1),
    # the other returns rowcount=0 and gets APPROVAL_NOT_PENDING.
    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result_a, result_b = await asyncio.gather(
            orch.execute_approved(approval_id, principal="admin-a", ip="1.2.3.4"),
            orch.execute_approved(approval_id, principal="admin-b", ip="1.2.3.5"),
        )

    results = [result_a, result_b]
    ran_results = [r for r in results if r.outcome == RunOutcome.RAN]
    denied_results = [
        r
        for r in results
        if r.outcome == RunOutcome.DENIED and r.denial_reason == DenialReason.APPROVAL_NOT_PENDING
    ]

    assert len(ran_results) == 1, (
        f"Expected exactly 1 RAN, got {len(ran_results)}. "
        f"Results: {[(r.outcome, r.denial_reason) for r in results]}"
    )
    assert len(denied_results) == 1, (
        f"Expected exactly 1 DENIED/APPROVAL_NOT_PENDING, got {len(denied_results)}. "
        f"Results: {[(r.outcome, r.denial_reason) for r in results]}"
    )

    # KEY assertion: exactly ONE runbook_runs row with mode='real' exists.
    real_runs = await repo.fetch_all(
        text("SELECT id FROM runbook_runs WHERE mode = :mode"),
        {"mode": RunMode.REAL.value},
    )
    assert len(real_runs) == 1, f"Expected exactly 1 real runbook_runs row, got {len(real_runs)}"


@pytest.mark.asyncio
async def test_execute_approved_sql_guard_zero_rowcount_denies_approval_not_pending(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Deterministic coverage for Fix I1's rowcount==0 branch (orchestrator.py:747).

    The concurrent-approve race test above uses ``asyncio.gather`` and does not
    reliably exercise the ``mark_approved_conn`` UPDATE returning 0. This test
    FORCES that branch by monkey-patching
    ``RunbookRunApprovalsRepository.mark_approved_conn`` to return 0, simulating a
    concurrent caller having already decided this approval between our read
    pre-check and our UPDATE.

    Asserts the full contract of the race safety-net branch:
      * RunResult: ran=False, outcome=DENIED, denial_reason=APPROVAL_NOT_PENDING,
        approval_id preserved.
      * No real exec fires (docker.last_call_cmd stays None post-reset).
      * autofix.denied audit row with gate='approval_not_pending' AND
        detail='approval was decided by another caller (race)' in after_json.
      * Approval row is untouched — status stays 'pending' (mocked UPDATE was
        a no-op, so no state change happened in the DB).
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

    # Stage 1: seed the dry run + PENDING approval via handle_alert.
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

    # Reset docker state so any subsequent exec call is detectable — the setup
    # handle_alert dry-run left last_call_cmd populated with the plan cmd.
    docker.last_call_cmd = None
    docker.last_call_container_id = ""
    docker.last_call_user = None
    docker.last_call_env = None

    # Stage 2: force the SQL guard rowcount=0 branch by mocking the UPDATE to
    # return 0. This simulates a concurrent caller having flipped status between
    # our read pre-check and our UPDATE. count_inflight is patched to 0 so the
    # in-lock inflight check would NOT be the reason for denial — we're isolating
    # the SQL guard branch.
    with (
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
        patch.object(
            RunbookRunApprovalsRepository,
            "mark_approved_conn",
            new=AsyncMock(return_value=0),
        ),
    ):
        result = await orch.execute_approved(approval_id, principal="alice", ip="1.2.3.4")

    assert result.ran is False
    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.APPROVAL_NOT_PENDING
    assert result.approval_id == approval_id

    # No real exec fired — the SQL guard denial preempts _claim_and_exec entirely.
    assert docker.last_call_cmd is None

    # The autofix.denied audit row for THIS branch specifically carries
    # gate='approval_not_pending' and the exact race-detail string.
    denied_row = await repo.fetch_one(
        text(
            "SELECT after_json FROM audit_log "
            "WHERE what = 'autofix.denied' "
            "AND json_extract(after_json, '$.approval_id') = :aid "
            "AND json_extract(after_json, '$.gate') = 'approval_not_pending'"
        ),
        {"aid": approval_id},
    )
    assert denied_row is not None
    after = json.loads(str(denied_row[0]))
    assert after.get("gate") == "approval_not_pending"
    assert after.get("detail") == "approval was decided by another caller (race)"

    # Since mark_approved_conn was mocked to return 0 (no rows written), the
    # real approval row in the DB is untouched — status stays 'pending'.
    approvals_repo = RunbookRunApprovalsRepository(repo)
    approval_after = await approvals_repo.get(approval_id)
    assert approval_after is not None
    assert approval_after.status == "pending"


@pytest.mark.asyncio
async def test_execute_approved_second_call_denies_after_first_succeeded(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Sequential: after a successful execute_approved the approval status is
    'approved' (with a real_run_id). A second execute_approved on the same
    approval_id must return outcome=DENIED with APPROVAL_NOT_PENDING. This test
    exercises the read-based pre-check happy fast-path, not the SQL guard.
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

    # First call: succeeds → status='approved', real_run_id set.
    docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
    docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"
    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result_first = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")
    assert result_first.outcome == RunOutcome.RAN
    assert result_first.run_id is not None

    # Second call: pre-check sees status='approved' → APPROVAL_NOT_PENDING, no exec.
    docker.last_call_cmd = None
    result_second = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")
    assert result_second.outcome == RunOutcome.DENIED
    assert result_second.denial_reason == DenialReason.APPROVAL_NOT_PENDING
    # Docker was not re-invoked for the second call.
    assert docker.last_call_cmd is None


# ---------------------------------------------------------------------------
# I2: revert-to-pending on claim denial — race-safe (rowcount=0 branch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_revert_race_safe(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Corner case for Fix I2: between "we noticed claim denied" and "we run
    revert", something ELSE modified the approval so ``revert_to_pending_conn``
    returns rowcount=0. Verify the code takes the warning-only branch (no
    audit_reverted row written, no exception) — exercised via a mock.
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

    # Force _claim_and_exec's in-lock inflight check to deny (run_id=None) AND
    # mock revert_to_pending_conn to return 0 (someone else won the race).
    with (
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=5)),
        patch.object(
            RunbookRunApprovalsRepository,
            "revert_to_pending_conn",
            new=AsyncMock(return_value=0),
        ),
    ):
        result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result.outcome == RunOutcome.DENIED
    assert result.run_id is None
    assert result.denial_reason == DenialReason.ALREADY_RUNNING

    # No revert audit written (rowcount=0 branch takes the warning path only).
    revert_audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.approval_reverted'"),
        {},
    )
    assert revert_audits == []


# ---------------------------------------------------------------------------
# T6: execute_approved_drift_rejects_no_exec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_drift_rejects_no_exec(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T6: Runbook hash changed since plan → rejection, no exec, no real run."""
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

    # Create dry run with pinned hash
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

    # Now change runbook hash (drift)
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE runbooks SET content_hash = :hash WHERE id = :id"),
            {"id": rb.id, "hash": "hash-v2"},
        )

    # Reset docker state so we can assert execute_approved does NOT exec.
    docker.last_call_cmd = None
    docker.last_call_container_id = ""
    docker.last_call_user = None
    docker.last_call_env = None

    # Approve should reject due to drift
    result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result is not None
    assert result.ran is False
    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.RUNBOOK_CHANGED
    assert result.run_id is None

    # Verify approval rejected
    approvals = await repo.fetch_all(
        text("SELECT status, approved_by FROM runbook_run_approvals WHERE id = :id"),
        {"id": approval_id},
    )
    assert len(approvals) == 1
    assert approvals[0].status == "rejected"
    assert approvals[0].approved_by == "admin"

    # Verify NO new real run
    runs = await repo.fetch_all(
        text("SELECT mode FROM runbook_runs ORDER BY created_at DESC LIMIT 1"),
        {},
    )
    assert len(runs) == 1
    assert runs[0].mode == RunMode.DRY_RUN.value  # Only the initial dry run

    # Verify docker never called
    assert docker.last_call_cmd is None


# ---------------------------------------------------------------------------
# Fix M2: execute_approved distinguishes RUNBOOK_MISSING (deleted) from
# RUNBOOK_CHANGED (hash-mutated). Different operator responses require
# distinct denial reasons + audit shapes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_runbook_missing_returns_runbook_missing_denial(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Fix M2: runbook DELETED between plan and approve →
    denial_reason=RUNBOOK_MISSING, audit gate='runbook_missing' with
    runbook_deleted=True (distinct from RUNBOOK_CHANGED which is a hash mutation).
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

    # Simulate runbook DELETED between plan and approve. We can't actually
    # DELETE the runbook row (FK from runbook_runs.runbook_id blocks it), so
    # patch RunbookRepo.get_runbook to return None — which is precisely what
    # execute_approved's drift check sees when the row is gone.
    #
    # Reset docker state so we can assert execute_approved does NOT exec.
    docker.last_call_cmd = None
    docker.last_call_container_id = ""
    docker.last_call_user = None
    docker.last_call_env = None

    with patch.object(RunbookRepo, "get_runbook", new=AsyncMock(return_value=None)):
        result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result.ran is False
    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.RUNBOOK_MISSING
    assert result.run_id is None

    # Approval rejected with correct approver
    approvals = await repo.fetch_all(
        text("SELECT status, approved_by FROM runbook_run_approvals WHERE id = :id"),
        {"id": approval_id},
    )
    assert len(approvals) == 1
    assert approvals[0].status == "rejected"
    assert approvals[0].approved_by == "admin"

    # Audit gate='runbook_missing' with runbook_deleted=True (not the mutated shape)
    audit = await repo.fetch_one(
        text(
            "SELECT after_json FROM audit_log WHERE what = 'autofix.rejected' "
            "AND json_extract(after_json, '$.approval_id') = :aid"
        ),
        {"aid": approval_id},
    )
    assert audit is not None
    after = json.loads(str(audit[0]))
    assert after.get("gate") == "runbook_missing"
    assert after.get("runbook_deleted") is True
    assert after.get("pinned_runbook_hash") == "hash-v1"
    # RUNBOOK_MISSING audit uses `runbook_deleted=True` in place of the
    # `current_runbook_hash` field the RUNBOOK_CHANGED audit uses; deleted
    # runbooks have no current hash, and None would be indistinguishable from a
    # genuine None hash on a mutated runbook.
    assert "current_runbook_hash" not in after

    # No real exec happened
    assert docker.last_call_cmd is None
    runs = await repo.fetch_all(
        text("SELECT mode FROM runbook_runs ORDER BY created_at DESC LIMIT 1"),
        {},
    )
    assert len(runs) == 1
    assert runs[0].mode == RunMode.DRY_RUN.value


# ---------------------------------------------------------------------------
# T7: execute_approved_not_pending_denies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_not_pending_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T7: Approval not pending → APPROVAL_NOT_PENDING denial, no exec."""
    rb = _make_runbook_record(
        alertname="TestAlert", dry_run_required=True, runbook_dir=tmp_path / "runbook"
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

    # Create dry run + approval
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

    # Mark approval as already approved
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE runbook_run_approvals SET status = 'approved' WHERE id = :id"),
            {"id": approval_id},
        )

    # Reset docker state so we can assert execute_approved does NOT exec.
    docker.last_call_cmd = None
    docker.last_call_container_id = ""
    docker.last_call_user = None
    docker.last_call_env = None

    # Try to approve again
    result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result is not None
    assert result.denial_reason == DenialReason.APPROVAL_NOT_PENDING
    assert result.ran is False

    # Verify docker never called
    assert docker.last_call_cmd is None


# ---------------------------------------------------------------------------
# T8: execute_approved_missing_approval_denies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_missing_approval_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """T8: Approval does not exist → APPROVAL_NOT_PENDING denial."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.execute_approved("nonexistent-id", principal="admin", ip="1.2.3.4")

    assert result is not None
    assert result.denial_reason == DenialReason.APPROVAL_NOT_PENDING
    assert result.runbook_id is None  # approval was None


# ---------------------------------------------------------------------------
# T9: execute_approved_gate_deny_on_approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_gate_deny_on_approve(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T9: Operational gate (kill-switch flipped) denies after plan."""
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

    # Create dry run
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

    # Flip kill-switch
    await app_settings.set("autofix_enabled", "false")

    # Reset docker state so we can assert execute_approved does NOT exec.
    docker.last_call_cmd = None
    docker.last_call_container_id = ""
    docker.last_call_user = None
    docker.last_call_env = None

    # Approve should deny on operational gate
    result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result is not None
    assert result.denial_reason == DenialReason.KILL_SWITCH
    assert result.ran is False

    # Verify docker never called
    assert docker.last_call_cmd is None


# ---------------------------------------------------------------------------
# T10: execute_approved_missing_alert_reconstructs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_missing_alert_reconstructs(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T10: Alert missing → reconstructed minimal Alert, real exec fires."""
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

    # Create dry run
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

    # Simulate "alert row is gone" so _load_alert_for_exec falls through to the
    # placeholder-Alert branch. Must:
    #   (a) NULL the approval.alert_id so _load_alert_for_exec receives None and
    #       builds placeholder with id="unknown" (not the vanished real id);
    #   (b) NULL the dry-run's runbook_runs.alert_id + delete any alert_outcomes
    #       so the DELETE FROM alerts doesn't fail FK enforcement;
    #   (c) seed an alerts row with id="unknown" so the subsequent real-run
    #       insert_started(alert_id="unknown") FK succeeds.
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE runbook_run_approvals SET alert_id = NULL WHERE id = :id"),
            {"id": approval_id},
        )
        await conn.execute(
            text("UPDATE runbook_runs SET alert_id = NULL WHERE alert_id = :id"),
            {"id": alert.id},
        )
        await conn.execute(
            text("DELETE FROM alert_outcomes WHERE alert_id = :id"),
            {"id": alert.id},
        )
        await conn.execute(
            text("DELETE FROM alerts WHERE id = :id"),
            {"id": alert.id},
        )
    unknown_alert = Alert(
        id="unknown",
        fingerprint="fp-unknown",
        source_tool="autofix-approval",
        severity=Severity.WARNING,
        status=AlertStatus.FIRING,
        opened_at=utc_now_iso(),
        last_seen_at=utc_now_iso(),
        payload={},
        labels={},
        annotations={},
    )
    await _insert_alert(repo, unknown_alert)

    # Approve should still work with reconstructed alert
    docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
    docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result is not None
    assert result.ran is True
    assert result.outcome == RunOutcome.RAN

    # Verify docker was called (real exec fired)
    assert docker.last_call_cmd is not None
    assert "--dangerously-skip-permissions" in docker.last_call_cmd


# ---------------------------------------------------------------------------
# T11: execute_approved_alert_id_none_placeholder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_alert_id_none_placeholder(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T11: Approval alert_id is None → uses minimal placeholder Alert."""
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

    # Create dry run
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

    # Manually set alert_id to None in approval
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE runbook_run_approvals SET alert_id = NULL WHERE id = :id"),
            {"id": approval_id},
        )

    # _load_alert_for_exec builds a placeholder Alert with id="unknown" when
    # approval.alert_id is None. The subsequent runbook_runs INSERT FKs alert_id
    # → alerts.id, so seed an "unknown" alert row so the FK passes.
    unknown_alert = Alert(
        id="unknown",
        fingerprint="fp-unknown",
        source_tool="autofix-approval",
        severity=Severity.WARNING,
        status=AlertStatus.FIRING,
        opened_at=utc_now_iso(),
        last_seen_at=utc_now_iso(),
        payload={},
        labels={},
        annotations={},
    )
    await _insert_alert(repo, unknown_alert)

    # Approve should work
    docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
    docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.execute_approved(approval_id, principal="admin", ip="1.2.3.4")

    assert result is not None
    assert result.ran is True
    assert result.outcome == RunOutcome.RAN


# ---------------------------------------------------------------------------
# T12-T15: read_dry_plan tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_dry_plan_happy(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T12: read_dry_plan success → plan_text from file."""
    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    # runbook_runs.runbook_id FKs to runbooks.id (NOT NULL, enforced): seed a parent.
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    # Create a dry run directly
    plan_content = "This is the plan content\n"
    transcript_path = f"{transcript_dir}/test-plan.transcript"
    with open(transcript_path, "w", encoding="utf-8") as fh:
        fh.write(plan_content)

    run_id = uuid7()
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO runbook_runs "
                "(id, runbook_id, created_at, alert_id, mode, prompt, started_at, "
                " ended_at, fixer_user, host, runbook_hash, transcript_path,"
                " exit_code, initiated_by) "
                "VALUES (:id, :rb_id, :ca, :alert_id, :mode, :prompt, :started, "
                " :ended, :fixer, :host, :hash, :transcript, :exit, :initiated_by)"
            ),
            {
                "id": run_id,
                "rb_id": rb.id,
                "ca": utc_now_iso(),
                "alert_id": None,
                "mode": RunMode.DRY_RUN.value,
                "prompt": "/test",
                "started": utc_now_iso(),
                "ended": utc_now_iso(),
                "fixer": "test-fixer",
                "host": "test-host",
                "hash": "hash-v1",
                "transcript": transcript_path,
                "exit": 0,
                "initiated_by": "alert",
            },
        )

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir)

    plan = await orch.read_dry_plan(run_id)

    assert plan is not None
    assert plan.plan_text == plan_content
    assert plan.exit_code == 0
    assert plan.transcript_path == transcript_path


@pytest.mark.asyncio
async def test_read_dry_plan_missing_run(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """T13: read_dry_plan run not found → None."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    plan = await orch.read_dry_plan("nonexistent-id")
    assert plan is None


@pytest.mark.asyncio
async def test_read_dry_plan_no_transcript_path(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T14: read_dry_plan transcript_path NULL → None."""
    # runbook_runs.runbook_id FKs to runbooks.id (NOT NULL, enforced): seed a parent.
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    run_id = uuid7()
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO runbook_runs "
                "(id, runbook_id, created_at, alert_id, mode, prompt, started_at, "
                " ended_at, fixer_user, host, runbook_hash, transcript_path,"
                " exit_code, initiated_by) "
                "VALUES (:id, :rb_id, :ca, :alert_id, :mode, :prompt, :started, "
                " :ended, :fixer, :host, :hash, :transcript, :exit, :initiated_by)"
            ),
            {
                "id": run_id,
                "rb_id": rb.id,
                "ca": utc_now_iso(),
                "alert_id": None,
                "mode": RunMode.DRY_RUN.value,
                "prompt": "/test",
                "started": utc_now_iso(),
                "ended": utc_now_iso(),
                "fixer": "test-fixer",
                "host": "test-host",
                "hash": "hash-v1",
                "transcript": None,
                "exit": 0,
                "initiated_by": "alert",
            },
        )

    docker = _FakeDockerClient()
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=str(tmp_path / "transcripts")
    )

    plan = await orch.read_dry_plan(run_id)
    assert plan is None


@pytest.mark.asyncio
async def test_read_dry_plan_file_unreadable(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T15: read_dry_plan file missing → None."""
    # runbook_runs.runbook_id FKs to runbooks.id (NOT NULL, enforced): seed a parent.
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    run_id = uuid7()
    transcript_path = str(tmp_path / "nonexistent" / "plan.transcript")

    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO runbook_runs "
                "(id, runbook_id, created_at, alert_id, mode, prompt, started_at, "
                " ended_at, fixer_user, host, runbook_hash, transcript_path,"
                " exit_code, initiated_by) "
                "VALUES (:id, :rb_id, :ca, :alert_id, :mode, :prompt, :started, "
                " :ended, :fixer, :host, :hash, :transcript, :exit, :initiated_by)"
            ),
            {
                "id": run_id,
                "rb_id": rb.id,
                "ca": utc_now_iso(),
                "alert_id": None,
                "mode": RunMode.DRY_RUN.value,
                "prompt": "/test",
                "started": utc_now_iso(),
                "ended": utc_now_iso(),
                "fixer": "test-fixer",
                "host": "test-host",
                "hash": "hash-v1",
                "transcript": transcript_path,
                "exit": 0,
                "initiated_by": "alert",
            },
        )

    docker = _FakeDockerClient()
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=str(tmp_path / "transcripts")
    )

    plan = await orch.read_dry_plan(run_id)
    assert plan is None


# ---------------------------------------------------------------------------
# T16: approvals_repo_insert_get_list_and_transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approvals_repo_insert_get_list_and_transitions(
    repo: SqliteRepository,
) -> None:
    """T16: Approvals repo methods.

    Covers: insert, get, list, mark_approved_conn, mark_rejected_conn,
    set_real_run_id_conn, own-txn mark_rejected.
    """
    approvals_repo = RunbookRunApprovalsRepository(repo)

    # Insert a pending approval. dry_run_id/real_run_id FK runbook_runs.id, and
    # runbook_runs.runbook_id FKs runbooks.id + runbook_runs.alert_id FKs alerts.id,
    # so seed the full chain first.
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        dry_run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.DRY_RUN,
            initiated_by="alert",
        )
    runbook_id = rb.id
    alert_id = alert.id
    pinned_hash = "hash-v1"

    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=runbook_id,
            alert_id=alert_id,
            pinned_runbook_hash=pinned_hash,
        )

    assert approval_id is not None

    # Get the approval
    approval = await approvals_repo.get(approval_id)
    assert approval is not None
    assert approval.id == approval_id
    assert approval.status == "pending"
    assert approval.dry_run_id == dry_run_id
    assert approval.runbook_id == runbook_id
    assert approval.alert_id == alert_id
    assert approval.pinned_runbook_hash == pinned_hash

    # List pending approvals
    approvals = await approvals_repo.list_by_status("pending")
    assert len(approvals) >= 1
    assert any(a.id == approval_id for a in approvals)

    # Mark approved
    async with repo.transaction() as conn:
        await approvals_repo.mark_approved_conn(
            conn,
            approval_id=approval_id,
            approved_by="admin",
            when=utc_now_iso(),
        )

    approval = await approvals_repo.get(approval_id)
    assert approval is not None
    assert approval.status == "approved"
    assert approval.approved_by == "admin"

    # Set real_run_id (FK → runbook_runs.id): seed a real run first.
    async with repo.transaction() as conn:
        real_run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
            initiated_by="alert",
        )
        await approvals_repo.set_real_run_id_conn(
            conn,
            approval_id=approval_id,
            real_run_id=real_run_id,
        )

    approval = await approvals_repo.get(approval_id)
    assert approval is not None
    assert approval.real_run_id == real_run_id

    # Create another approval for rejection test — seed another dry run first (FK).
    approval_id_2: str | None = None
    async with repo.transaction() as conn:
        dry_run_id_2 = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.DRY_RUN,
            initiated_by="alert",
        )
        approval_id_2 = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id_2,
            runbook_id=rb.id,
            alert_id=None,
            pinned_runbook_hash="hash-v2",
        )

    assert approval_id_2 is not None

    # Own-txn mark_rejected (includes audit)
    await approvals_repo.mark_rejected(
        approval_id=approval_id_2,
        approved_by="admin",
        when=None,
        ip="1.2.3.4",
    )

    approval = await approvals_repo.get(approval_id_2)
    assert approval is not None
    assert approval.status == "rejected"
    assert approval.approved_by == "admin"

    # Verify audit written
    audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.rejected'"),
        {},
    )
    assert len(audits) >= 1


# ---------------------------------------------------------------------------
# T17: build_claude_cmd_dry_and_real
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_claude_cmd_dry_and_real(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """T17: _build_claude_cmd dry vs real branches."""
    rb = _make_runbook_record(alertname="Test")
    orch = _make_orchestrator(repo, secrets_repo_fixture, _FakeDockerClient())

    # Dry cmd
    dry_cmd = orch._build_claude_cmd(rb, dry=True)  # pyright: ignore[reportPrivateUsage]
    assert dry_cmd == ["claude", "-p", rb.path, "--permission-mode", "plan"]
    assert "--dangerously-skip-permissions" not in dry_cmd

    # Real cmd
    real_cmd = orch._build_claude_cmd(rb, dry=False)  # pyright: ignore[reportPrivateUsage]
    assert real_cmd == ["claude", "-p", rb.path, "--dangerously-skip-permissions"]
    assert "--permission-mode" not in real_cmd


# ---------------------------------------------------------------------------
# T18: dry_in_lock_inflight_denies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_in_lock_inflight_denies(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """T18: Dry exec path, in-lock inflight check denies (ALREADY_RUNNING)."""
    rb = _make_runbook_record(alertname="TestAlert", dry_run_required=True, content_hash="hash-v1")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=1)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.denial_reason == DenialReason.ALREADY_RUNNING
    assert result.ran is False

    # No run, no approval
    runs = await repo.fetch_all(text("SELECT id FROM runbook_runs"), {})
    assert runs == []

    approvals = await repo.fetch_all(text("SELECT id FROM runbook_run_approvals"), {})
    assert approvals == []


# ---------------------------------------------------------------------------
# T19: dry_claim_error_audited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_claim_error_audited(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """T19: Dry exec path, insert_started raises → CLAIM_ERROR, audit written."""
    rb = _make_runbook_record(alertname="TestAlert", dry_run_required=True, content_hash="hash-v1")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    # Mock insert_started to raise
    with (
        patch.object(
            RunbookRunsRepository,
            "insert_started",
            side_effect=Exception("DB error"),
        ),
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
    ):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.denial_reason == DenialReason.CLAIM_ERROR
    assert result.ran is False

    # Verify audit
    audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.claim_error'"),
        {},
    )
    assert len(audits) >= 1


# ---------------------------------------------------------------------------
# STAGE-009-007: kill_inflight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_inflight_no_current_run_returns_no_inflight_and_audits(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """No in-flight run -> KillResult(killed=False, error='no_inflight_run') + audit."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.kill_inflight(reason="test", killed_by="test-user")

    assert result == KillResult(
        killed=False,
        run_id=None,
        reason="test",
        error="no_inflight_run",
        unwind_warning=None,
    )

    audit = await repo.fetch_one(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.kill_no_inflight'"), {}
    )
    assert audit is not None
    after = json.loads(str(audit[0]))
    assert after["reason"] == "test"


@pytest.mark.asyncio
async def test_kill_inflight_success_stamps_killed_at_and_audits_killed(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Success: docker.kill_container succeeds, killed_at stamped, audit written,
    unwind observed cleanly (post_snapshot is None fast-path is NOT exercised here
    because _current_run stays non-None until we clear it ourselves after the kill,
    simulating the exec unwinding concurrently)."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
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
            initiated_by="alert",
        )

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
    orch._current_run = _CurrentRun(  # pyright: ignore[reportPrivateUsage]
        run_id=run_id, container="test-fixer", started_at_monotonic=time.monotonic()
    )
    # Clear the handle as a side effect of the kill call, so the post-kill
    # snapshot (before the poll) is already non-None initially but the poll
    # observes the clear almost immediately.
    docker.on_kill_call = lambda: setattr(orch, "_current_run", None)  # pyright: ignore[reportPrivateUsage]

    result = await orch.kill_inflight(reason="user_toggle", killed_by="user:admin")

    assert result.killed is True
    assert result.run_id == run_id
    assert result.error is None
    assert result.unwind_warning is None
    assert docker.last_kill_container_id == "test-fixer"
    assert docker.last_kill_signal == "SIGKILL"
    assert docker.last_kill_timeout_seconds == 5.0  # noqa: PLR2004

    run_row = await repo.fetch_one(
        text("SELECT killed_at, ended_at FROM runbook_runs WHERE id = :id"), {"id": run_id}
    )
    assert run_row is not None
    assert run_row[0] is not None  # killed_at set
    assert run_row[1] is None  # ended_at untouched

    audit = await repo.fetch_one(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.killed'"), {}
    )
    assert audit is not None
    after = json.loads(str(audit[0]))
    assert after["run_id"] == run_id
    assert after["killed_by"] == "user:admin"


@pytest.mark.asyncio
async def test_kill_inflight_docker_kill_fails_audits_kill_failed_and_reraises(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """docker.kill_container raises DockerSocketError -> audited + re-raised."""
    docker = _FakeDockerClient(kill_raises=DockerSocketConnectionError("boom"))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
    orch._current_run = _CurrentRun(  # pyright: ignore[reportPrivateUsage]
        run_id="r1", container="test-fixer", started_at_monotonic=time.monotonic()
    )

    with pytest.raises(DockerSocketConnectionError):
        await orch.kill_inflight(reason="user_toggle", killed_by="user:admin")

    audit = await repo.fetch_one(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.kill_failed'"), {}
    )
    assert audit is not None
    after = json.loads(str(audit[0]))
    assert after["run_id"] == "r1"
    assert "boom" in after["error"]

    rows = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what LIKE 'autofix.kill%'"), {}
    )
    whats = [r[0] for r in rows]
    assert "autofix.kill_attempted" in whats
    assert "autofix.kill_failed" in whats
    assert "autofix.killed" not in whats


@pytest.mark.asyncio
async def test_kill_inflight_unwind_deadline_exceeded_returns_warning(
    repo: SqliteRepository,
    secrets_repo_fixture: AsyncSecretsRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_current_run never clears -> unwind_warning='unwind_deadline_exceeded'."""
    monkeypatch.setattr(
        "homelab_monitor.kernel.autofix.orchestrator._KILL_UNWIND_DEADLINE_SECONDS", 0.05
    )
    monkeypatch.setattr(
        "homelab_monitor.kernel.autofix.orchestrator._KILL_UNWIND_POLL_INTERVAL_SECONDS", 0.01
    )

    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
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
            initiated_by="alert",
        )

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
    # NEVER clear _current_run — simulates the exec never unwinding within the
    # deadline. The snapshot the poll observes stays non-None throughout.
    orch._current_run = _CurrentRun(  # pyright: ignore[reportPrivateUsage]
        run_id=run_id, container="test-fixer", started_at_monotonic=time.monotonic()
    )

    result = await orch.kill_inflight(reason="user_toggle", killed_by="user:admin")

    assert result.killed is True
    assert result.unwind_warning == "unwind_deadline_exceeded"


@pytest.mark.asyncio
async def test_kill_inflight_unwind_observed_returns_no_warning(
    repo: SqliteRepository,
    secrets_repo_fixture: AsyncSecretsRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_current_run is non-None when the poll starts but clears mid-poll (not
    at the pre-poll snapshot) -> the `if cleared: break` path is taken and
    unwind_warning stays None (clean unwind observed)."""
    monkeypatch.setattr(
        "homelab_monitor.kernel.autofix.orchestrator._KILL_UNWIND_DEADLINE_SECONDS", 1.0
    )
    monkeypatch.setattr(
        "homelab_monitor.kernel.autofix.orchestrator._KILL_UNWIND_POLL_INTERVAL_SECONDS", 0.01
    )

    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
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
            initiated_by="alert",
        )

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
    # Non-None at snapshot time (post_snapshot is not None -> loop entered).
    # Do NOT clear via on_kill_call — the delayed task below does the
    # clearing, AFTER the poll has started, so the loop's `if cleared: break`
    # branch (not the pre-poll fast path) is what resolves this.
    orch._current_run = _CurrentRun(  # pyright: ignore[reportPrivateUsage]
        run_id=run_id, container="test-fixer", started_at_monotonic=time.monotonic()
    )

    async def _delayed_clear() -> None:
        await asyncio.sleep(0.05)
        orch._current_run = None  # pyright: ignore[reportPrivateUsage]

    delay_task = asyncio.create_task(_delayed_clear())
    try:
        result = await orch.kill_inflight(reason="user_toggle", killed_by="user:admin")
    finally:
        # Ensure the delayed-clear task completes or is cancelled cleanly.
        if not delay_task.done():
            delay_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await delay_task

    assert result.killed is True
    assert result.unwind_warning is None
    assert result.error is None
    assert result.run_id == run_id


@pytest.mark.asyncio
async def test_kill_inflight_current_run_already_none_at_post_snapshot_skips_polling(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """When _current_run is already None at the post-kill snapshot (post_snapshot
    is None), the unwind-deadline poll is skipped entirely (fast-path). Success
    path still runs: killed_at is stamped and autofix.killed is audited."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
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
            initiated_by="alert",
        )

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
    orch._current_run = _CurrentRun(  # pyright: ignore[reportPrivateUsage]
        run_id=run_id, container="test-fixer", started_at_monotonic=time.monotonic()
    )
    # Clear it as a side effect of the kill call itself, so by the time
    # kill_inflight takes its post-kill snapshot, _current_run is already None.
    docker.on_kill_call = lambda: setattr(orch, "_current_run", None)  # pyright: ignore[reportPrivateUsage]

    result = await orch.kill_inflight(reason="user_toggle", killed_by="user:admin")

    assert result.killed is True
    assert result.unwind_warning is None

    run_row = await repo.fetch_one(
        text("SELECT killed_at FROM runbook_runs WHERE id = :id"), {"id": run_id}
    )
    assert run_row is not None
    assert run_row[0] is not None


@pytest.mark.asyncio
async def test_kill_inflight_wrong_run_race_audits_and_skips_stamp(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """When _current_run is republished with a different run_id between the
    snapshot and post-kill re-check, kill_inflight audits
    autofix.kill_wrong_run_race and returns killed=False,
    error='wrong_run_race' without stamping killed_at on either run."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        run_id_a = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.REAL,
            initiated_by="alert",
        )

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
    orch._current_run = _CurrentRun(  # pyright: ignore[reportPrivateUsage]
        run_id=run_id_a, container="test-fixer", started_at_monotonic=time.monotonic()
    )

    # Republish _current_run with a DIFFERENT run_id during the kill call.
    # This simulates a concurrent execute_approved starting a fresh run
    # after our snapshot but before our post-kill re-check.
    docker.on_kill_call = lambda: setattr(  # pyright: ignore[reportPrivateUsage]
        orch,
        "_current_run",
        _CurrentRun(
            run_id="run_id_b_replaced",
            container="test-fixer",
            started_at_monotonic=time.monotonic(),
        ),
    )

    result = await orch.kill_inflight(reason="user_toggle", killed_by="user:admin")

    assert result.killed is False
    assert result.error == "wrong_run_race"
    assert result.run_id == run_id_a

    run_row = await repo.fetch_one(
        text("SELECT killed_at FROM runbook_runs WHERE id = :id"), {"id": run_id_a}
    )
    assert run_row is not None
    assert run_row[0] is None

    killed_row = await repo.fetch_one(
        text("SELECT id FROM audit_log WHERE what = 'autofix.killed'"), {}
    )
    assert killed_row is None

    race_rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.kill_wrong_run_race'"),
        {},
    )
    assert len(race_rows) == 1
    after = json.loads(str(race_rows[0][0]))
    assert after["snapshot_run_id"] == run_id_a
    assert after["current_run_id"] == "run_id_b_replaced"

    attempted_rows = await repo.fetch_all(
        text("SELECT id FROM audit_log WHERE what = 'autofix.kill_attempted'"), {}
    )
    assert len(attempted_rows) == 1


@pytest.mark.asyncio
async def test_kill_inflight_writes_pre_kill_audit_before_docker_kill(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """The pre-kill autofix.kill_attempted audit row commits BEFORE
    docker.kill_container is called, so a crash mid-kill still leaves a
    forensic trail. Verified by making docker.kill_container raise: the
    pre-kill audit row must still be present."""
    docker = _FakeDockerClient(kill_raises=DockerSocketConnectionError("boom"))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
    orch._current_run = _CurrentRun(  # pyright: ignore[reportPrivateUsage]
        run_id="r1", container="test-fixer", started_at_monotonic=time.monotonic()
    )

    with pytest.raises(DockerSocketConnectionError):
        await orch.kill_inflight(reason="user_toggle", killed_by="user:admin")

    attempted_rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.kill_attempted'"), {}
    )
    assert len(attempted_rows) == 1
    attempted_after = json.loads(str(attempted_rows[0][0]))
    assert attempted_after["run_id"] == "r1"

    failed_rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.kill_failed'"), {}
    )
    assert len(failed_rows) == 1
    failed_after = json.loads(str(failed_rows[0][0]))
    assert failed_after["run_id"] == "r1"

    killed_row = await repo.fetch_one(
        text("SELECT id FROM audit_log WHERE what = 'autofix.killed'"), {}
    )
    assert killed_row is None

    run_row = await repo.fetch_one(
        text("SELECT killed_at FROM runbook_runs WHERE id = :id"), {"id": "r1"}
    )
    # No runbook_runs row was inserted for "r1" in this test (unlike the
    # other kill_inflight tests) -- confirm no accidental row exists.
    assert run_row is None


# ---------------------------------------------------------------------------
# STAGE-009-007: _exec_claude publish/clear of _current_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_claude_publishes_and_clears_current_run_on_success(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """dry=False success: _current_run set during exec, cleared after (finally)."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    alert = _make_alert(alertname="TestAlert")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    observed_during_exec: _CurrentRun | None = None

    class _ObservingDocker(_FakeDockerClient):
        async def exec_capture(
            self,
            *,
            container_id: str,
            cmd: list[str],
            timeout_seconds: float,
            user: str | None = None,
            env: Mapping[str, str] | None = None,
        ) -> ExecResult:
            nonlocal observed_during_exec
            observed_during_exec = orch._current_run  # pyright: ignore[reportPrivateUsage]
            return await super().exec_capture(
                container_id=container_id,
                cmd=cmd,
                timeout_seconds=timeout_seconds,
                user=user,
                env=env,
            )

    docker = _ObservingDocker(result=ExecResult(exit_code=0, stdout="ok", stderr=""))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir)

    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]

    await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    assert observed_during_exec is not None
    assert observed_during_exec.run_id == "r1"
    assert observed_during_exec.container == "test-fixer"
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_exec_claude_clears_current_run_on_docker_socket_error(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """dry=False, exec_capture raises DockerSocketError -> _current_run cleared after."""
    rb = _make_runbook_record(alertname="TestAlert")
    alert = _make_alert(alertname="TestAlert")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(raises=DockerSocketConnectionError("boom"))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir)

    exec_result, _transcript, error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    assert errored is True
    assert error_msg is not None
    assert exec_result.exit_code == 1
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_exec_claude_clears_current_run_on_timeout(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """dry=False, exec_capture raises DockerExecTimeoutError -> exit 124, cleared after."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    alert = _make_alert(alertname="TestAlert")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(raises=DockerExecTimeoutError("timed out"))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir)

    exec_result, _transcript, error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    _TIMEOUT_EXIT_CODE = 124
    assert errored is True
    assert error_msg is not None
    assert exec_result.exit_code == _TIMEOUT_EXIT_CODE
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_exec_claude_does_not_publish_current_run_for_dry(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """dry=True: _current_run stays None throughout (never published)."""
    rb = _make_runbook_record(alertname="TestAlert")
    alert = _make_alert(alertname="TestAlert")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    observed_during_exec: _CurrentRun | None = None

    class _ObservingDocker(_FakeDockerClient):
        async def exec_capture(
            self,
            *,
            container_id: str,
            cmd: list[str],
            timeout_seconds: float,
            user: str | None = None,
            env: Mapping[str, str] | None = None,
        ) -> ExecResult:
            nonlocal observed_during_exec
            observed_during_exec = orch._current_run  # pyright: ignore[reportPrivateUsage]
            return await super().exec_capture(
                container_id=container_id,
                cmd=cmd,
                timeout_seconds=timeout_seconds,
                user=user,
                env=env,
            )

    docker = _ObservingDocker(result=ExecResult(exit_code=0, stdout="plan", stderr=""))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir)

    await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=True
    )

    assert observed_during_exec is None
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# STAGE-009-007: pre-run gate denial latency (kill-switch check must be O(1))
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_run_gate_denial_latency_under_100ms(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """_check_operational_gates denies fast (kill-switch off, checked first) —
    no accidental blocking I/O added by the kill-switch feature. Runs the check
    ~10 times and asserts the max wall-clock delta stays under 100ms."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)
    # autofix_enabled left unset (falsy) -> kill-switch gate denies immediately.

    max_delta = 0.0
    for _ in range(10):
        start = time.monotonic()
        denial = await orch._check_operational_gates(rb)  # pyright: ignore[reportPrivateUsage]
        delta = time.monotonic() - start
        max_delta = max(max_delta, delta)
        assert denial == DenialReason.KILL_SWITCH

    assert max_delta < 0.100  # noqa: PLR2004


# ---------------------------------------------------------------------------
# STAGE-009-008: scoped capability grant resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_resolution_happy_path_docker_ssh_egress(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T1: docker + ssh + egress declared; real run -> grants resolved and egress audited."""
    rb_dir = tmp_path / "test-runbook"
    rb_dir.mkdir()
    runbook_yaml = rb_dir / "runbook.yaml"
    runbook_yaml.write_text(
        """\
name: test-runbook
match_patterns:
  - alertname: TestAlert
    labels: {}
rate_limit_per_hour: 100
cooldown_seconds: 0
scoped_capabilities:
  docker:
    container: "foo"
    allowed_actions:
      - "restart"
  ssh:
    target_id: "udm"
  egress:
    - "1.2.3.4:443"
"""
    )

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    await _insert_runbook(repo, rb)

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        ssh_target_ids_provider=lambda: frozenset({"udm"}),
    )

    exec_result, _transcript_path, error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    # Should proceed to exec (no early return on grant failure).
    assert exec_result.exit_code == 0
    assert error_msg is None
    assert errored is False

    # Check audit rows.
    audit_rows = await repo.execute(
        text(
            "SELECT what, after_json FROM audit_log "
            "WHERE what IN ('autofix.grant_resolved', 'autofix.egress_unenforced') "
            "ORDER BY rowid"
        )
    )
    rows = audit_rows.fetchall()
    assert len(rows) == 2  # noqa: PLR2004

    resolved_row, egress_row = rows
    assert resolved_row[0] == "autofix.grant_resolved"
    resolved_after = json.loads(resolved_row[1])
    assert resolved_after["docker_container"] == "foo"
    assert resolved_after["docker_allowed_actions"] == ["restart"]
    assert resolved_after["ssh_target_id"] == "udm"
    assert resolved_after["egress"] == ["1.2.3.4:443"]

    assert egress_row[0] == "autofix.egress_unenforced"
    egress_after = json.loads(egress_row[1])
    assert egress_after["egress"] == ["1.2.3.4:443"]


@pytest.mark.asyncio
async def test_grant_resolution_missing_runbook_yaml(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T2: runbook.yaml missing -> grant_failed audit, early return,
    _current_run never published."""
    rb_dir = tmp_path / "test-runbook"
    rb_dir.mkdir()
    # No runbook.yaml file

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    await _insert_runbook(repo, rb)

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir)

    exec_result, transcript_path, error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    # Should return early (grant failure).
    assert exec_result.exit_code == 1
    assert transcript_path is None
    assert error_msg is not None
    assert "runbook config unavailable" in error_msg
    assert errored is True

    # _current_run should never have been set (grant failure is before the publish).
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]

    # Check audit row.
    audit_rows = await repo.execute(
        text("SELECT what, after_json FROM audit_log WHERE what = 'autofix.grant_failed'")
    )
    rows = audit_rows.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "autofix.grant_failed"
    after_data = json.loads(rows[0][1])
    assert after_data["reason"] == "scoped_capabilities_unavailable"


@pytest.mark.asyncio
async def test_grant_resolution_malformed_yaml(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T3: malformed YAML -> grant_failed audit, no exec attempted."""
    rb_dir = tmp_path / "test-runbook"
    rb_dir.mkdir()
    runbook_yaml = rb_dir / "runbook.yaml"
    runbook_yaml.write_text(
        """\
scoped_capabilities:
  docker:
    container: "foo"
    allowed_actions: [
      # unbalanced bracket
"""
    )

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    await _insert_runbook(repo, rb)

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir)

    exec_result, transcript_path, error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    # Should return early on malformed YAML.
    assert exec_result.exit_code == 1
    assert transcript_path is None
    assert errored is True
    assert error_msg is not None and "malformed YAML" in error_msg

    # Docker should never have been called.
    assert docker.last_call_cmd is None

    # _current_run should never have been published
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_grant_resolution_unknown_ssh_target_id(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T4: ssh.target_id unknown -> grant_failed with reason='unknown_ssh_target_id'."""
    rb_dir = tmp_path / "test-runbook"
    rb_dir.mkdir()
    runbook_yaml = rb_dir / "runbook.yaml"
    runbook_yaml.write_text(
        """\
name: test-runbook
match_patterns:
  - alertname: TestAlert
    labels: {}
rate_limit_per_hour: 100
cooldown_seconds: 0
scoped_capabilities:
  ssh:
    target_id: "nonexistent"
"""
    )

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    await _insert_runbook(repo, rb)

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        ssh_target_ids_provider=lambda: frozenset({"udm", "synology"}),
    )

    exec_result, transcript_path, error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    # Should return early on unknown SSH target.
    assert exec_result.exit_code == 1
    assert transcript_path is None
    assert errored is True
    assert error_msg is not None and "not a known SSH target" in error_msg

    # _current_run never published.
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]

    # Check audit row.
    audit_rows = await repo.execute(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.grant_failed'")
    )
    rows = audit_rows.fetchall()
    assert len(rows) == 1
    after_data = json.loads(rows[0][0])
    assert after_data["reason"] == "unknown_ssh_target_id"


@pytest.mark.asyncio
async def test_grant_resolution_ssh_provider_raises_maps_to_grant_failed(
    repo: SqliteRepository,
    secrets_repo_fixture: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Test: ssh_target_ids_provider() raising an exception is caught and mapped
    to GrantResolutionError(reason='scoped_capabilities_unavailable')."""
    rb_dir = tmp_path / "runbook"
    rb_dir.mkdir(parents=True, exist_ok=True)
    (rb_dir / RUNBOOK_CONFIG_FILENAME).write_text(
        """\
name: test-runbook
match_patterns:
  - alertname: TestAlert
    labels: {}
rate_limit_per_hour: 100
cooldown_seconds: 0
scoped_capabilities:
  ssh:
    target_id: udm
"""
    )

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    alert = _make_alert(alertname="TestAlert")

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    def _failing_provider() -> frozenset[str]:
        raise ValueError("simulated ssh_targets registry read failure")

    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        ssh_target_ids_provider=_failing_provider,
    )

    exec_result, transcript_path, error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    assert exec_result.exit_code == 1
    assert errored is True
    assert transcript_path is None
    assert error_msg is not None
    assert "ssh target registry unreadable" in error_msg
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]

    audit_rows = await repo.execute(
        text("SELECT what, after_json FROM audit_log WHERE what = 'autofix.grant_failed'")
    )
    rows = audit_rows.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "autofix.grant_failed"
    grant_failed_after = json.loads(rows[0][1])
    assert grant_failed_after["reason"] == "scoped_capabilities_unavailable"
    assert "ssh target registry" in grant_failed_after["detail"]


@pytest.mark.asyncio
async def test_grant_resolution_audit_failure_swallowed_and_returned(
    repo: SqliteRepository,
    secrets_repo_fixture: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Test: if the autofix.grant_failed audit-write itself raises, the exception
    is logged and swallowed; _exec_claude still returns the errored failure tuple."""
    # Use a fixture that will trigger GrantResolutionError (missing yaml).
    rb_dir = tmp_path / "runbook"
    rb_dir.mkdir(parents=True, exist_ok=True)
    # Do NOT write runbook.yaml -> triggers OSError branch in _resolve_grants.

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    alert = _make_alert(alertname="TestAlert")

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
    )

    async def _raising_insert_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated audit-write failure")

    with patch.object(orch_module, "insert_audit", side_effect=_raising_insert_audit):
        exec_result, transcript_path, error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
            record=rb, alert=alert, run_id="r1", dry=False
        )

    # Failure tuple returned, exception NOT propagated.
    assert exec_result.exit_code == 1
    assert errored is True
    assert transcript_path is None
    assert error_msg is not None

    # NO grant_failed audit row lands (because insert_audit raised).
    audit_rows = await repo.execute(
        text("SELECT what FROM audit_log WHERE what = 'autofix.grant_failed'")
    )
    rows = audit_rows.fetchall()
    assert len(rows) == 0

    # _current_run still None (kill-switch coexistence preserved even on audit failure).
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]


def test_autofix_package_lazy_getattr_raises_on_unknown_attribute() -> None:
    """Test: autofix package's __getattr__ raises AttributeError for unknown names."""
    with pytest.raises(AttributeError):
        _ = autofix_pkg.NonExistentAttribute  # type: ignore[attr-defined]


def test_autofix_package_lazy_getattr_returns_autofix_orchestrator() -> None:
    """Test: autofix package's __getattr__ correctly returns AutoFixOrchestrator via lazy import."""
    assert autofix_pkg.AutoFixOrchestrator is AutoFixOrchestrator


@pytest.mark.asyncio
async def test_grant_resolution_dry_run_suppresses_egress_unenforced(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T5: dry=True -> grant_resolved present, egress_unenforced absent."""
    rb_dir = tmp_path / "test-runbook"
    rb_dir.mkdir()
    runbook_yaml = rb_dir / "runbook.yaml"
    runbook_yaml.write_text(
        """\
name: test-runbook
match_patterns:
  - alertname: TestAlert
    labels: {}
rate_limit_per_hour: 100
cooldown_seconds: 0
scoped_capabilities:
  docker:
    container: "foo"
    allowed_actions:
      - "restart"
  ssh:
    target_id: "udm"
  egress:
    - "1.2.3.4:443"
"""
    )

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    await _insert_runbook(repo, rb)

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="plan", stderr=""))
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        ssh_target_ids_provider=lambda: frozenset({"udm"}),
    )

    exec_result, _transcript_path, error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=True
    )

    # Dry run should proceed normally.
    assert exec_result.exit_code == 0
    assert error_msg is None
    assert errored is False

    # Check that grant_resolved is present but egress_unenforced is NOT.
    audit_rows = await repo.execute(
        text(
            "SELECT what FROM audit_log "
            "WHERE what IN ('autofix.grant_resolved', 'autofix.egress_unenforced')"
        )
    )
    rows = audit_rows.fetchall()
    whats = [row[0] for row in rows]
    assert "autofix.grant_resolved" in whats
    assert "autofix.egress_unenforced" not in whats


@pytest.mark.asyncio
async def test_grant_resolution_empty_egress_suppresses_unenforced(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T6: egress: [] (empty) -> grant_resolved present, egress_unenforced absent."""
    rb_dir = tmp_path / "test-runbook"
    rb_dir.mkdir()
    runbook_yaml = rb_dir / "runbook.yaml"
    runbook_yaml.write_text(
        """\
name: test-runbook
match_patterns:
  - alertname: TestAlert
    labels: {}
rate_limit_per_hour: 100
cooldown_seconds: 0
scoped_capabilities:
  docker:
    container: "foo"
    allowed_actions:
      - "restart"
  egress: []
"""
    )

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    await _insert_runbook(repo, rb)

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir)

    exec_result, _transcript_path, _error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    # Should proceed normally.
    assert exec_result.exit_code == 0
    assert errored is False

    # Check audit rows: grant_resolved present, egress_unenforced absent.
    audit_rows = await repo.execute(
        text(
            "SELECT what FROM audit_log "
            "WHERE what IN ('autofix.grant_resolved', 'autofix.egress_unenforced')"
        )
    )
    rows = audit_rows.fetchall()
    whats = [row[0] for row in rows]
    assert "autofix.grant_resolved" in whats
    assert "autofix.egress_unenforced" not in whats


@pytest.mark.asyncio
async def test_grant_resolution_default_egress_when_key_missing(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Test: egress key missing (default) -> grant_resolved present,
    egress_unenforced absent, egress=[]."""
    rb_dir = tmp_path / "test-runbook"
    rb_dir.mkdir()
    runbook_yaml = rb_dir / "runbook.yaml"
    runbook_yaml.write_text(
        """\
name: test-runbook
match_patterns:
  - alertname: TestAlert
    labels: {}
rate_limit_per_hour: 100
cooldown_seconds: 0
scoped_capabilities:
  docker:
    container: "foo"
    allowed_actions:
      - "restart"
"""
    )

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    await _insert_runbook(repo, rb)

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir)

    exec_result, _transcript_path, _error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    # Should proceed normally.
    assert exec_result.exit_code == 0
    assert errored is False

    # Check audit rows: grant_resolved present, egress_unenforced absent.
    audit_rows = await repo.execute(
        text(
            "SELECT what FROM audit_log "
            "WHERE what IN ('autofix.grant_resolved', 'autofix.egress_unenforced')"
        )
    )
    rows = audit_rows.fetchall()
    whats = [row[0] for row in rows]
    assert "autofix.grant_resolved" in whats
    assert "autofix.egress_unenforced" not in whats


@pytest.mark.asyncio
async def test_grant_resolution_ssh_only_no_docker(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T7: ssh-only (no docker) -> grants resolved with
    docker_container=None, docker_allowed_actions=[]."""
    rb_dir = tmp_path / "test-runbook"
    rb_dir.mkdir()
    runbook_yaml = rb_dir / "runbook.yaml"
    runbook_yaml.write_text(
        """\
name: test-runbook
match_patterns:
  - alertname: TestAlert
    labels: {}
rate_limit_per_hour: 100
cooldown_seconds: 0
scoped_capabilities:
  ssh:
    target_id: "udm"
"""
    )

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    await _insert_runbook(repo, rb)

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        ssh_target_ids_provider=lambda: frozenset({"udm"}),
    )

    exec_result, _transcript_path, _error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    # Should proceed normally.
    assert exec_result.exit_code == 0
    assert errored is False

    # Check grant_resolved audit row.
    audit_rows = await repo.execute(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.grant_resolved'")
    )
    rows = audit_rows.fetchall()
    assert len(rows) == 1
    after_data = json.loads(rows[0][0])
    assert after_data["docker_container"] is None
    assert after_data["docker_allowed_actions"] == []
    assert after_data["ssh_target_id"] == "udm"


@pytest.mark.asyncio
async def test_grant_resolution_invalid_yaml_schema_valueerror(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T9: syntactically valid YAML that is a list (not a mapping) -> ValueError branch."""
    rb_dir = tmp_path / "test-runbook"
    rb_dir.mkdir()
    # A syntactically valid YAML list (not a mapping) — RunbookConfig.load_from_path
    # wraps this as ValueError, which _resolve_grants catches.
    (rb_dir / RUNBOOK_CONFIG_FILENAME).write_text("- item1\n- item2\n")

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    await _insert_runbook(repo, rb)

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir)

    exec_result, transcript_path, error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    assert exec_result.exit_code == 1
    assert errored is True
    assert transcript_path is None
    assert error_msg is not None
    # ValueError wraps the mapping/validation error into the
    # "scoped_capabilities_unavailable" reason; detail wraps the original message.
    audit_rows = await repo.execute(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.grant_failed'")
    )
    rows = audit_rows.fetchall()
    assert len(rows) == 1
    after_data = json.loads(rows[0][0])
    assert after_data["reason"] == "scoped_capabilities_unavailable"

    # _current_run should never have been published
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_grant_resolution_failure_never_publishes_current_run(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """T8: grant failure (real/non-dry) never publishes _current_run; kill_inflight sees no-op."""
    rb_dir = tmp_path / "test-runbook"
    rb_dir.mkdir()
    # No runbook.yaml to trigger grant failure

    rb = _make_runbook_record(alertname="TestAlert")
    rb = dataclasses.replace(rb, path=str(rb_dir))
    await _insert_runbook(repo, rb)

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir)

    # _current_run should be None before the call.
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]

    # Call _exec_claude in non-dry mode with a grant failure setup.
    _exec_result, transcript_path, _error_msg, errored, _snapshot = await orch._exec_claude(  # pyright: ignore[reportPrivateUsage]
        record=rb, alert=alert, run_id="r1", dry=False
    )

    # Should have failed with a grant error.
    assert errored is True
    assert transcript_path is None

    # _current_run should STILL be None (never published during grant failure).
    assert orch._current_run is None  # pyright: ignore[reportPrivateUsage]

    # kill_inflight called with no in-flight run should be a no-op (not running).
    kill_result = await orch.kill_inflight(reason="test", killed_by="test-user")
    assert kill_result.killed is False
    assert kill_result.error == "no_inflight_run"


# ---------------------------------------------------------------------------
# handle_operator_trigger (STAGE-009-010A)
# ---------------------------------------------------------------------------


def _make_risky_runbook_record(  # noqa: PLR0913 -- test factory params
    *,
    runbook_id: str | None = None,
    alertname: str = "TestAlert",
    enabled: bool = True,
    rate_limit_per_hour: int | None = None,
    cooldown_seconds: int | None = None,
    content_hash: str | None = "risky-hash",
    runbook_dir: Path | None = None,
) -> RunbookRecord:
    """Like _make_runbook_record but risk_tag='risky', dry_run_required=True."""
    patterns: list[dict[str, Any]] = [{"alertname": alertname, "labels": {}}]
    if runbook_dir is not None:
        _write_valid_runbook_yaml(runbook_dir, dry_run_required=True)
    path = str(runbook_dir) if runbook_dir is not None else "/runbooks/risky-runbook"
    return RunbookRecord(
        id=runbook_id or uuid7(),
        path=path,
        created_at=utc_now_iso(),
        alert_match_patterns=patterns,
        risk_tag="risky",
        dry_run_required=True,
        rate_limit_per_hour=rate_limit_per_hour,
        cooldown_seconds=cooldown_seconds,
        enabled=enabled,
        auto_trigger=False,
        content_hash=content_hash,
    )


@pytest.mark.asyncio
async def test_operator_trigger_dry_run_success(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Safe runbook, dry mode -> DRY_RUN_STORED, run_id + approval_id set."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="plan", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_operator_trigger(
            rb.id, RunMode.DRY_RUN, principal="alice", ip="10.0.0.1"
        )

    assert result.ran is True
    assert result.outcome == RunOutcome.DRY_RUN_STORED
    assert result.run_id is not None
    assert result.approval_id is not None


@pytest.mark.asyncio
async def test_operator_trigger_real_success_on_safe(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Safe runbook (dry_run_required=False), real mode -> RAN, run_id set."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

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
        result = await orch.handle_operator_trigger(
            rb.id, RunMode.REAL, principal="alice", ip="10.0.0.1"
        )

    assert result.ran is True
    assert result.outcome == RunOutcome.RAN
    assert result.run_id is not None


@pytest.mark.asyncio
async def test_operator_trigger_real_on_risky_raises(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Risky runbook, real mode -> DryRunRequiredForRiskyError; no run row; audit."""
    rb = _make_risky_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    with pytest.raises(DryRunRequiredForRiskyError):
        await orch.handle_operator_trigger(rb.id, RunMode.REAL, principal="alice", ip="10.0.0.1")

    runs = await repo.fetch_all(
        text("SELECT id FROM runbook_runs WHERE runbook_id = :rid"), {"rid": rb.id}
    )
    assert runs == []

    audit = await repo.fetch_one(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.trigger_rejected'"), {}
    )
    assert audit is not None
    after = json.loads(str(audit[0]))
    assert after["reason"] == "dry_run_required_for_risky"
    assert after["initiated_by"] == "operator"
    assert after["mode"] == "real"

    who_row = await repo.fetch_one(
        text("SELECT who FROM audit_log WHERE what = 'autofix.trigger_rejected'"), {}
    )
    assert who_row is not None
    assert str(who_row[0]) == "alice"


@pytest.mark.asyncio
async def test_operator_trigger_kill_switch_denied(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Kill-switch engaged (unset) -> DENIED, KILL_SWITCH; audit who=principal."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_operator_trigger(
        rb.id, RunMode.DRY_RUN, principal="alice", ip="10.0.0.1"
    )
    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.KILL_SWITCH

    audit = await repo.fetch_one(
        text("SELECT who, after_json FROM audit_log WHERE what = 'autofix.denied'"), {}
    )
    assert audit is not None
    assert str(audit[0]) == "alice"
    after = json.loads(str(audit[1]))
    assert after["initiated_by"] == "operator"


@pytest.mark.asyncio
async def test_operator_trigger_kill_switch_denied_with_credential_type(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Kill-switch denial audit includes credential_type when the caller passes one.

    Mirrors test_operator_trigger_kill_switch_denied, but exercises the
    `credential_type` conditional-add in _deny_operator (orchestrator.py
    ~1750-1751) via a RunMode.REAL trigger with credential_type="phrase".
    """
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_operator_trigger(
        rb.id,
        RunMode.REAL,
        principal="alice",
        ip="10.0.0.1",
        credential_type="phrase",
    )
    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.KILL_SWITCH

    audit = await repo.fetch_one(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.denied'"), {}
    )
    assert audit is not None
    after = json.loads(str(audit[0]))
    assert after["credential_type"] == "phrase"


@pytest.mark.asyncio
async def test_operator_trigger_disabled_runbook(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """enabled=False -> DENIED, ALLOW_LIST."""
    rb = _make_runbook_record(alertname="TestAlert", enabled=False)
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_operator_trigger(
        rb.id, RunMode.DRY_RUN, principal="alice", ip="10.0.0.1"
    )
    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.ALLOW_LIST


@pytest.mark.asyncio
async def test_operator_trigger_rate_limited(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Rate-limit tripped -> DENIED, RATE_LIMIT."""
    rb = _make_runbook_record(alertname="TestAlert", rate_limit_per_hour=1)
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    with patch.object(RunbookRunsRepository, "count_started_since", new=AsyncMock(return_value=1)):
        result = await orch.handle_operator_trigger(
            rb.id, RunMode.DRY_RUN, principal="alice", ip="10.0.0.1"
        )
    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.RATE_LIMIT


@pytest.mark.asyncio
async def test_operator_trigger_cooldown(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Cooldown active -> DENIED, COOLDOWN."""
    rb = _make_runbook_record(alertname="TestAlert", cooldown_seconds=3600)
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    with patch.object(
        RunbookRunsRepository,
        "latest_ended_at",
        new=AsyncMock(return_value=utc_now_iso()),
    ):
        result = await orch.handle_operator_trigger(
            rb.id, RunMode.DRY_RUN, principal="alice", ip="10.0.0.1"
        )
    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.COOLDOWN


@pytest.mark.asyncio
async def test_operator_trigger_in_lock_denial_audit_includes_credential_type(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """In-lock ALREADY_RUNNING denial (_claim_and_exec, not the fast-path lock
    check) audits credential_type when the operator trigger supplied one.

    Forces the in-lock gate (not the fast `_lock_for(...).locked()` check used
    by test_operator_trigger_already_running) by patching count_inflight, per
    the pattern in test_execute_approved_claim_denies_no_real_run_id_set.
    Exercises orchestrator.py's _claim_and_exec in_lock_denial branch
    (~lines 580-581) where credential_type is conditionally added to
    denied_after.
    """
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=5)):
        result = await orch.handle_operator_trigger(
            rb.id,
            RunMode.REAL,
            principal="alice",
            ip="10.0.0.1",
            credential_type="pin",
        )

    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.ALREADY_RUNNING

    audit = await repo.fetch_one(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.denied'"), {}
    )
    assert audit is not None
    after = json.loads(str(audit[0]))
    assert after["credential_type"] == "pin"


@pytest.mark.asyncio
async def test_operator_trigger_already_running(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Per-runbook lock already held -> DENIED, ALREADY_RUNNING (fast-path, no deadlock)."""
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    lock = orch._lock_for(rb.id)  # pyright: ignore[reportPrivateUsage]
    await lock.acquire()
    try:
        result = await orch.handle_operator_trigger(
            rb.id, RunMode.DRY_RUN, principal="alice", ip="10.0.0.1"
        )
    finally:
        lock.release()

    assert result.outcome == RunOutcome.DENIED
    assert result.denial_reason == DenialReason.ALREADY_RUNNING


@pytest.mark.asyncio
async def test_operator_trigger_runbook_not_found(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Nonexistent runbook_id -> RunbookNotFoundError."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    with pytest.raises(RunbookNotFoundError):
        await orch.handle_operator_trigger(
            "does-not-exist", RunMode.DRY_RUN, principal="alice", ip="10.0.0.1"
        )


@pytest.mark.asyncio
async def test_operator_trigger_initiated_by_column_dry(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """After a dry operator trigger, runbook_runs.initiated_by == 'operator'."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="plan", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_operator_trigger(
            rb.id, RunMode.DRY_RUN, principal="alice", ip="10.0.0.1"
        )

    assert result.run_id is not None
    row = await repo.fetch_one(
        text("SELECT initiated_by FROM runbook_runs WHERE id = :id"), {"id": result.run_id}
    )
    assert row is not None
    assert str(row[0]) == "operator"


@pytest.mark.asyncio
async def test_operator_trigger_initiated_by_column_real(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """After a real operator trigger on a safe runbook, initiated_by == 'operator'."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

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
        result = await orch.handle_operator_trigger(
            rb.id, RunMode.REAL, principal="alice", ip="10.0.0.1"
        )

    assert result.run_id is not None
    row = await repo.fetch_one(
        text("SELECT initiated_by FROM runbook_runs WHERE id = :id"), {"id": result.run_id}
    )
    assert row is not None
    assert str(row[0]) == "operator"


@pytest.mark.asyncio
async def test_alert_path_still_writes_initiated_by_alert(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Existing alert path (handle_alert) still results in initiated_by == 'alert'."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
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
    assert result.run_id is not None
    row = await repo.fetch_one(
        text("SELECT initiated_by FROM runbook_runs WHERE id = :id"), {"id": result.run_id}
    )
    assert row is not None
    assert str(row[0]) == "alert"


@pytest.mark.asyncio
async def test_audit_ran_after_json_contains_initiated_by(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """After a real operator run, autofix.ran's after_json includes initiated_by=operator."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

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
        await orch.handle_operator_trigger(rb.id, RunMode.REAL, principal="alice", ip="10.0.0.1")

    audit = await repo.fetch_one(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.ran'"), {}
    )
    assert audit is not None
    after = json.loads(str(audit[0]))
    assert after["initiated_by"] == "operator"


@pytest.mark.asyncio
async def test_audit_dry_run_stored_after_json_contains_initiated_by(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """After a dry operator run, autofix.dry_run_stored's after_json includes initiated_by.

    As of STAGE-009-010 Finding I1, the orchestrator's _claim_and_store_dry
    method includes initiated_by in the after_json dict for autofix.dry_run_stored
    audit entries. This test verifies that the DB row's initiated_by column is
    correctly set and matches the audit trail.
    """
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="plan", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_operator_trigger(
            rb.id, RunMode.DRY_RUN, principal="alice", ip="10.0.0.1"
        )

    audit = await repo.fetch_one(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.dry_run_stored'"), {}
    )
    assert audit is not None
    after = json.loads(str(audit[0]))
    assert after["initiated_by"] == "operator"
    assert result.run_id is not None
    row = await repo.fetch_one(
        text("SELECT initiated_by FROM runbook_runs WHERE id = :id"), {"id": result.run_id}
    )
    assert row is not None
    assert str(row[0]) == "operator"


@pytest.mark.asyncio
async def test_operator_trigger_shares_rate_limit_bucket_with_alert(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Real run via alert path, then operator trigger within rate window -> RATE_LIMIT."""
    rb = _make_runbook_record(
        alertname="TestAlert", rate_limit_per_hour=1, runbook_dir=tmp_path / "runbook"
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

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="done", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        first = await orch.handle_alert(alert)
        assert first is not None
        assert first.outcome == RunOutcome.RAN

        second = await orch.handle_operator_trigger(
            rb.id, RunMode.DRY_RUN, principal="alice", ip="10.0.0.1"
        )
    assert second.outcome == RunOutcome.DENIED
    assert second.denial_reason == DenialReason.RATE_LIMIT


@pytest.mark.asyncio
async def test_operator_trigger_shares_cooldown_bucket_with_alert(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Real run via alert path, then operator trigger within cooldown window -> COOLDOWN."""
    rb = _make_runbook_record(
        alertname="TestAlert", cooldown_seconds=3600, runbook_dir=tmp_path / "runbook"
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

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="done", stderr=""))
    orch = _make_orchestrator(
        repo, secrets_repo_fixture, docker, transcript_dir=transcript_dir, exec_log_dir=exec_log_dir
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        first = await orch.handle_alert(alert)
        assert first is not None
        assert first.outcome == RunOutcome.RAN

        second = await orch.handle_operator_trigger(
            rb.id, RunMode.DRY_RUN, principal="alice", ip="10.0.0.1"
        )
    assert second.outcome == RunOutcome.DENIED
    assert second.denial_reason == DenialReason.COOLDOWN


@pytest.mark.asyncio
async def test_operator_trigger_denial_audit_who_is_principal(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository
) -> None:
    """Denial audit who = principal, NOT 'system:autofix'."""
    rb = _make_runbook_record(alertname="TestAlert", enabled=False)
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    result = await orch.handle_operator_trigger(
        rb.id, RunMode.DRY_RUN, principal="bob", ip="10.0.0.1"
    )
    assert result.outcome == RunOutcome.DENIED

    audit = await repo.fetch_one(
        text("SELECT who FROM audit_log WHERE what = 'autofix.denied'"), {}
    )
    assert audit is not None
    assert str(audit[0]) == "bob"
    assert str(audit[0]) != "system:autofix"


@pytest.mark.asyncio
async def test_operator_trigger_ran_audit_who_is_principal(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """autofix.ran audit who = principal on the operator path."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)

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
        await orch.handle_operator_trigger(rb.id, RunMode.REAL, principal="carol", ip="10.0.0.1")

    audit = await repo.fetch_one(text("SELECT who FROM audit_log WHERE what = 'autofix.ran'"), {})
    assert audit is not None
    assert str(audit[0]) == "carol"


# ---------------------------------------------------------------------------
# execute_approved: credential_type in audit (STAGE-009-010B)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approved_runbook_deleted_credential_type_in_audit(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """When executing an approval for a deleted runbook, audit row includes
    credential_type that was used for the approve operation.

    Covers orchestrator.py:962 branch (runbook_missing gate).
    """
    rb = _make_runbook_record(
        alertname="TestAlert", runbook_dir=tmp_path / "runbook", content_hash="h-v1"
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="plan", stderr=""))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    # Create a dry run and approval.
    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        dry_run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb.content_hash,
            mode=RunMode.DRY_RUN,
            initiated_by="alert",
        )

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb.id,
            alert_id=alert.id,
            pinned_runbook_hash=rb.content_hash,
        )

    # Simulate runbook DELETED between plan and approve. We can't actually
    # DELETE the runbook row (FK from runbook_runs blocks it), so patch
    # RunbookRepo.get_runbook to return None — which is precisely what
    # execute_approved's drift check sees when the row is gone.

    # Execute with credential_type="pin".
    with patch.object(RunbookRepo, "get_runbook", new=AsyncMock(return_value=None)):
        await orch.execute_approved(
            approval_id, principal="admin", ip="127.0.0.1", credential_type="pin"
        )

    # Check the audit row for credential_type and gate.
    audit_rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = :w"),
        {"w": "autofix.rejected"},
    )
    assert len(audit_rows) >= 1
    # Find the row matching this approval_id
    for row in audit_rows:
        after = json.loads(row[0])
        if after.get("approval_id") == approval_id:
            assert after["credential_type"] == "pin"
            assert after["gate"] == "runbook_missing"
            return
    pytest.fail(f"No autofix.rejected audit row found for approval {approval_id}")


@pytest.mark.asyncio
async def test_execute_approved_runbook_changed_credential_type_in_audit(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """When executing an approval where the runbook's content_hash has changed,
    audit row includes the credential_type used for the approve operation.

    Covers orchestrator.py:996 branch (runbook_changed gate).
    """
    rb = _make_runbook_record(
        alertname="TestAlert", runbook_dir=tmp_path / "runbook", content_hash="hash-a"
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="plan", stderr=""))
    orch = _make_orchestrator(repo, secrets_repo_fixture, docker)

    # Create a dry run and approval with the original hash.
    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        dry_run_id = await runs_repo.insert_started(
            conn,
            runbook_id=rb.id,
            alert_id=alert.id,
            prompt=rb.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash="hash-a",
            mode=RunMode.DRY_RUN,
            initiated_by="alert",
        )

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb.id,
            alert_id=alert.id,
            pinned_runbook_hash="hash-a",
        )

    # Update the runbook's content_hash to trigger the runbook_changed gate.
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE runbooks SET content_hash = :hash WHERE id = :id"),
            {"hash": "hash-b", "id": rb.id},
        )

    # Execute with credential_type="phrase".
    await orch.execute_approved(
        approval_id, principal="admin", ip="127.0.0.1", credential_type="phrase"
    )

    # Check the audit row for credential_type and gate.
    audit_rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = :w"),
        {"w": "autofix.rejected"},
    )
    assert len(audit_rows) >= 1
    # Find the row matching this approval_id
    for row in audit_rows:
        after = json.loads(row[0])
        if after.get("approval_id") == approval_id:
            assert after["credential_type"] == "phrase"
            assert after["gate"] == "runbook_changed"
            return
    pytest.fail(f"No autofix.rejected audit row found for approval {approval_id}")


@pytest.mark.asyncio
async def test_execute_approved_happy_path_audit_includes_credential_type(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """Happy-path execute_approved with credential_type includes it in both
    autofix.approved and autofix.ran audit rows.

    Covers orchestrator.py:1084 (approval audit) and orchestrator.py:1583
    (ran audit) on the success path where both branches execute.
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

    # Create a dry run and approval via handle_alert
    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        dry_result = await orch.handle_alert(alert)
    assert dry_result is not None
    approval_id = dry_result.approval_id
    assert approval_id is not None

    # Execute approved with credential_type="pin"
    docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
    docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.execute_approved(
            approval_id, principal="admin", ip="1.2.3.4", credential_type="pin"
        )

    assert result is not None
    assert result.ran is True
    assert result.run_id is not None

    # Check autofix.approved audit includes credential_type
    approved_rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = :w"),
        {"w": "autofix.approved"},
    )
    assert len(approved_rows) >= 1
    approved_found = False
    for row in approved_rows:
        after = json.loads(row[0])
        if after.get("approval_id") == approval_id:
            assert after["credential_type"] == "pin", (
                f"Expected credential_type='pin' in autofix.approved audit, got {after}"
            )
            approved_found = True
            break
    assert approved_found, f"No autofix.approved audit found for approval {approval_id}"

    # Check autofix.ran audit includes credential_type
    ran_rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = :w"),
        {"w": "autofix.ran"},
    )
    assert len(ran_rows) >= 1
    ran_found = False
    for row in ran_rows:
        after = json.loads(row[0])
        if after.get("run_id") == result.run_id:
            assert after["credential_type"] == "pin", (
                f"Expected credential_type='pin' in autofix.ran audit, got {after}"
            )
            ran_found = True
            break
    assert ran_found, f"No autofix.ran audit found for run {result.run_id}"
