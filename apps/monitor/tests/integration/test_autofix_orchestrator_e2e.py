"""Integration test: AutoFixOrchestrator end-to-end (STAGE-009-005 Refinement).

Drives AutoFixOrchestrator.handle_alert against a REAL fixer-runner container
running the FAKE claude binary, proving the full exec path:

  match -> gates -> docker-exec fake claude -> capture transcript/stdout/stderr/exit
  -> persist runbook_runs (started + completed) + alert_outcomes(auto_fixed) + audit_log

Acceptance criteria (all must pass):
  1. handle_alert returns RunResult with ran=True / outcome=RAN for a matching alert.
  2. Fake claude ACTUALLY executed (transcript file appears in shared transcript dir).
  3. runbook_runs row: started_at + ended_at set, exit_code==0, mode='real',
     fixer_user='homelab-fixer', runbook_hash set, transcript_path discovered.
  4. alert_outcomes row with outcome='auto_fixed'.
  5. audit_log row recording 'autofix.ran'.
  6. exec-log sibling file written to exec_log_dir.
  7. Second call for same alert/runbook is DENIED by rate_limit (rate_limit_per_hour=1)
     with NO new runbook_runs row and an 'autofix.denied' audit entry.

Rig-gated via require_docker() -- SKIPS FAST when Docker is unavailable.

Run via:
    make integration
    pytest -m integration apps/monitor/tests/integration/test_autofix_orchestrator_e2e.py
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import Alert, AlertStatus, Severity
from homelab_monitor.kernel.autofix.approvals_repository import (
    RunbookRunApprovalsRepository,
)
from homelab_monitor.kernel.autofix.orchestrator import AutoFixOrchestrator
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import DenialReason, RunOutcome
from homelab_monitor.kernel.config import FixerRunnerConfig
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.socket_client import DockerSocketClient
from homelab_monitor.kernel.runbooks.repository import RunbookRepo
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

from .helpers.rig_health import require_docker

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXER_RUNNER_DIR = (
    Path(__file__).parent.parent.parent.parent.parent  # repo root
    / "deploy"
    / "compose"
    / "fixer-runner"
)

_FIXER_UID = 1002
_FIXER_GID = 1002

# alertname used for matching — must be unique enough not to collide with prod alerts
_TEST_ALERTNAME = "TestAutoFixOrchestratorE2E"


# ---------------------------------------------------------------------------
# Docker helpers (mirrors test_fixer_runner.py)
# ---------------------------------------------------------------------------


def _docker(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Module-scoped image fixture (builds once for all tests in this module)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixer_image() -> Iterator[str]:
    """Build the fixer-runner image (fake claude) once; remove after module."""
    require_docker()
    tag = f"homelab-monitor-fixer-runner-e2e-test:{uuid.uuid4().hex[:12]}"
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


# ---------------------------------------------------------------------------
# Per-test fixtures: container + isolated DB + tmp dirs + orchestrator
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Return (transcript_dir, exec_log_dir) — both world-writable."""
    transcript_dir = tmp_path / "transcripts"
    exec_log_dir = tmp_path / "exec-logs"
    transcript_dir.mkdir()
    exec_log_dir.mkdir()
    transcript_dir.chmod(0o777)
    exec_log_dir.chmod(0o777)
    return transcript_dir, exec_log_dir


@pytest.fixture
def running_container(
    fixer_image: str, tmp_dirs: tuple[Path, Path]
) -> Iterator[tuple[str, Path, Path]]:
    """Start a keepalive container; yield (name, transcript_dir, exec_log_dir); teardown."""
    require_docker()
    transcript_dir, exec_log_dir = tmp_dirs
    container_name = f"fixer-runner-e2e-test-{uuid.uuid4().hex[:12]}"
    run = _docker(
        "run",
        "-d",
        "--name",
        container_name,
        "-v",
        f"{transcript_dir}:/data/runbook-transcripts",
        fixer_image,
        timeout=60.0,
    )
    if run.returncode != 0:
        pytest.fail(f"fixer-runner container failed to start:\n{run.stdout}\n{run.stderr}")
    try:
        yield container_name, transcript_dir, exec_log_dir
    finally:
        _docker("rm", "-f", container_name, timeout=60.0)


