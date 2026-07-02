"""Unit tests for the feedback-scan wiring in AutoFixOrchestrator (STAGE-009-009).

Covers:
  - No feedback file after exec -> no inserts, no parse_error audit.
  - Valid single/multi-item feedback file -> N insert_conn calls, linked to run_id.
  - Malformed feedback file -> 1 PARSE_ERROR insert_conn call + audit event.
  - feedback_repo=None -> no crash, no attempted inserts.
  - Real success / real errored / dry-run paths all call _process_feedback.
  - _process_feedback swallows feedback_repo exceptions so the parent txn
    (completion + audit) still lands.

Mirrors the fixture/mocking conventions of test_autofix_orchestrator.py exactly
(_FakeDockerClient, _make_runbook_record, _make_alert, _insert_runbook,
_insert_alert, _make_orchestrator) -- extended minimally to support writing a
`.feedback.json` sentinel alongside the `.transcript` file during exec.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import structlog
from sqlalchemy import text

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import Alert, AlertStatus, Severity
from homelab_monitor.kernel.autofix.approvals_repository import (
    RunbookRunApprovalsRepository,
)
from homelab_monitor.kernel.autofix.feedback_repository import RunbookRunFeedbackRepository
from homelab_monitor.kernel.autofix.orchestrator import AutoFixOrchestrator
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import FeedbackKind, RunOutcome
from homelab_monitor.kernel.config import FixerRunnerConfig
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.socket_client import (
    DockerSocketClient,
    DockerSocketConnectionError,
    ExecResult,
)
from homelab_monitor.kernel.runbooks.loader import RUNBOOK_CONFIG_FILENAME
from homelab_monitor.kernel.runbooks.repository import RunbookRecord, RunbookRepo
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

# ---------------------------------------------------------------------------
# Helpers / Fakes (mirrors test_autofix_orchestrator.py conventions)
# ---------------------------------------------------------------------------


@dataclass
class _FakeDockerClient:
    """Minimal DockerSocketClient-shaped stub, extended with feedback-sentinel support.

    If `feedback_content` is set, a `<uuid>.feedback.json` file is written
    alongside the transcript during exec_capture, containing that raw string.
    """

    result: ExecResult = field(
        default_factory=lambda: ExecResult(exit_code=0, stdout="ok", stderr="")
    )
    raises: BaseException | None = None
    transcript_to_write: str | None = None
    feedback_dir: str | None = None
    feedback_content: str | None = None
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
        if self.transcript_to_write is not None:
            with open(self.transcript_to_write, "w", encoding="utf-8") as fh:
                fh.write("fake-claude-transcript\n")
        if self.feedback_dir is not None and self.feedback_content is not None:
            sentinel = Path(self.feedback_dir) / f"{uuid7()}.feedback.json"
            sentinel.write_text(self.feedback_content, encoding="utf-8")
        return self.result


def _write_valid_runbook_yaml(runbook_dir: Path, *, dry_run_required: bool = False) -> None:
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


def _make_runbook_record(
    *,
    runbook_id: str | None = None,
    alertname: str = "TestAlert",
    dry_run_required: bool = False,
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
        rate_limit_per_hour=100,
        cooldown_seconds=0,
        enabled=True,
        auto_trigger=True,
        content_hash=content_hash,
    )


def _make_alert(alertname: str = "TestAlert") -> Alert:
    labels = {"alertname": alertname, "severity": "warning"}
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
                "created": utc_now_iso(),
            },
        )


def _make_orchestrator(  # noqa: PLR0913 -- test-only factory
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    docker_client: _FakeDockerClient | DockerSocketClient,
    *,
    transcript_dir: str,
    exec_log_dir: str,
    feedback_repo: RunbookRunFeedbackRepository | None,
    feedback_id_provider: Callable[[], str] | None = None,
) -> AutoFixOrchestrator:
    log = structlog.get_logger()
    config = FixerRunnerConfig(
        container="test-fixer",
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        fixer_user="homelab-fixer",
        exec_timeout_seconds=60.0,
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
        ssh_target_ids_provider=frozenset,
        feedback_repo=feedback_repo,
        feedback_id_provider=feedback_id_provider,
    )


async def _run_with_gates_open(
    repo: SqliteRepository,
    rb: RunbookRecord,
    alert: Alert,
) -> None:
    await _insert_runbook(repo, rb)
    await _insert_alert(repo, alert)
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")


def _make_dirs(tmp_path: Path) -> tuple[str, str]:
    transcript_dir = str(tmp_path / "transcripts")
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(transcript_dir, exist_ok=True)
    os.makedirs(exec_log_dir, exist_ok=True)
    return transcript_dir, exec_log_dir


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
# No feedback file -> no inserts, no audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_success_no_feedback_file_no_inserts_no_audit(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    rb = _make_runbook_record(runbook_dir=tmp_path / "runbook")
    alert = _make_alert()
    await _run_with_gates_open(repo, rb, alert)

    transcript_dir, exec_log_dir = _make_dirs(tmp_path)
    docker = _FakeDockerClient(result=ExecResult(exit_code=0, stdout="ok", stderr=""))
    feedback_repo = RunbookRunFeedbackRepository(repo)
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        feedback_repo=feedback_repo,
    )

    with (
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
        patch.object(
            RunbookRunFeedbackRepository,
            "insert_conn",
            new=AsyncMock(wraps=feedback_repo.insert_conn),
        ) as mock_insert,
    ):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    mock_insert.assert_not_called()

    rows = await repo.fetch_all(text("SELECT id FROM runbook_run_feedback"), {})
    assert rows == []
    audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.feedback_parse_error'"), {}
    )
    assert audits == []


# ---------------------------------------------------------------------------
# Valid single-item feedback file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_success_single_feedback_item_persisted(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    rb = _make_runbook_record(runbook_dir=tmp_path / "runbook")
    alert = _make_alert()
    await _run_with_gates_open(repo, rb, alert)

    transcript_dir, exec_log_dir = _make_dirs(tmp_path)
    feedback_payload = json.dumps(
        [
            {
                "kind": "missing_capability",
                "suggestion_text": "need ssh access to udm",
                "structured_hint": {"target": "udm"},
            }
        ]
    )
    docker = _FakeDockerClient(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        feedback_dir=transcript_dir,
        feedback_content=feedback_payload,
    )
    feedback_repo = RunbookRunFeedbackRepository(repo)
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        feedback_repo=feedback_repo,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    run_id = result.run_id
    assert run_id is not None

    rows = await feedback_repo.list_by_run(run_id)
    assert len(rows) == 1
    assert rows[0].kind is FeedbackKind.MISSING_CAPABILITY
    assert rows[0].suggestion_text == "need ssh access to udm"
    assert rows[0].structured_hint == {"target": "udm"}
    assert rows[0].runbook_run_id == run_id
    assert rows[0].created_at is not None


# ---------------------------------------------------------------------------
# Valid multi-item feedback file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_success_multi_feedback_items_all_linked(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    rb = _make_runbook_record(runbook_dir=tmp_path / "runbook")
    alert = _make_alert()
    await _run_with_gates_open(repo, rb, alert)

    transcript_dir, exec_log_dir = _make_dirs(tmp_path)
    feedback_payload = json.dumps(
        [
            {"kind": "config_change", "suggestion_text": "a"},
            {"kind": "blocked", "suggestion_text": "b"},
            {"kind": "worked_around", "suggestion_text": "c"},
        ]
    )
    docker = _FakeDockerClient(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        feedback_dir=transcript_dir,
        feedback_content=feedback_payload,
    )
    feedback_repo = RunbookRunFeedbackRepository(repo)
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        feedback_repo=feedback_repo,
    )

    with (
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
        patch.object(
            RunbookRunFeedbackRepository,
            "insert_conn",
            new=AsyncMock(wraps=feedback_repo.insert_conn),
        ) as mock_insert,
    ):
        result = await orch.handle_alert(alert)

    assert result is not None
    run_id = result.run_id
    assert run_id is not None
    assert mock_insert.await_count == 3  # noqa: PLR2004

    rows = await feedback_repo.list_by_run(run_id)
    assert len(rows) == 3  # noqa: PLR2004
    assert all(r.runbook_run_id == run_id for r in rows)


# ---------------------------------------------------------------------------
# Malformed feedback file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_success_malformed_feedback_persists_parse_error_and_audits(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    rb = _make_runbook_record(runbook_dir=tmp_path / "runbook")
    alert = _make_alert()
    await _run_with_gates_open(repo, rb, alert)

    transcript_dir, exec_log_dir = _make_dirs(tmp_path)
    docker = _FakeDockerClient(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        feedback_dir=transcript_dir,
        feedback_content="not valid json{{{",
    )
    feedback_repo = RunbookRunFeedbackRepository(repo)
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        feedback_repo=feedback_repo,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    run_id = result.run_id
    assert run_id is not None

    rows = await feedback_repo.list_by_run(run_id)
    assert len(rows) == 1
    assert rows[0].kind is FeedbackKind.PARSE_ERROR
    assert "not valid json{{{" in rows[0].suggestion_text

    audit = await repo.fetch_one(
        text(
            "SELECT what FROM audit_log WHERE what = 'autofix.feedback_parse_error' "
            "AND json_extract(after_json, '$.run_id') = :rid"
        ),
        {"rid": run_id},
    )
    assert audit is not None


# ---------------------------------------------------------------------------
# feedback_repo=None -> no crash, no attempted inserts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_repo_none_no_crash_orchestrator_continues(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    rb = _make_runbook_record(runbook_dir=tmp_path / "runbook")
    alert = _make_alert()
    await _run_with_gates_open(repo, rb, alert)

    transcript_dir, exec_log_dir = _make_dirs(tmp_path)
    feedback_payload = json.dumps([{"kind": "other", "suggestion_text": "x"}])
    docker = _FakeDockerClient(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        feedback_dir=transcript_dir,
        feedback_content=feedback_payload,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        feedback_repo=None,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True

    rows = await repo.fetch_all(text("SELECT id FROM runbook_run_feedback"), {})
    assert rows == []


# ---------------------------------------------------------------------------
# Real errored path (DockerSocketError) also calls _process_feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_errored_path_still_processes_feedback(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    rb = _make_runbook_record(runbook_dir=tmp_path / "runbook")
    alert = _make_alert()
    await _run_with_gates_open(repo, rb, alert)

    transcript_dir, exec_log_dir = _make_dirs(tmp_path)
    # Write the feedback sentinel directly into transcript_dir up front so the
    # post-snapshot scan finds it even though exec_capture raises before it
    # would normally write anything (errored exec still scans for feedback).
    feedback_payload = json.dumps([{"kind": "blocked", "suggestion_text": "docker down"}])
    docker = _FakeDockerClient(raises=DockerSocketConnectionError("connection refused"))
    feedback_repo = RunbookRunFeedbackRepository(repo)
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        feedback_repo=feedback_repo,
    )

    # Patch exec_capture to write the sentinel AND raise, so the snapshot-diff
    # picks it up exactly like a real errored exec that still emitted feedback
    # before crashing.
    original_exec_capture = docker.exec_capture

    async def _exec_capture_with_feedback(**kwargs: Any) -> ExecResult:  # noqa: ANN401 -- test kwargs proxy
        sentinel = Path(transcript_dir) / f"{uuid7()}.feedback.json"
        sentinel.write_text(feedback_payload, encoding="utf-8")
        return await original_exec_capture(**kwargs)

    docker.exec_capture = _exec_capture_with_feedback  # type: ignore[method-assign]

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    run_id = result.run_id
    assert run_id is not None

    rows = await feedback_repo.list_by_run(run_id)
    assert len(rows) == 1
    assert rows[0].kind is FeedbackKind.BLOCKED


# ---------------------------------------------------------------------------
# Dry-run path also calls _process_feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_path_processes_feedback(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    rb = _make_runbook_record(dry_run_required=True, runbook_dir=tmp_path / "runbook")
    alert = _make_alert()
    await _run_with_gates_open(repo, rb, alert)

    transcript_dir, exec_log_dir = _make_dirs(tmp_path)
    feedback_payload = json.dumps([{"kind": "runbook_gap", "suggestion_text": "plan incomplete"}])
    docker = _FakeDockerClient(
        result=ExecResult(exit_code=0, stdout="plan output", stderr=""),
        transcript_to_write=f"{transcript_dir}/plan-{uuid7()}.transcript",
        feedback_dir=transcript_dir,
        feedback_content=feedback_payload,
    )
    feedback_repo = RunbookRunFeedbackRepository(repo)
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        feedback_repo=feedback_repo,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None

    assert result.outcome == RunOutcome.DRY_RUN_STORED
    run_id = result.run_id
    assert run_id is not None

    rows = await feedback_repo.list_by_run(run_id)
    assert len(rows) == 1
    assert rows[0].kind is FeedbackKind.RUNBOOK_GAP


# ---------------------------------------------------------------------------
# _process_feedback swallows repo exceptions; parent txn still lands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_repo_insert_conn_exception_swallowed_parent_txn_lands(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """insert_conn raising must not prevent completion + autofix.ran audit landing."""
    rb = _make_runbook_record(runbook_dir=tmp_path / "runbook")
    alert = _make_alert()
    await _run_with_gates_open(repo, rb, alert)

    transcript_dir, exec_log_dir = _make_dirs(tmp_path)
    feedback_payload = json.dumps([{"kind": "other", "suggestion_text": "x"}])
    docker = _FakeDockerClient(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        feedback_dir=transcript_dir,
        feedback_content=feedback_payload,
    )
    feedback_repo = RunbookRunFeedbackRepository(repo)
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        feedback_repo=feedback_repo,
    )

    with (
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
        patch.object(
            RunbookRunFeedbackRepository,
            "insert_conn",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    run_id = result.run_id
    assert run_id is not None

    # No feedback row (insert failed), but parent txn (completion + audit) landed.
    rows = await repo.fetch_all(text("SELECT id FROM runbook_run_feedback"), {})
    assert rows == []

    run_row = await repo.fetch_one(
        text("SELECT ended_at, exit_code FROM runbook_runs WHERE id = :id"), {"id": run_id}
    )
    assert run_row is not None
    assert run_row.ended_at is not None
    assert int(run_row.exit_code) == 0

    audit_row = await repo.fetch_one(
        text(
            "SELECT what FROM audit_log WHERE what = 'autofix.ran' "
            "AND json_extract(after_json, '$.run_id') = :rid"
        ),
        {"rid": run_id},
    )
    assert audit_row is not None

    outcome_row = await repo.fetch_one(
        text("SELECT outcome FROM alert_outcomes WHERE alert_id = :aid"), {"aid": alert.id}
    )
    assert outcome_row is not None
    assert str(outcome_row[0]) == "auto_fixed"


@pytest.mark.asyncio
async def test_feedback_repo_insert_exception_on_errored_path_swallowed_parent_txn_lands(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """insert_conn raising on the ERRORED branch must not poison the parent txn.

    Mirrors test_real_errored_path_still_processes_feedback (fixer exec raises
    DockerSocketConnectionError, sentinel written before the raise) combined
    with the insert_conn-raises mocking pattern from
    test_feedback_repo_insert_conn_exception_swallowed_parent_txn_lands.
    """
    rb = _make_runbook_record(runbook_dir=tmp_path / "runbook")
    alert = _make_alert()
    await _run_with_gates_open(repo, rb, alert)

    transcript_dir, exec_log_dir = _make_dirs(tmp_path)
    feedback_payload = json.dumps([{"kind": "other", "suggestion_text": "x"}])
    docker = _FakeDockerClient(raises=DockerSocketConnectionError("connection refused"))
    feedback_repo = RunbookRunFeedbackRepository(repo)
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        feedback_repo=feedback_repo,
    )

    original_exec_capture = docker.exec_capture

    async def _exec_capture_with_feedback(**kwargs: Any) -> ExecResult:  # noqa: ANN401 -- test kwargs proxy
        sentinel = Path(transcript_dir) / f"{uuid7()}.feedback.json"
        sentinel.write_text(feedback_payload, encoding="utf-8")
        return await original_exec_capture(**kwargs)

    docker.exec_capture = _exec_capture_with_feedback  # type: ignore[method-assign]

    with (
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
        patch.object(
            RunbookRunFeedbackRepository,
            "insert_conn",
            new=AsyncMock(side_effect=RuntimeError("simulated DB failure")),
        ),
    ):
        result = await orch.handle_alert(alert)

    assert result is not None
    run_id = result.run_id
    assert run_id is not None

    run_row = await repo.fetch_one(
        text("SELECT ended_at FROM runbook_runs WHERE id = :id"), {"id": run_id}
    )
    assert run_row is not None
    assert run_row.ended_at is not None

    exec_error_audit = await repo.fetch_one(
        text(
            "SELECT what FROM audit_log WHERE what = 'autofix.exec_error' "
            "AND json_extract(after_json, '$.run_id') = :rid"
        ),
        {"rid": run_id},
    )
    assert exec_error_audit is not None

    rows = await repo.fetch_all(
        text("SELECT id FROM runbook_run_feedback WHERE runbook_run_id = :rid"), {"rid": run_id}
    )
    assert rows == []

    parse_error_audit = await repo.fetch_all(
        text(
            "SELECT what FROM audit_log WHERE what = 'autofix.feedback_parse_error' "
            "AND json_extract(after_json, '$.run_id') = :rid"
        ),
        {"rid": run_id},
    )
    assert parse_error_audit == []


@pytest.mark.asyncio
async def test_feedback_sentinel_with_empty_list_no_inserts(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """A sentinel file exists (scan finds it) but parses to an empty list -> no-op.

    Covers the `if not items: return` branch distinct from "no sentinel found".
    """
    rb = _make_runbook_record(runbook_dir=tmp_path / "runbook")
    alert = _make_alert()
    await _run_with_gates_open(repo, rb, alert)

    transcript_dir, exec_log_dir = _make_dirs(tmp_path)
    docker = _FakeDockerClient(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        feedback_dir=transcript_dir,
        feedback_content="[]",
    )
    feedback_repo = RunbookRunFeedbackRepository(repo)
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        feedback_repo=feedback_repo,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    run_id = result.run_id
    assert run_id is not None

    rows = await feedback_repo.list_by_run(run_id)
    assert rows == []


@pytest.mark.asyncio
async def test_feedback_id_provider_used_when_supplied(
    repo: SqliteRepository, secrets_repo_fixture: AsyncSecretsRepository, tmp_path: Path
) -> None:
    """A custom feedback_id_provider is used for deterministic ids in tests."""
    rb = _make_runbook_record(runbook_dir=tmp_path / "runbook")
    alert = _make_alert()
    await _run_with_gates_open(repo, rb, alert)

    transcript_dir, exec_log_dir = _make_dirs(tmp_path)
    feedback_payload = json.dumps([{"kind": "other", "suggestion_text": "x"}])
    docker = _FakeDockerClient(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        feedback_dir=transcript_dir,
        feedback_content=feedback_payload,
    )
    feedback_repo = RunbookRunFeedbackRepository(repo)
    fixed_id = "fixed-feedback-id-0001"
    orch = _make_orchestrator(
        repo,
        secrets_repo_fixture,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
        feedback_repo=feedback_repo,
        feedback_id_provider=lambda: fixed_id,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    run_id = result.run_id
    assert run_id is not None

    rows = await feedback_repo.list_by_run(run_id)
    assert len(rows) == 1
    assert rows[0].id == fixed_id
