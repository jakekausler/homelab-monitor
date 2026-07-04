"""End-to-end pipeline validation for the ``pihole-restart-loop`` example runbook.

STAGE-009-013 (epic-closing). Exercises the WHOLE auto-fix pipeline against the
shipped ``runbooks/_examples/pihole-restart-loop/`` exemplar to prove non-negotiables
#1 (allow-list match), #2 (scope envelope), #4 (audit), #5 (dry-run + approval),
#6 (rate-limit/cooldown gate), #7 (kill switch). Non-negotiable #3 (identity)
is exercised by the Refinement-phase prod-rig validation.

The exemplar itself ships inert: the loader skips ``_``-prefixed folders. To
drive the pipeline the test copies the shipped folder into ``tmp_path`` under a
non-underscore name so the loader picks it up + the orchestrator resolves docker
grants from the real YAML.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.autofix.runs_repository import (
    RunbookRunsRepository,
    RunsFilter,
)
from homelab_monitor.kernel.autofix.types import RunMode, RunOutcome
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.socket_client import ExecResult
from homelab_monitor.kernel.runbooks.config import RunbookConfig
from homelab_monitor.kernel.runbooks.loader import (
    RUNBOOK_CONFIG_FILENAME,
    scan_runbooks,
)
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository
from tests.kernel.autofix.test_autofix_intent_gateway import (
    _FakeDockerClientWithRestart,  # pyright: ignore[reportPrivateUsage]
    _read_intent_audits,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_autofix_orchestrator import (
    _insert_alert,  # pyright: ignore[reportPrivateUsage]
    _insert_runbook,  # pyright: ignore[reportPrivateUsage]
    _make_alert,  # pyright: ignore[reportPrivateUsage]
    _make_orchestrator,  # pyright: ignore[reportPrivateUsage]
    _make_runbook_record,  # pyright: ignore[reportPrivateUsage]
)

# Repo-root path to the shipped exemplar.
# tests/kernel/autofix/test_pihole_example_e2e.py → up 5 levels → repo root →
# runbooks/_examples/pihole-restart-loop/
_SHIPPED_EXAMPLE = (
    Path(__file__).resolve().parents[5] / "runbooks" / "_examples" / "pihole-restart-loop"
)

# Expected values from the shipped exemplar runbook.yaml — used across multiple
# tests to assert config drift is caught.
_EXPECTED_RATE_LIMIT_PER_HOUR = 2
_EXPECTED_COOLDOWN_SECONDS = 900
_EXPECTED_MIN_RUN_COUNT = 2  # 1 dry-run row + 1 real-run row after the pipeline test


def test_shipped_example_config_validates_against_schema() -> None:
    """The shipped `runbook.yaml` parses + validates against the STAGE-009-001 model.

    Drift-detection guard: if the schema changes and the exemplar is not
    updated, this fails immediately with a clear config error rather than
    silently shipping a stale example.
    """
    config = RunbookConfig.load_from_path(_SHIPPED_EXAMPLE / RUNBOOK_CONFIG_FILENAME)
    assert config.name == "pihole-restart-loop"
    assert config.risk_tag.value == "risky"
    assert config.dry_run_required is True
    assert config.rate_limit_per_hour == _EXPECTED_RATE_LIMIT_PER_HOUR
    assert config.cooldown_seconds == _EXPECTED_COOLDOWN_SECONDS
    assert config.scoped_capabilities.docker is not None
    assert config.scoped_capabilities.docker.container == "pihole-unbound"
    assert config.scoped_capabilities.docker.allowed_actions == ["restart"]
    assert len(config.match_patterns) == 1
    assert config.match_patterns[0].alertname == "PiholeCrashLoop"


def test_shipped_example_has_claude_md_and_is_non_empty() -> None:
    """CLAUDE.md exists + is non-empty (required by the loader)."""
    claude_md = _SHIPPED_EXAMPLE / "CLAUDE.md"
    assert claude_md.is_file(), f"missing CLAUDE.md at {claude_md}"
    body = claude_md.read_text(encoding="utf-8")
    assert body.strip(), "CLAUDE.md must be non-empty"
    assert "pihole-unbound" in body
    assert "restart" in body


def test_shipped_example_is_skipped_by_loader() -> None:
    """The shipped exemplar under `_examples/` is auto-skipped by the loader.

    Guards non-negotiable #7 / LOCKED Decision 4 (ship-inert): a fresh
    install has zero active auto-fix; the pihole-restart-loop exemplar is
    NOT registered.
    """
    runbooks_root = _SHIPPED_EXAMPLE.parents[1]  # .../_examples/pihole-restart-loop -> .../runbooks
    result = scan_runbooks(runbooks_root)
    loaded_folders = {rb.folder.resolve() for rb in result.loaded}
    assert _SHIPPED_EXAMPLE.resolve() not in loaded_folders
    error_paths = {err.path for err in result.errors}
    assert str(_SHIPPED_EXAMPLE) not in error_paths
    assert str(_SHIPPED_EXAMPLE.parent) not in error_paths


@pytest.mark.asyncio
async def test_pipeline_e2e_dry_run_approval_real_run(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Full pipeline: match -> dry-run stored -> approved -> real exec -> audited.

    Exercises non-negotiables #1 (allow-list match), #2 (scope envelope),
    #4 (audit), #5 (dry-run + approval), #6 (rate-limit/cooldown gate — both
    configured generously enough not to trip on a single run).
    """
    runbook_dir = tmp_path / "pihole-restart-loop"
    shutil.copytree(_SHIPPED_EXAMPLE, runbook_dir)

    rb = _make_runbook_record(
        alertname="PiholeCrashLoop",
        auto_trigger=False,
        dry_run_required=True,
        rate_limit_per_hour=2,
        cooldown_seconds=0,
        content_hash="pihole-example-hash-v1",
        runbook_dir=runbook_dir,
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="PiholeCrashLoop")
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
        pending_intent_payload=json.dumps([{"container": "pihole-unbound", "action": "restart"}]),
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
        result_dry = await orch.handle_operator_trigger(
            rb.id, mode=RunMode.DRY_RUN, principal="test-user", ip="127.0.0.1"
        )
        assert result_dry is not None
        assert result_dry.outcome == RunOutcome.DRY_RUN_STORED
        assert result_dry.approval_id is not None

        result_real = await orch.execute_approved(
            result_dry.approval_id, principal="test-user", ip="127.0.0.1"
        )
        assert result_real.outcome == RunOutcome.RAN

    assert docker.restart_calls == ["pihole-unbound"]

    # Audit trail: query broadly, assert expected events are a subset of what fired.
    async with repo.transaction() as conn:
        rows = await conn.execute(text("SELECT what FROM audit_log ORDER BY id"))
        whats = {row[0] for row in rows}
    expected_subset = {"autofix.dry_run_stored", "autofix.approved", "autofix.intent_executed"}
    assert expected_subset.issubset(whats), f"missing expected audit events; got {whats}"

    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(
        RunsFilter(
            runbook_id=rb.id,
            mode=None,
            outcome=None,
            initiator=None,
            since=None,
            until=None,
        ),
        limit=100,
        offset=0,
    )
    assert total >= _EXPECTED_MIN_RUN_COUNT
    real_rows = [r for r in rows if r.mode == "real"]
    assert real_rows, "expected at least one real-mode run row"
    assert any(r.exit_code == 0 for r in real_rows)