@pytest_asyncio.fixture
async def db_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Fresh migrated SQLite engine for this test."""
    db_file = tmp_path / "test-e2e.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    alembic_upgrade_head(db_url)
    engine = get_engine(url=db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def repo(db_engine: AsyncEngine) -> SqliteRepository:
    return SqliteRepository(engine=db_engine)


@pytest_asyncio.fixture
async def master_key() -> bytes:
    return os.urandom(32)


@pytest_asyncio.fixture
async def secrets_repo(repo: SqliteRepository, master_key: bytes) -> AsyncSecretsRepository:
    return AsyncSecretsRepository(repo, master_key)


def _make_orchestrator(  # noqa: PLR0913
    *,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    docker_client: DockerSocketClient,
    container_name: str,
    transcript_dir: Path,
    exec_log_dir: Path,
    rate_limit_per_hour: int | None = 1,
) -> AutoFixOrchestrator:
    """Build a fully-wired orchestrator pointed at the test container."""
    log = structlog.get_logger()
    config = FixerRunnerConfig(
        container=container_name,
        transcript_dir=str(transcript_dir),
        exec_log_dir=str(exec_log_dir),
        fixer_user="homelab-fixer",
        exec_timeout_seconds=60.0,
    )
    return AutoFixOrchestrator(
        runbook_repo=RunbookRepo(repo),
        alert_repo=AlertRepository(repo),
        app_settings_repo=AppSettingsRepository(repo),
        secrets_repo=secrets_repo,
        docker_client=docker_client,
        db=repo,
        runs_repo=RunbookRunsRepository(repo),
        approvals_repo=RunbookRunApprovalsRepository(repo),
        config=config,
        log=log,
    )


async def _insert_test_runbook(  # noqa: PLR0913 -- keyword-only test-fixture insert
    repo: SqliteRepository,
    *,
    runbook_path: str,
    rate_limit_per_hour: int | None = 1,
    risk_tag: str = "safe",
    dry_run_required: bool = False,
    content_hash: str = "test-content-hash-abc123",
) -> str:
    """Directly insert a runbook row ready for the orchestrator."""
    runbook_id = uuid7()
    matcher: list[dict[str, object]] = [{"alertname": _TEST_ALERTNAME, "labels": {}}]
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
                "id": runbook_id,
                "path": runbook_path,
                "created_at": utc_now_iso(),
                "patterns": json.dumps(matcher),
                "risk_tag": risk_tag,
                "dry_run": 1 if dry_run_required else 0,
                "rate_limit": rate_limit_per_hour,
                "cooldown": 0,
                "enabled": 1,
                "auto_trigger": 1,
                "hash": content_hash,
            },
        )
    return runbook_id


async def _insert_test_alert(repo: SqliteRepository) -> Alert:
    """Insert a firing alert for the test alertname; return the Alert."""
    alert_id = uuid7()
    fingerprint = f"test-fp-{uuid.uuid4().hex[:8]}"
    now = utc_now_iso()
    payload = {
        "labels": {"alertname": _TEST_ALERTNAME, "severity": "warning"},
        "annotations": {},
    }
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO alerts "
                "(id, fingerprint, source_tool, severity, status, "
                " opened_at, last_seen_at, payload_json, created_at) "
                "VALUES (:id, :fp, :st, :sev, :status, :opened, :last_seen, :pj, :created)"
            ),
            {
                "id": alert_id,
                "fp": fingerprint,
                "st": "vmalert",
                "sev": "warning",
                "status": "firing",
                "opened": now,
                "last_seen": now,
                "pj": json.dumps(payload, sort_keys=True),
                "created": now,
            },
        )
    return Alert(
        id=alert_id,
        fingerprint=fingerprint,
        source_tool="vmalert",
        severity=Severity.WARNING,
        status=AlertStatus.FIRING,
        opened_at=now,
        last_seen_at=now,
        payload=payload,
        labels={"alertname": _TEST_ALERTNAME, "severity": "warning"},
        annotations={},
    )


# ---------------------------------------------------------------------------
# The main E2E test
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_autofix_orchestrator_real_exec_e2e(  # noqa: PLR0915
    running_container: tuple[str, Path, Path],
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Full E2E: match -> gate -> exec fake claude -> persist all records.

    Verifies ALL 7 acceptance criteria for STAGE-009-005 Refinement.
    """
    container_name, transcript_dir, exec_log_dir = running_container

    # The runbook path must exist IN THE CONTAINER.  The fake claude just needs
    # a -p argument; it doesn't read the folder.  Use the container's
    # /data/runbook-transcripts mount point, which is guaranteed to exist.
    runbook_path = "/data/runbook-transcripts"

    # Wire up the kill switch: autofix_enabled = "true"
    app_settings_repo = AppSettingsRepository(repo)
    await app_settings_repo.set("autofix_enabled", "true")

    # Insert test runbook (rate_limit=1/h so the second call is denied)
    runbook_id = await _insert_test_runbook(
        repo,
        runbook_path=runbook_path,
        rate_limit_per_hour=1,
    )

    # Insert test alert
    alert = await _insert_test_alert(repo)

    # Build real DockerSocketClient + orchestrator
    log = structlog.get_logger()
    docker_client = DockerSocketClient(log=log)
    try:
        orchestrator = _make_orchestrator(
            repo=repo,
            secrets_repo=secrets_repo,
            docker_client=docker_client,
            container_name=container_name,
            transcript_dir=transcript_dir,
            exec_log_dir=exec_log_dir,
        )

        # ---- FIRST CALL: should RAN ----
        result = await orchestrator.handle_alert(alert)

        # Assertion 1: RunResult.ran=True, outcome=RAN
        assert result is not None, "handle_alert returned None (no-match) — expected a match"
        assert result.ran is True, f"Expected ran=True, got: {result}"
        assert result.outcome == RunOutcome.RAN, f"Expected outcome=RAN, got: {result.outcome}"
        assert result.exit_code == 0, f"Expected exit_code=0, got: {result.exit_code}"
        run_id = result.run_id
        assert run_id is not None, "run_id should be set for a ran result"

        # Assertion 2: fake claude actually executed — transcript file appeared
        transcript_files = list(transcript_dir.glob("fake-claude-*.transcript"))
        assert len(transcript_files) >= 1, (
            f"No fake-claude-*.transcript in {transcript_dir}: "
            f"files={list(transcript_dir.iterdir())}"
        )
        args_files = list(transcript_dir.glob("fake-claude-*.args"))
        assert len(args_files) >= 1, f"No fake-claude-*.args in {transcript_dir}"
        argv_text = args_files[0].read_text(encoding="utf-8")
        assert "-p" in argv_text.splitlines(), f"Expected -p in argv; got:\n{argv_text}"
        assert "--dangerously-skip-permissions" in argv_text.splitlines(), (
            f"Expected --dangerously-skip-permissions in argv; got:\n{argv_text}"
        )

        # Assertion 3: runbook_runs row is complete
        runs_rows = await repo.fetch_all(
            text("SELECT * FROM runbook_runs WHERE id = :id"),
            {"id": run_id},
        )
        assert len(runs_rows) == 1, (
            f"Expected 1 runbook_runs row for {run_id}, got {len(runs_rows)}"
        )
        run_row = runs_rows[0]
        assert run_row.started_at is not None, "started_at must be set"
        assert run_row.ended_at is not None, "ended_at must be set"
        assert int(run_row.exit_code) == 0, f"exit_code must be 0, got {run_row.exit_code}"
        assert str(run_row.mode) == "real", f"mode must be 'real', got {run_row.mode}"
        assert str(run_row.fixer_user) == "homelab-fixer", (
            f"fixer_user must be 'homelab-fixer', got {run_row.fixer_user}"
        )
        assert run_row.host is not None and str(run_row.host) != "", (
            f"host must be set, got {run_row.host}"
        )
        assert str(run_row.runbook_hash) == "test-content-hash-abc123", (
            f"runbook_hash mismatch: {run_row.runbook_hash}"
        )
        assert run_row.transcript_path is not None, "transcript_path must be discovered and set"
        assert str(run_row.alert_id) == alert.id, (
            f"alert_id mismatch: {run_row.alert_id} != {alert.id}"
        )

        # Assertion 4: alert_outcomes row with outcome='auto_fixed'
        outcome_rows = await repo.fetch_all(
            text("SELECT * FROM alert_outcomes WHERE alert_id = :alert_id"),
            {"alert_id": alert.id},
        )
        assert len(outcome_rows) >= 1, (
            f"No alert_outcomes row for alert {alert.id}; rows={outcome_rows}"
        )
        outcomes = [str(r.outcome) for r in outcome_rows]
        assert "auto_fixed" in outcomes, f"Expected 'auto_fixed' in alert_outcomes, got: {outcomes}"

        # Assertion 5: audit_log row with what='autofix.ran'
        audit_rows = await repo.fetch_all(
            text(
                "SELECT * FROM audit_log WHERE what = 'autofix.ran' "
                "AND json_extract(after_json, '$.run_id') = :run_id"
            ),
            {"run_id": run_id},
        )
        assert len(audit_rows) >= 1, f"No audit_log row with what='autofix.ran' for run_id={run_id}"

        # Assertion 6: exec-log file written to exec_log_dir
        exec_log_files = list(exec_log_dir.glob(f"{run_id}.exec.log"))
        assert len(exec_log_files) == 1, (
            f"Expected exec-log {run_id}.exec.log in {exec_log_dir}, "
            f"found: {list(exec_log_dir.iterdir())}"
        )
        exec_log_text = exec_log_files[0].read_text(encoding="utf-8")
        assert f"run_id={run_id}" in exec_log_text, (
            f"exec.log missing run_id header:\n{exec_log_text}"
        )

        # Assertion 7: second call for same alert/runbook is DENIED by rate_limit
        # (rate_limit_per_hour=1 and we just ran once)
        result2 = await orchestrator.handle_alert(alert)
        assert result2 is not None, "Second handle_alert returned None (unexpected no-match)"
        assert result2.ran is False, f"Expected second call denied (ran=False), got: {result2}"
        assert result2.outcome == RunOutcome.DENIED, (
            f"Expected outcome=DENIED for second call, got: {result2.outcome}"
        )

        # No new runbook_runs row (still exactly 1 row for this runbook)
        all_runs = await repo.fetch_all(
            text("SELECT id FROM runbook_runs WHERE runbook_id = :rbid"),
            {"rbid": runbook_id},
        )
        assert len(all_runs) == 1, (
            f"Expected exactly 1 runbook_runs row after denied second call, got {len(all_runs)}"
        )

        # audit_log row for 'autofix.denied' (the denial)
        denied_audit = await repo.fetch_all(
            text(
                "SELECT * FROM audit_log WHERE what = 'autofix.denied' "
                "AND json_extract(after_json, '$.alert_id') = :alert_id"
            ),
            {"alert_id": alert.id},
        )
        assert len(denied_audit) >= 1, (
            f"No audit_log row with what='autofix.denied' for alert {alert.id}"
        )

    finally:
        await docker_client.aclose()


