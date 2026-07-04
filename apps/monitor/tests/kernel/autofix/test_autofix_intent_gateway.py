"""Orchestrator-integration tests for _execute_intents (STAGE-009-014).

Tests the docker intent gateway end-to-end via _claim_and_exec and
_claim_and_store_dry, using mocked DockerSocketClient with
restart_container call tracking.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import ResolvedGrants, RunOutcome
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.socket_client import (
    DockerExecTimeoutError,
    DockerSocketConnectionError,
    DockerSocketProtocolError,
    ExecResult,
)
from homelab_monitor.kernel.runbooks.loader import RUNBOOK_CONFIG_FILENAME
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
# Helpers
# ---------------------------------------------------------------------------


async def _read_intent_audits(
    repo: SqliteRepository, run_id: str
) -> list[tuple[str, dict[str, Any]]]:
    """Read all intent-related audit rows for a run, ordered by id."""
    async with repo.transaction() as conn:
        rows = await conn.execute(
            text(
                "SELECT what, after_json FROM audit_log "
                "WHERE what LIKE 'autofix.intent_%' "
                "AND after_json LIKE :run_id_pattern "
                "ORDER BY id"
            ),
            {"run_id_pattern": f"%{run_id}%"},
        )
        result: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            what = row[0]
            after_json = json.loads(row[1])
            result.append((what, after_json))
    return result


def _write_intent(transcript_dir: Path, run_id: str, payload: str) -> Path:
    """Write an intent file to <transcript_dir>/<run_id>/docker-intent.json."""
    intent_dir = transcript_dir / run_id
    intent_dir.mkdir(parents=True, exist_ok=True)
    intent_path = intent_dir / "docker-intent.json"
    intent_path.write_text(payload)
    return intent_path


def _empty_restart_calls() -> list[str]:
    """Factory for empty restart_calls list."""
    return []


def _empty_restart_raises() -> dict[int, BaseException]:
    """Factory for empty restart_raises_by_index dict."""
    return {}


@dataclass
class _FakeDockerClientWithRestart(_FakeDockerClient):
    """Extend _FakeDockerClient with restart_container tracking + intent-file emission.

    Mirrors production behavior: the fixer container writes docker-intent.json
    into the transcript dir during its exec. Since handle_alert generates a
    fresh run_id per call and the intent gateway reads from that run's dir,
    tests can't pre-write the file. Instead, they set pending_intent_payload
    and transcript_root; exec_capture writes the file using the current
    HM_RUN_ID env var right before delegating to the base exec_capture.
    """

    restart_calls: list[str] = field(default_factory=_empty_restart_calls)
    restart_raises_by_index: dict[int, BaseException] = field(default_factory=_empty_restart_raises)
    pending_intent_payload: str | None = None
    transcript_root: Path | None = None

    async def exec_capture(  # type: ignore[override]
        self,
        *,
        container_id: str,
        cmd: list[str],
        timeout_seconds: float,
        user: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        if (
            self.pending_intent_payload is not None
            and self.transcript_root is not None
            and env is not None
            and "HM_RUN_ID" in env
        ):
            run_id = env["HM_RUN_ID"]
            intent_dir = self.transcript_root / run_id
            intent_dir.mkdir(parents=True, exist_ok=True)
            (intent_dir / "docker-intent.json").write_text(self.pending_intent_payload)
        return await super().exec_capture(
            container_id=container_id,
            cmd=cmd,
            timeout_seconds=timeout_seconds,
            user=user,
            env=env,
        )

    async def restart_container(
        self, container_id: str, *, timeout_seconds: int | None = None
    ) -> None:
        """Track restart calls and optionally raise per-index."""
        idx = len(self.restart_calls)
        self.restart_calls.append(container_id)
        raised = self.restart_raises_by_index.get(idx)
        if raised is not None:
            raise raised


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_intents_no_audits(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """No intent file written → no audits, no docker calls."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result.run_id or "")
    assert audits == []