@pytest.mark.asyncio
async def test_pipeline_e2e_docker_intent_denied_for_wrong_container(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Intent for a container outside the scope envelope is denied, not executed.

    Exercises non-negotiable #2 (scope enforcement).
    """
    runbook_dir = tmp_path / "pihole-restart-loop"
    shutil.copytree(_SHIPPED_EXAMPLE, runbook_dir)

    rb = _make_runbook_record(
        alertname="PiholeCrashLoop",
        auto_trigger=False,
        dry_run_required=True,
        rate_limit_per_hour=2,
        cooldown_seconds=0,
        content_hash="pihole-example-hash-v1",
        runbook_dir=runbook_dir,
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="PiholeCrashLoop")
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
        pending_intent_payload=json.dumps(
            [{"container": "unauthorized-container", "action": "restart"}]
        ),
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
        result_dry = await orch.handle_operator_trigger(
            rb.id, mode=RunMode.DRY_RUN, principal="test-user", ip="127.0.0.1"
        )
        assert result_dry is not None
        assert result_dry.outcome == RunOutcome.DRY_RUN_STORED
        assert result_dry.approval_id is not None

        result_real = await orch.execute_approved(
            result_dry.approval_id, principal="test-user", ip="127.0.0.1"
        )
        # Denial of the intent doesn't fail the run itself.
        assert result_real.outcome == RunOutcome.RAN

    assert docker.restart_calls == []

    audits = await _read_intent_audits(repo, result_real.run_id or "")
    whats = [w for w, _ in audits]
    assert "autofix.intent_denied" in whats
    denied_payload = next(payload for w, payload in audits if w == "autofix.intent_denied")
    assert "not in envelope" in denied_payload["reason"]


@pytest.mark.asyncio
async def test_pipeline_e2e_denies_when_kill_switch_off(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Kill switch off -> handle_alert denies before any dry-run/approval/exec.

    Exercises non-negotiable #7 (kill switch, checked first in
    _check_operational_gates).
    """
    runbook_dir = tmp_path / "pihole-restart-loop"
    shutil.copytree(_SHIPPED_EXAMPLE, runbook_dir)

    rb = _make_runbook_record(
        alertname="PiholeCrashLoop",
        dry_run_required=True,
        rate_limit_per_hour=2,
        cooldown_seconds=900,
        content_hash="pihole-example-hash-v1",
        runbook_dir=runbook_dir,
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="PiholeCrashLoop")
    await _insert_alert(repo, alert)
    # Deliberately do NOT set autofix_enabled — kill switch stays off (default).

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir(parents=True, exist_ok=True)

    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / f"transcript-{uuid7()}.txt"),
        pending_intent_payload=json.dumps([{"container": "pihole-unbound", "action": "restart"}]),
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
    assert result.outcome == RunOutcome.DENIED
    assert result.approval_id is None

    assert docker.restart_calls == []

    async with repo.transaction() as conn:
        rows = await conn.execute(text("SELECT what FROM audit_log ORDER BY id"))
        whats = {row[0] for row in rows}
    assert "autofix.denied" in whats

    runs_repo = RunbookRunsRepository(repo)
    _rows, _total = await runs_repo.list_paged(
        RunsFilter(
            runbook_id=rb.id,
            mode=None,
            outcome=None,
            initiator=None,
            since=None,
            until=None,
        ),
        limit=100,
        offset=0,
    )
    # No approval was ever created; there may still be a run row recording
    # the denial itself — assert no run reached mode="real" with exit_code 0.
    assert not any(r.mode == "real" and r.exit_code == 0 for r in _rows)