# ---------------------------------------------------------------------------
# STAGE-009-006: dry-run -> approval -> real-run E2E pipeline
# ---------------------------------------------------------------------------


def _read_argv_files(transcript_dir: Path) -> list[list[str]]:
    """Read every fake-claude-*.args file's argv (as a list of lines)."""
    return [
        f.read_text(encoding="utf-8").splitlines()
        for f in sorted(transcript_dir.glob("fake-claude-*.args"))
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_autofix_dry_run_approval_real_run_e2e_pipeline(  # noqa: PLR0915
    running_container: tuple[str, Path, Path],
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Full E2E: risky runbook -> dry-run stored -> approval -> real run.

    Proves the STAGE-009-006 safety chain against a REAL fixer-runner
    container: a risky runbook can never bypass dry-run, and only an
    explicit execute_approved() call triggers the real
    --dangerously-skip-permissions exec.
    """
    container_name, transcript_dir, exec_log_dir = running_container
    runbook_path = "/data/runbook-transcripts"

    app_settings_repo = AppSettingsRepository(repo)
    await app_settings_repo.set("autofix_enabled", "true")

    runbook_id = await _insert_test_runbook(
        repo,
        runbook_path=runbook_path,
        rate_limit_per_hour=None,  # no rate limit — this test drives 2 real-ish attempts
        risk_tag="risky",
        dry_run_required=True,
        content_hash="dry-run-pinned-hash-001",
    )
    alert = await _insert_test_alert(repo)

    log = structlog.get_logger()
    docker_client = DockerSocketClient(log=log)
    try:
        orchestrator = _make_orchestrator(
            repo=repo,
            secrets_repo=secrets_repo,
            docker_client=docker_client,
            container_name=container_name,
            transcript_dir=transcript_dir,
            exec_log_dir=exec_log_dir,
            rate_limit_per_hour=None,
        )

        # ---- Phase 1: dry-run stored ----
        result1 = await orchestrator.handle_alert(alert)
        assert result1 is not None, "handle_alert returned None (no-match) — expected a match"
        assert result1.ran is True, f"Expected ran=True for dry run, got: {result1}"
        assert result1.outcome == RunOutcome.DRY_RUN_STORED, (
            f"Expected outcome=DRY_RUN_STORED, got: {result1.outcome}"
        )
        approval_id = result1.approval_id
        assert approval_id is not None, "approval_id must be set for a dry-run-stored result"
        dry_run_id = result1.run_id
        assert dry_run_id is not None, "run_id must be set for the dry run"

        dry_runs_rows = await repo.fetch_all(
            text("SELECT * FROM runbook_runs WHERE id = :id"),
            {"id": dry_run_id},
        )
        assert len(dry_runs_rows) == 1, f"Expected 1 runbook_runs row for dry run {dry_run_id}"
        dry_row = dry_runs_rows[0]
        assert str(dry_row.mode) == "dry_run", f"mode must be 'dry_run', got {dry_row.mode}"
        assert dry_row.transcript_path is not None, "dry run transcript_path must be set"

        # Fake-claude was invoked with --permission-mode plan, WITHOUT
        # --dangerously-skip-permissions.
        argvs_after_phase1 = _read_argv_files(transcript_dir)
        assert len(argvs_after_phase1) == 1, (
            f"Expected exactly 1 fake-claude invocation after dry run, got "
            f"{len(argvs_after_phase1)}"
        )
        dry_argv = argvs_after_phase1[0]
        assert "--permission-mode" in dry_argv and "plan" in dry_argv, (
            f"Expected --permission-mode plan in dry argv; got:\n{dry_argv}"
        )
        assert "--dangerously-skip-permissions" not in dry_argv, (
            f"Dry run must NOT pass --dangerously-skip-permissions; got:\n{dry_argv}"
        )

        approval_rows = await repo.fetch_all(
            text("SELECT * FROM runbook_run_approvals WHERE id = :id"),
            {"id": approval_id},
        )
        assert len(approval_rows) == 1, f"Expected 1 approval row for {approval_id}"
        approval_row = approval_rows[0]
        assert str(approval_row.status) == "pending", (
            f"Expected status='pending', got {approval_row.status}"
        )
        assert str(approval_row.pinned_runbook_hash) == "dry-run-pinned-hash-001", (
            f"pinned_runbook_hash mismatch: {approval_row.pinned_runbook_hash}"
        )

        dry_stored_audit = await repo.fetch_all(
            text(
                "SELECT * FROM audit_log WHERE what = 'autofix.dry_run_stored' "
                "AND json_extract(after_json, '$.run_id') = :run_id"
            ),
            {"run_id": dry_run_id},
        )
        assert len(dry_stored_audit) >= 1, (
            f"No audit_log row with what='autofix.dry_run_stored' for run_id={dry_run_id}"
        )

        # ---- Phase 2: risky runbook can NEVER bypass dry-run ----
        # No second runbook_runs row exists yet; only the dry invocation was recorded.
        all_runs_after_phase1 = await repo.fetch_all(
            text("SELECT id FROM runbook_runs WHERE runbook_id = :rbid"),
            {"rbid": runbook_id},
        )
        assert len(all_runs_after_phase1) == 1, (
            f"Expected exactly 1 runbook_runs row before approval, got {len(all_runs_after_phase1)}"
        )
        real_exec_argvs = [a for a in argvs_after_phase1 if "--dangerously-skip-permissions" in a]
        assert len(real_exec_argvs) == 0, (
            "No real (--dangerously-skip-permissions) exec should have happened yet"
        )

        # ---- Phase 3: explicit approval -> real run ----
        result3 = await orchestrator.execute_approved(
            approval_id, principal="test-user", ip="127.0.0.1"
        )
        assert result3.ran is True, f"Expected ran=True for approved real run, got: {result3}"
        assert result3.outcome == RunOutcome.RAN, (
            f"Expected outcome=RAN for approved real run, got: {result3.outcome}"
        )
        real_run_id = result3.run_id
        assert real_run_id is not None, "run_id must be set for the real run"
        assert real_run_id != dry_run_id, "real run_id must be distinct from the dry run_id"

        real_runs_rows = await repo.fetch_all(
            text("SELECT * FROM runbook_runs WHERE id = :id"),
            {"id": real_run_id},
        )
        assert len(real_runs_rows) == 1, f"Expected 1 runbook_runs row for real run {real_run_id}"
        real_row = real_runs_rows[0]
        assert str(real_row.mode) == "real", f"mode must be 'real', got {real_row.mode}"
        assert real_row.transcript_path is not None, "real run transcript_path must be set"
        assert str(real_row.transcript_path) != str(dry_row.transcript_path), (
            "real run must have its own distinct transcript_path"
        )

        # Fake-claude was invoked with --dangerously-skip-permissions for the real run.
        argvs_after_phase3 = _read_argv_files(transcript_dir)
        assert len(argvs_after_phase3) == 2, (  # noqa: PLR2004 -- exactly one dry + one real = 2
            f"Expected exactly 2 fake-claude invocations total, got {len(argvs_after_phase3)}"
        )
        real_argv_candidates = [
            a for a in argvs_after_phase3 if "--dangerously-skip-permissions" in a
        ]
        assert len(real_argv_candidates) == 1, (
            f"Expected exactly 1 real (--dangerously-skip-permissions) argv, got:\n"
            f"{argvs_after_phase3}"
        )

        approval_rows_final = await repo.fetch_all(
            text("SELECT * FROM runbook_run_approvals WHERE id = :id"),
            {"id": approval_id},
        )
        assert len(approval_rows_final) == 1
        approval_final = approval_rows_final[0]
        assert str(approval_final.status) == "approved", (
            f"Expected status='approved', got {approval_final.status}"
        )
        assert approval_final.decided_at is not None, "decided_at must be set after approval"
        assert str(approval_final.real_run_id) == real_run_id, (
            f"real_run_id mismatch: {approval_final.real_run_id} != {real_run_id}"
        )

        approved_audit = await repo.fetch_all(
            text(
                "SELECT * FROM audit_log WHERE what = 'autofix.approved' "
                "AND json_extract(after_json, '$.approval_id') = :approval_id"
            ),
            {"approval_id": approval_id},
        )
        assert len(approved_audit) >= 1, (
            f"No audit_log row with what='autofix.approved' for approval_id={approval_id}"
        )

        ran_audit = await repo.fetch_all(
            text(
                "SELECT * FROM audit_log WHERE what = 'autofix.ran' "
                "AND json_extract(after_json, '$.run_id') = :run_id"
            ),
            {"run_id": real_run_id},
        )
        assert len(ran_audit) >= 1, (
            f"No audit_log row with what='autofix.ran' for run_id={real_run_id}"
        )

    finally:
        await docker_client.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_autofix_approval_drift_invalidates_e2e(
    running_container: tuple[str, Path, Path],
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """A runbook content_hash change between dry-run and approval must
    invalidate the pending approval (RUNBOOK_CHANGED) and skip the real exec.
    """
    container_name, transcript_dir, exec_log_dir = running_container
    runbook_path = "/data/runbook-transcripts"

    app_settings_repo = AppSettingsRepository(repo)
    await app_settings_repo.set("autofix_enabled", "true")

    runbook_id = await _insert_test_runbook(
        repo,
        runbook_path=runbook_path,
        rate_limit_per_hour=None,
        risk_tag="risky",
        dry_run_required=True,
        content_hash="drift-original-hash-001",
    )
    alert = await _insert_test_alert(repo)

    log = structlog.get_logger()
    docker_client = DockerSocketClient(log=log)
    try:
        orchestrator = _make_orchestrator(
            repo=repo,
            secrets_repo=secrets_repo,
            docker_client=docker_client,
            container_name=container_name,
            transcript_dir=transcript_dir,
            exec_log_dir=exec_log_dir,
            rate_limit_per_hour=None,
        )

        # Produce a pending approval via a dry run.
        result1 = await orchestrator.handle_alert(alert)
        assert result1 is not None
        assert result1.outcome == RunOutcome.DRY_RUN_STORED, (
            f"Expected DRY_RUN_STORED, got: {result1.outcome}"
        )
        approval_id = result1.approval_id
        assert approval_id is not None

        argvs_before = _read_argv_files(transcript_dir)
        assert len(argvs_before) == 1, "Expected exactly 1 dry-claude invocation so far"

        # Simulate a config change post-plan: mutate the runbook's content_hash.
        async with repo.transaction() as conn:
            await conn.execute(
                text("UPDATE runbooks SET content_hash = :hash WHERE id = :id"),
                {"hash": "drift-mutated-hash-002", "id": runbook_id},
            )

        result2 = await orchestrator.execute_approved(
            approval_id, principal="test-user", ip="127.0.0.1"
        )
        assert result2.ran is False, f"Expected ran=False on drift, got: {result2}"
        assert result2.denial_reason == DenialReason.RUNBOOK_CHANGED, (
            f"Expected denial_reason=RUNBOOK_CHANGED, got: {result2.denial_reason}"
        )

        approval_rows = await repo.fetch_all(
            text("SELECT * FROM runbook_run_approvals WHERE id = :id"),
            {"id": approval_id},
        )
        assert len(approval_rows) == 1
        assert str(approval_rows[0].status) == "rejected", (
            f"Expected status='rejected' after drift, got {approval_rows[0].status}"
        )

        # No real exec happened: still only the original dry-claude invocation,
        # and no runbook_runs row with mode='real'.
        argvs_after = _read_argv_files(transcript_dir)
        assert len(argvs_after) == 1, (
            f"No new fake-claude invocation should have happened on drift; got {len(argvs_after)}"
        )
        real_mode_runs = await repo.fetch_all(
            text("SELECT id FROM runbook_runs WHERE runbook_id = :rbid AND mode = 'real'"),
            {"rbid": runbook_id},
        )
        assert len(real_mode_runs) == 0, (
            f"Expected NO mode='real' runbook_runs row after drift, got {len(real_mode_runs)}"
        )

        rejected_audit = await repo.fetch_all(
            text(
                "SELECT * FROM audit_log WHERE what = 'autofix.rejected' "
                "AND json_extract(after_json, '$.approval_id') = :approval_id"
            ),
            {"approval_id": approval_id},
        )
        assert len(rejected_audit) >= 1, (
            f"No audit_log row with what='autofix.rejected' for approval_id={approval_id}"
        )

    finally:
        await docker_client.aclose()