@pytest.mark.asyncio
async def test_one_valid_intent_executes_and_audits(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """One intent matching grants → restart called + intent_executed audit."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload='[{"container":"pihole-unbound","action":"restart"}]',
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    assert docker.restart_calls == ["pihole-unbound"]
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_executed"]


@pytest.mark.asyncio
async def test_one_intent_denied_container_mismatch_no_docker_call(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Intent container mismatch → intent_denied, no docker call."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload='[{"container":"different","action":"restart"}]',
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_denied"]
    assert "container 'different' not in envelope" in audits[0][1]["reason"]


@pytest.mark.asyncio
async def test_one_intent_denied_action_not_allowed_no_docker_call(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Intent action not in allowed_actions → intent_denied."""
    # Create a runbook with empty allowed_actions
    runbook_dir = tmp_path / "runbook"
    runbook_dir.mkdir(parents=True, exist_ok=True)
    (runbook_dir / RUNBOOK_CONFIG_FILENAME).write_text(
        """\
name: test-runbook
match_patterns:
  - alertname: TestAlert
    labels: {}
risk_tag: safe
dry_run_required: false
rate_limit_per_hour: 100
cooldown_seconds: 0
scoped_capabilities:
  docker:
    container: "pihole-unbound"
    allowed_actions: []
"""
    )

    rb = _make_runbook_record(
        alertname="TestAlert",
        runbook_dir=runbook_dir,
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload='[{"container":"pihole-unbound","action":"restart"}]',
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_denied"]
    assert "action 'restart' not in allowed_actions" in audits[0][1]["reason"]


@pytest.mark.asyncio
async def test_no_docker_capability_all_intents_denied(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """No docker in scoped_capabilities → all intents denied."""
    runbook_dir = tmp_path / "runbook"
    runbook_dir.mkdir(parents=True, exist_ok=True)
    (runbook_dir / RUNBOOK_CONFIG_FILENAME).write_text(
        """\
name: test-runbook
match_patterns:
  - alertname: TestAlert
    labels: {}
risk_tag: safe
dry_run_required: false
rate_limit_per_hour: 100
cooldown_seconds: 0
scoped_capabilities:
  ssh:
    target_id: "udm"
"""
    )

    rb = _make_runbook_record(
        alertname="TestAlert",
        runbook_dir=runbook_dir,
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload='[{"container":"pihole-unbound","action":"restart"}]',
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
        ssh_target_ids_provider=lambda: frozenset({"udm"}),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_denied"]
    assert "docker not in scoped_capabilities" in audits[0][1]["reason"]


@pytest.mark.asyncio
async def test_multi_intent_two_valid_one_invalid_three_audits(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Multiple intents: valid, denied, valid → executed, denied, executed."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload="["
        '{"container":"pihole-unbound","action":"restart"},'
        '{"container":"wrong","action":"restart"},'
        '{"container":"pihole-unbound","action":"restart"}'
        "]",
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert docker.restart_calls == ["pihole-unbound", "pihole-unbound"]
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_executed", "autofix.intent_denied", "autofix.intent_executed"]


@pytest.mark.asyncio
async def test_docker_protocol_error_continues_batch(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """DockerSocketProtocolError on first intent → audits, continue batch."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        restart_raises_by_index={
            0: DockerSocketProtocolError("boom"),
        },
        pending_intent_payload="["
        '{"container":"pihole-unbound","action":"restart"},'
        '{"container":"pihole-unbound","action":"restart"}'
        "]",
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    # Both containers called despite first error
    assert docker.restart_calls == ["pihole-unbound", "pihole-unbound"]
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_exec_error", "autofix.intent_executed"]
    # Fix M6: Verify payload fields for exec_error audit
    assert audits[0][1]["container"] == "pihole-unbound"
    assert audits[0][1]["action"] == "restart"
    # Verify payload fields for executed audit
    assert audits[1][1]["container"] == "pihole-unbound"
    assert audits[1][1]["action"] == "restart"


@pytest.mark.asyncio
async def test_docker_connection_error_halts_batch_with_skipped_audits(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """DockerSocketConnectionError → exec_error + skip rest of batch."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        restart_raises_by_index={
            0: DockerSocketConnectionError("down"),
        },
        pending_intent_payload="["
        '{"container":"pihole-unbound","action":"restart"},'
        '{"container":"pihole-unbound","action":"restart"},'
        '{"container":"pihole-unbound","action":"restart"}'
        "]",
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    # Only first called before error
    assert docker.restart_calls == ["pihole-unbound"]
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == [
        "autofix.intent_exec_error",
        "autofix.intent_skipped_after_error",
        "autofix.intent_skipped_after_error",
    ]
    # Fix M6: Verify payload fields for all skipped audits
    skipped_rows = [row for row in audits if row[0] == "autofix.intent_skipped_after_error"]
    assert len(skipped_rows) == 2  # noqa: PLR2004 — 1 executed + 1 error
    assert all(row[1]["container"] == "pihole-unbound" for row in skipped_rows)
    assert all(row[1]["action"] == "restart" for row in skipped_rows)


@pytest.mark.asyncio
async def test_docker_exec_timeout_error_treated_as_connection_error(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """DockerExecTimeoutError (subclass of ConnectionError) halts batch."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        restart_raises_by_index={
            0: DockerExecTimeoutError("timeout"),
        },
        pending_intent_payload="["
        '{"container":"pihole-unbound","action":"restart"},'
        '{"container":"pihole-unbound","action":"restart"},'
        '{"container":"pihole-unbound","action":"restart"}'
        "]",
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert docker.restart_calls == ["pihole-unbound"]
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == [
        "autofix.intent_exec_error",
        "autofix.intent_skipped_after_error",
        "autofix.intent_skipped_after_error",
    ]


@pytest.mark.asyncio
async def test_malformed_intent_json_audits_malformed_and_returns(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Malformed JSON → intent_malformed audit, no docker calls."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload="not json{",
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_malformed"]
    assert audits[0][1]["reason"] == "not_json"


@pytest.mark.asyncio
async def test_not_a_list_json_audits_malformed_with_reason(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """JSON object (not list) → intent_malformed with not_a_list reason."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload='{"container":"foo","action":"restart"}',
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_malformed"]
    assert audits[0][1]["reason"] == "not_a_list"


@pytest.mark.asyncio
async def test_invalid_entry_audits_malformed(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Invalid entry (min_length violation) → intent_malformed."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload='[{"container":"","action":"restart"}]',
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_malformed"]
    assert audits[0][1]["reason"] == "invalid_entry"


@pytest.mark.asyncio
async def test_kill_switch_off_skips_all_intents_with_single_audit(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """autofix_enabled=false → intent_kill_switched audit, no docker calls."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "false")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    run_id = uuid7()

    # Write intent file (should be ignored by kill-switch)
    _write_intent(transcript_dir, run_id, '[{"container":"pihole-unbound","action":"restart"}]')

    # Directly call _execute_intents (mock grants)
    grants = ResolvedGrants(
        docker_container="pihole-unbound",
        docker_allowed_actions=("restart",),
        ssh_target_id=None,
        egress=(),
    )

    await orch._execute_intents(  # pyright: ignore[reportPrivateUsage]
        run_id=run_id,
        alert=alert,
        record=rb,
        grants=grants,
        audit_who="system:test",
        dry=False,
    )

    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, run_id)
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_kill_switched"]


@pytest.mark.asyncio
async def test_dry_run_valid_intent_audits_dry_planned_no_docker_call(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Dry run with valid intent → intent_dry_planned, no docker call."""
    rb = _make_runbook_record(
        alertname="TestAlert", dry_run_required=True, runbook_dir=tmp_path / "runbook"
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="plan", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload='[{"container":"pihole-unbound","action":"restart"}]',
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.outcome == RunOutcome.DRY_RUN_STORED
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_dry_planned"]


@pytest.mark.asyncio
async def test_dry_run_denied_intent_audits_denied_not_dry_planned(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Dry run with denied intent → intent_denied (not dry_planned)."""
    rb = _make_runbook_record(
        alertname="TestAlert", dry_run_required=True, runbook_dir=tmp_path / "runbook"
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="plan", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload='[{"container":"wrong","action":"restart"}]',
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_denied"]


@pytest.mark.asyncio
async def test_dry_run_docker_never_called_regardless_of_validation(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Dry run: docker never called regardless of validation outcome."""
    rb = _make_runbook_record(
        alertname="TestAlert", dry_run_required=True, runbook_dir=tmp_path / "runbook"
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="plan", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload="["
        '{"container":"pihole-unbound","action":"restart"},'
        '{"container":"wrong","action":"restart"},'
        '{"container":"pihole-unbound","action":"restart"}'
        "]",
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result.run_id or "")
    whats = [w for w, _ in audits]
    # Should have dry_planned for first and third, denied for second
    assert whats == [
        "autofix.intent_dry_planned",
        "autofix.intent_denied",
        "autofix.intent_dry_planned",
    ]


@pytest.mark.asyncio
async def test_grant_resolution_failure_skips_intent_gateway(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Grant resolution failure → intent gateway skipped, no intent audits."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
        ssh_target_ids_provider=frozenset,
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.run_id is not None
    run_id = result.run_id

    _write_intent(transcript_dir, run_id, '[{"container":"pihole-unbound","action":"restart"}]')

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result2 = await orch.handle_alert(alert)

    assert result2 is not None
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result2.run_id or "")
    # No intent_* audits when grant resolution fails
    assert audits == []


@pytest.mark.asyncio
async def test_empty_intent_list_no_audits(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Intent file present but an empty JSON list → no restart calls, no audits."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload="[]",
        transcript_root=transcript_dir,
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.ran is True
    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, result.run_id or "")
    assert audits == []


@pytest.mark.asyncio
async def test_execute_intents_raises_emits_gateway_failed_audit_real(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """_execute_intents raising in the real-exec path emits intent_gateway_failed
    and the run row is still completed (not orphaned)."""
    rb = _make_runbook_record(
        alertname="TestAlert", dry_run_required=False, runbook_dir=tmp_path / "runbook"
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with (
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
        patch.object(orch, "_execute_intents", side_effect=RuntimeError("boom")),
    ):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.run_id is not None
    audits = await _read_intent_audits(repo, result.run_id)
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_gateway_failed"]
    assert audits[0][1]["error"] == "boom"

    run_row = await repo.fetch_one(
        text("SELECT ended_at FROM runbook_runs WHERE id = :id"),
        {"id": result.run_id},
    )
    assert run_row is not None
    assert run_row.ended_at is not None


@pytest.mark.asyncio
async def test_execute_intents_raises_emits_gateway_failed_audit_dry(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """_execute_intents raising in the dry-run path emits intent_gateway_failed
    and the dry-run outcome is still stored (not orphaned)."""
    rb = _make_runbook_record(
        alertname="TestAlert", dry_run_required=True, runbook_dir=tmp_path / "runbook"
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="plan", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with (
        patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)),
        patch.object(orch, "_execute_intents", side_effect=TypeError("boom")),
    ):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.outcome == RunOutcome.DRY_RUN_STORED
    assert result.run_id is not None
    audits = await _read_intent_audits(repo, result.run_id)
    whats = [w for w, _ in audits]
    assert whats == ["autofix.intent_gateway_failed"]
    assert audits[0][1]["error"] == "boom"

    run_row = await repo.fetch_one(
        text("SELECT ended_at FROM runbook_runs WHERE id = :id"),
        {"id": result.run_id},
    )
    assert run_row is not None
    assert run_row.ended_at is not None


@pytest.mark.asyncio
async def test_exec_error_skips_intent_gateway(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Exec error → intent gateway skipped, no intent_* audits."""
    rb = _make_runbook_record(alertname="TestAlert", runbook_dir=tmp_path / "runbook")
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        raises=DockerSocketConnectionError("exec failed"),
    )
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
    )

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        result = await orch.handle_alert(alert)

    assert result is not None
    assert result.run_id is not None
    run_id = result.run_id

    _write_intent(transcript_dir, run_id, '[{"container":"pihole-unbound","action":"restart"}]')

    assert docker.restart_calls == []
    audits = await _read_intent_audits(repo, run_id)
    # No intent_* audits when exec error occurs
    assert audits == []