@pytest.mark.asyncio
async def test_pipeline_e2e_denies_when_rate_limit_exhausted(
    tmp_path: Path,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Non-negotiable #6: after `rate_limit_per_hour` operator-triggered dry-runs,
    a subsequent trigger denies at the rate-limit gate.

    The exemplar declares `rate_limit_per_hour: 2`. This test fires 2 dry-runs
    (bringing the sliding-window count to 2) and confirms the 3rd is denied
    with `RunOutcome.DENIED` and denial_reason indicating rate-limit.
    """
    shipped_dir = tmp_path / "pihole-restart-loop"
    shutil.copytree(_SHIPPED_EXAMPLE, shipped_dir)
    rb = _make_runbook_record(
        alertname="PiholeCrashLoop",
        enabled=True,
        auto_trigger=False,
        dry_run_required=True,
        rate_limit_per_hour=_EXPECTED_RATE_LIMIT_PER_HOUR,  # =2 from exemplar
        cooldown_seconds=0,
        content_hash="pihole-e2e-rate-limit-hash",
        runbook_dir=shipped_dir,
    )
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname="PiholeCrashLoop")
    await _insert_alert(repo, alert)
    await AppSettingsRepository(repo).set("autofix_enabled", "true")

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    exec_log_dir = tmp_path / "exec-logs"
    exec_log_dir.mkdir()
    docker = _FakeDockerClientWithRestart(
        result=ExecResult(exit_code=0, stdout="ok", stderr=""),
        transcript_to_write=str(transcript_dir / "transcript.txt"),
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
        # Fire dry-runs up to the exemplar's rate_limit_per_hour (2).
        for _ in range(_EXPECTED_RATE_LIMIT_PER_HOUR):
            result = await orch.handle_operator_trigger(
                rb.id, mode=RunMode.DRY_RUN, principal="test-user", ip="127.0.0.1"
            )
            assert result.outcome == RunOutcome.DRY_RUN_STORED

        # The (N+1)th trigger should be denied by the rate-limit gate.
        denied = await orch.handle_operator_trigger(
            rb.id, mode=RunMode.DRY_RUN, principal="test-user", ip="127.0.0.1"
        )

    assert denied.outcome == RunOutcome.DENIED
    assert denied.denial_reason is not None
    # Denial reason string form varies by codebase; require RATE_LIMIT-like.
    reason_str = str(denied.denial_reason).lower()
    assert "rate" in reason_str or "limit" in reason_str, (
        f"expected rate-limit denial, got: {denied.denial_reason}"
    )
