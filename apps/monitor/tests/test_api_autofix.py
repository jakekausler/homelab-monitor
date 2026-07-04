"""API endpoint tests for the auto-fix approval flow (STAGE-009-006).

Tests the router endpoints: GET /api/autofix/approvals, GET /api/autofix/approvals/{id}/plan,
POST /api/autofix/approvals/{id}/approve, POST /api/autofix/approvals/{id}/reject.

Uses authenticated_client fixture (session + CSRF); orchestrator wired via app.state.
Covers all branches: auth, CSRF, drift detection, 404/409/403 errors, happy paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text

from homelab_monitor.kernel.autofix.approvals_repository import (
    RunbookRunApprovalsRepository,
)
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import RunMode
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.socket_client import ExecResult
from homelab_monitor.kernel.runbooks.repository import RunbookRepo
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository
from homelab_monitor.kernel.security.pin import PIN_HASH_KEY, hash_pin
from tests.test_api_runbooks import (
    _valid_config_dict,  # pyright: ignore[reportPrivateUsage]
    _write_runbook,  # pyright: ignore[reportPrivateUsage]
)

# Import test fixtures and helpers from orchestrator tests. These are underscore-
# private factories shared across the autofix test suite; the codebase idiom for
# cross-test-file helper reuse is a per-name reportPrivateUsage suppression (see
# tests/test_drain_metrics.py / test_drain_models_debug.py).
from tests.test_autofix_orchestrator import (
    _FakeDockerClient,  # pyright: ignore[reportPrivateUsage]
    _insert_alert,  # pyright: ignore[reportPrivateUsage]
    _insert_runbook,  # pyright: ignore[reportPrivateUsage]
    _make_alert,  # pyright: ignore[reportPrivateUsage]
    _make_orchestrator,  # pyright: ignore[reportPrivateUsage]
    _make_runbook_record,  # pyright: ignore[reportPrivateUsage]
)


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Extract CSRF token from client cookies (empty string when absent)."""
    csrf = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


async def _seed_dry_run_chain(
    repo: SqliteRepository,
    *,
    alertname: str = "TestAlert",
    content_hash: str = "hash-v1",
) -> tuple[str, str, str]:
    """Seed the full FK chain runbooks → alerts → runbook_runs (mode=dry_run).

    Returns ``(runbook_id, alert_id, dry_run_id)`` — sufficient to satisfy the
    ``runbook_run_approvals.dry_run_id`` FK to ``runbook_runs.id``.
    """
    rb = _make_runbook_record(alertname=alertname, content_hash=content_hash)
    await _insert_runbook(repo, rb)
    alert = _make_alert(alertname=alertname)
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
    return rb.id, alert.id, dry_run_id


# ---------------------------------------------------------------------------
# R1-R16 Router tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_approvals_pending_drift_flags(
    authenticated_client: AsyncClient, repo: SqliteRepository, tmp_path: Path
) -> None:
    """R1: GET /api/autofix/approvals?status_filter=pending with drift detection."""
    rb1 = _make_runbook_record(runbook_id=uuid7(), alertname="Alert1", content_hash="hash-v1")
    await _insert_runbook(repo, rb1)

    rb2 = _make_runbook_record(runbook_id=uuid7(), alertname="Alert2", content_hash="hash-v2")
    await _insert_runbook(repo, rb2)

    # Seed alerts + runbook_runs rows so the approvals' dry_run_id FK is satisfied.
    alert1 = _make_alert(alertname="Alert1")
    await _insert_alert(repo, alert1)
    alert2 = _make_alert(alertname="Alert2")
    await _insert_alert(repo, alert2)

    # Create two approvals
    approvals_repo = RunbookRunApprovalsRepository(repo)
    runs_repo = RunbookRunsRepository(repo)

    async with repo.transaction() as conn:
        run_id_1 = await runs_repo.insert_started(
            conn,
            runbook_id=rb1.id,
            alert_id=alert1.id,
            prompt=rb1.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb1.content_hash,
            mode=RunMode.DRY_RUN,
            initiated_by="alert",
        )
        run_id_2 = await runs_repo.insert_started(
            conn,
            runbook_id=rb2.id,
            alert_id=alert2.id,
            prompt=rb2.path,
            fixer_user="homelab-fixer",
            host="testhost",
            runbook_hash=rb2.content_hash,
            mode=RunMode.DRY_RUN,
            initiated_by="alert",
        )
        approval_id_1 = await approvals_repo.insert_pending(
            conn,
            dry_run_id=run_id_1,
            runbook_id=rb1.id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )
        approval_id_2 = await approvals_repo.insert_pending(
            conn,
            dry_run_id=run_id_2,
            runbook_id=rb2.id,
            alert_id=None,
            pinned_runbook_hash="hash-v2",
        )

    # rb1: no drift (hash matches)
    # rb2: drift (update hash to v3)
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE runbooks SET content_hash = :hash WHERE id = :id"),
            {"id": rb2.id, "hash": "hash-v3"},
        )

    response = await authenticated_client.get(
        "/api/autofix/approvals", params={"status_filter": "pending"}
    )
    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert "items" in data
    items = data["items"]
    assert len(items) == 2  # noqa: PLR2004

    # Find by approval_id
    item1 = next((i for i in items if i["id"] == approval_id_1), None)
    item2 = next((i for i in items if i["id"] == approval_id_2), None)

    assert item1 is not None
    assert item1["drift_detected"] is False

    assert item2 is not None
    assert item2["drift_detected"] is True


@pytest.mark.asyncio
async def test_list_approvals_drift_when_runbook_missing(
    authenticated_client: AsyncClient, repo: SqliteRepository
) -> None:
    """R1b: GET /api/autofix/approvals?status_filter=pending flags drift when the
    referenced runbook no longer exists (covers _drift_for's ``record is None``
    branch — router line 128 ``return True``).

    ``runbook_run_approvals.runbook_id`` has NO FK to ``runbooks.id`` (see
    migration 0047), so an approval can validly reference a non-existent runbook
    id. The dry_run_id FK still needs a real ``runbook_runs`` row, hence the
    ``_seed_dry_run_chain`` helper for the valid parent chain.
    """
    _rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    # Insert an approval whose runbook_id references a runbook that does NOT
    # exist. RunbookRepo.get_runbook(...) will return None, exercising the
    # missing-runbook branch of _drift_for.
    missing_runbook_id = "nonexistent-runbook-id"
    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=missing_runbook_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    response = await authenticated_client.get(
        "/api/autofix/approvals", params={"status_filter": "pending"}
    )
    assert response.status_code == 200  # noqa: PLR2004
    items = response.json()["items"]
    item = next((i for i in items if i["id"] == approval_id), None)
    assert item is not None
    assert item["drift_detected"] is True


@pytest.mark.asyncio
async def test_list_approvals_requires_session(
    unauthenticated_client: AsyncClient,
) -> None:
    """R2: GET /api/autofix/approvals without auth → 401."""
    response = await unauthenticated_client.get("/api/autofix/approvals")
    assert response.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_plan_happy(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """R3: GET /api/autofix/approvals/{id}/plan with real orchestrator."""
    # Set up orchestrator on app.state
    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    exec_log_dir = str(tmp_path / "exec-logs")
    os.makedirs(exec_log_dir, exist_ok=True)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    # runbook_runs.runbook_id FKs runbooks.id (NOT NULL, enforced): seed parent.
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    # Create a dry run with a transcript file
    plan_content = "PLAN: dry-run analysis\n"
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
                "mode": "dry_run",
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

    # Create approval
    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=run_id,
            runbook_id=rb.id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    response = await authenticated_client.get(f"/api/autofix/approvals/{approval_id}/plan")
    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["approval_id"] == approval_id
    assert data["dry_run_id"] == run_id
    assert data["plan_text"] == plan_content
    assert data["exit_code"] == 0


@pytest.mark.asyncio
async def test_get_plan_approval_not_found(
    authenticated_client: AsyncClient,
    secrets_repo: AsyncSecretsRepository,
    repo: SqliteRepository,
) -> None:
    """R4: GET /plan unknown approval → 404."""
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.get("/api/autofix/approvals/nonexistent/plan")
    assert response.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_plan_transcript_missing(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """R5: GET /plan dry run without transcript → 404."""
    transcript_dir = str(tmp_path / "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(
        repo,
        secrets_repo,
        docker,
        transcript_dir=transcript_dir,
    )
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    # runbook_runs.runbook_id FKs runbooks.id (NOT NULL, enforced): seed parent.
    rb = _make_runbook_record(alertname="TestAlert")
    await _insert_runbook(repo, rb)

    # Dry run with no transcript file
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
                "mode": "dry_run",
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

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=run_id,
            runbook_id=rb.id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    response = await authenticated_client.get(f"/api/autofix/approvals/{approval_id}/plan")
    assert response.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_approve_happy(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """R6: POST /approve happy path → real exec fires."""
    rb = _make_runbook_record(
        alertname="Test",
        dry_run_required=True,
        content_hash="hash-v1",
        runbook_dir=tmp_path / "runbook",
    )
    await _insert_runbook(repo, rb)

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
        secrets_repo,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    alert = _make_alert(alertname="Test")
    await _insert_alert(repo, alert)

    # Create dry run + approval via orchestrator
    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        dry_result = await orch.handle_alert(alert)

    assert dry_result is not None
    approval_id = dry_result.approval_id
    assert approval_id is not None

    # Switch docker to real exec
    docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
    docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        response = await authenticated_client.post(
            f"/api/autofix/approvals/{approval_id}/approve",
            json={"confirm_phrase": "approve"},
            headers=_csrf(authenticated_client),
        )

    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["approval_id"] == approval_id
    assert data["ran"] is True
    assert data["outcome"] == "ran"
    assert data["real_run_id"] is not None


@pytest.mark.asyncio
async def test_approve_confirm_mismatch(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    tmp_path: Path,
) -> None:
    """R7: POST /approve wrong confirm_phrase → 400."""
    # Seed runbook + alert + runbook_runs so the approval FK is satisfied.
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    # Create a dummy approval
    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={"confirm_phrase": "nope"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 400  # noqa: PLR2004
    assert "approve" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_approve_not_found(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """R8: POST /approve unknown approval → 404."""
    # Wire an orchestrator so the endpoint isn't 503 (autofix_unavailable).
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.post(
        "/api/autofix/approvals/nonexistent/approve",
        json={"confirm_phrase": "approve"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_approve_not_pending(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """R9: POST /approve status != pending → 409."""
    # Seed runbook + alert + runbook_runs so the approval FK is satisfied.
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )
        # Mark as approved
        await approvals_repo.mark_approved_conn(
            conn,
            approval_id=approval_id,
            approved_by="admin",
            when=utc_now_iso(),
        )

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={"confirm_phrase": "approve"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["error"]["code"] == "approval_not_pending"


@pytest.mark.asyncio
async def test_approve_drift_conflict_and_rejects(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """R10: POST /approve runbook hash changed → 409 + auto-reject.

    I3: the drift check + rejection + enriched audit are owned by the
    orchestrator (execute_approved), not the router. The end-to-end contract
    (approval ends rejected, 409 with code=runbook_changed_since_plan, one
    autofix.rejected audit row) is unchanged, but the audit ``after_json`` is
    now the enriched orchestrator shape: {approval_id, runbook_id,
    gate='runbook_changed', pinned_runbook_hash, current_runbook_hash}.
    """
    rb = _make_runbook_record(
        alertname="Test",
        dry_run_required=True,
        content_hash="hash-v1",
        runbook_dir=tmp_path / "runbook",
    )
    await _insert_runbook(repo, rb)

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
        secrets_repo,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    alert = _make_alert(alertname="Test")
    await _insert_alert(repo, alert)

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        dry_result = await orch.handle_alert(alert)

    assert dry_result is not None
    approval_id = dry_result.approval_id
    assert approval_id is not None

    # Change runbook hash
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE runbooks SET content_hash = :hash WHERE id = :id"),
            {"id": rb.id, "hash": "hash-v2"},
        )

    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={"confirm_phrase": "approve"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["error"]["code"] == "runbook_changed_since_plan"

    # Verify approval auto-rejected
    approvals_repo = RunbookRunApprovalsRepository(repo)
    approval = await approvals_repo.get(approval_id)
    assert approval is not None
    assert approval.status == "rejected"

    # I3: the enriched autofix.rejected audit is written by the orchestrator.
    # Assert the after_json contains the enriched fields (approval_id,
    # runbook_id, gate='runbook_changed', pinned + current hashes). This test
    # produces exactly one autofix.rejected row (the drift rejection), so no
    # LIKE filter is needed.
    rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.rejected'"),
        {},
    )
    assert len(rows) == 1
    after_json_raw = rows[0][0]
    assert isinstance(after_json_raw, str)
    after = json.loads(after_json_raw)
    assert after["approval_id"] == approval_id
    assert after["runbook_id"] == rb.id
    assert after["gate"] == "runbook_changed"
    assert after["pinned_runbook_hash"] == "hash-v1"
    assert after["current_runbook_hash"] == "hash-v2"


@pytest.mark.asyncio
async def test_pending_approval_from_v1_hash_gets_409_after_refresh(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primary regression: a v1-hash pinned approval 409s once the runbook is
    rehashed to v2 on refresh — this IS the intended audit trail per Design.
    """
    # 1. Write a valid runbook folder
    rb_folder = tmp_path / "runbook"
    _write_runbook(rb_folder, config=_valid_config_dict("test-runbook"))
    monkeypatch.setenv("HOMELAB_MONITOR_RUNBOOKS_DIR", str(tmp_path))

    # Create the runbook record with v2 hash
    rb = _make_runbook_record(
        alertname="TestAlert",
        dry_run_required=True,
        runbook_dir=rb_folder,
    )
    await _insert_runbook(repo, rb)

    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "true")

    # 2. Simulate pre-stage v1 row by directly UPDATEing to bare hex
    async with repo.transaction() as conn:
        await conn.execute(
            text("UPDATE runbooks SET content_hash = :hash WHERE id = :id"),
            {"id": rb.id, "hash": "a" * 64},
        )

    # 3. Create a pending approval with the v1 hash
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
        secrets_repo,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    alert = _make_alert(alertname="TestAlert")
    await _insert_alert(repo, alert)

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        dry_result = await orch.handle_alert(alert)

    assert dry_result is not None
    approval_id = dry_result.approval_id
    assert approval_id is not None

    # Verify approval was created with v1 hash
    approvals_repo = RunbookRunApprovalsRepository(repo)
    approval = await approvals_repo.get(approval_id)
    assert approval is not None
    assert approval.pinned_runbook_hash == "a" * 64

    # 4. Manually POST /api/runbooks/refresh to trigger v2 hash computation
    resp = await authenticated_client.post(
        "/api/runbooks/refresh", json={}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004

    # Verify the runbook now has v2 hash
    resp = await authenticated_client.get("/api/runbooks")
    runbooks = resp.json()["items"]
    assert len(runbooks) == 1
    current_hash = runbooks[0]["content_hash"]
    assert current_hash.startswith("v2:sha256:")
    assert current_hash != "a" * 64

    # 5. POST /approve with the v1-pinned approval
    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={"confirm_phrase": "approve"},
        headers=_csrf(authenticated_client),
    )

    # 6. Assert 409 with runbook_changed_since_plan
    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["error"]["code"] == "runbook_changed_since_plan"

    # Verify approval is now rejected
    approval = await approvals_repo.get(approval_id)
    assert approval is not None
    assert approval.status == "rejected"


@pytest.mark.asyncio
async def test_router_approve_runbook_missing_returns_409_with_specific_code(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """Fix M2: POST /approve when the runbook was DELETED after the plan →
    409 with code='runbook_missing' (distinct from 'runbook_changed_since_plan')
    so the UI/operator can distinguish "runbook was deleted, re-author it" from
    "runbook was edited, re-plan against the new content".
    """
    rb = _make_runbook_record(
        alertname="Test",
        dry_run_required=True,
        content_hash="hash-v1",
        runbook_dir=tmp_path / "runbook",
    )
    await _insert_runbook(repo, rb)

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
        secrets_repo,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    alert = _make_alert(alertname="Test")
    await _insert_alert(repo, alert)

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        dry_result = await orch.handle_alert(alert)

    assert dry_result is not None
    approval_id = dry_result.approval_id
    assert approval_id is not None

    # Simulate the runbook being DELETED between the dry-run plan and the
    # approve call. We can't actually DELETE the row (FK from
    # runbook_runs.runbook_id blocks it), so patch RunbookRepo.get_runbook to
    # return None — precisely what execute_approved's drift check sees when the
    # row is gone. Scope the patch around the POST call ONLY, so the
    # handle_alert dry-run setup above still finds the real runbook row.
    with patch.object(RunbookRepo, "get_runbook", new=AsyncMock(return_value=None)):
        response = await authenticated_client.post(
            f"/api/autofix/approvals/{approval_id}/approve",
            json={"confirm_phrase": "approve"},
            headers=_csrf(authenticated_client),
        )

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["error"]["code"] == "runbook_missing"

    # Verify approval auto-rejected + audit gate is runbook_missing (not runbook_changed).
    approvals_repo = RunbookRunApprovalsRepository(repo)
    approval = await approvals_repo.get(approval_id)
    assert approval is not None
    assert approval.status == "rejected"

    rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.rejected'"),
        {},
    )
    assert len(rows) == 1
    after_json_raw = rows[0][0]
    assert isinstance(after_json_raw, str)
    after = json.loads(after_json_raw)
    assert after["gate"] == "runbook_missing"
    assert after["runbook_deleted"] is True


@pytest.mark.asyncio
async def test_router_approve_gate_kill_switch_denies_returns_409_kill_switch(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Post-I3: when execute_approved returns an operational-gate denial that is
    NOT one of the three specifically-handled reasons (RUNBOOK_CHANGED /
    RUNBOOK_MISSING / APPROVAL_NOT_PENDING), the router surfaces it as a 409
    conflict with the denial_reason.value used as the ``code`` — exercising the
    else-fallthrough branch in the reason-to-HTTP mapping.

    Trigger: real runbook + matching pinned hash (drift check passes), but
    autofix_enabled is falsy → _check_operational_gates returns KILL_SWITCH → the
    router else-branch maps that to code='kill_switch'.
    """
    # Seed a real runbook so the drift check finds it and passes; the pinned
    # hash matches so RUNBOOK_CHANGED does not trigger.
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    # Wire an orchestrator (else 503 masks the denial-branch we're testing).
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    # Explicitly disable auto-fix so the kill-switch gate trips.
    app_settings = AppSettingsRepository(repo)
    await app_settings.set("autofix_enabled", "false")

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={"confirm_phrase": "approve"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["error"]["code"] == "kill_switch"


@pytest.mark.asyncio
async def test_router_approve_returns_409_when_execute_approved_denies_approval_not_pending(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Coverage for autofix.py line 241: the ``if reason ==
    DenialReason.APPROVAL_NOT_PENDING`` branch inside the ``approve`` endpoint,
    reachable ONLY when ``orchestrator.execute_approved`` returns a RunResult
    with ``denial_reason=APPROVAL_NOT_PENDING`` (the concurrent-approve race —
    the pre-check at line 209 saw ``status='pending'`` but the approval was
    mutated to a decided state before ``execute_approved`` re-loaded it).

    Simulated by patching ``approvals.get`` so the FIRST call (router pre-check)
    returns the pending record unchanged, then mutating the row to 'approved'
    before returning. The SECOND call (from inside ``execute_approved``) then
    sees ``status='approved'`` and the orchestrator returns
    APPROVAL_NOT_PENDING, which the router maps to 409 with
    code='approval_not_pending' at line 241.
    """
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    # Wire an orchestrator (else 503 masks the denial-branch we're testing).
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    # Simulate the race: on the router's pre-check read (call 1), return the
    # pending record verbatim, then mutate the row to 'approved' so the
    # orchestrator's subsequent .get() (call 2) sees status != 'pending' and
    # returns a DENIED/APPROVAL_NOT_PENDING RunResult.
    original_get = RunbookRunApprovalsRepository.get
    call_count = 0

    async def _pending_get_then_decide(self: RunbookRunApprovalsRepository, aid: str) -> object:
        nonlocal call_count
        call_count += 1
        record = await original_get(self, aid)
        if call_count == 1 and record is not None:
            async with repo.transaction() as conn:
                await conn.execute(
                    text(
                        "UPDATE runbook_run_approvals SET status = 'approved', "
                        "approved_by = 'other-admin', decided_at = :dt WHERE id = :id"
                    ),
                    {"id": aid, "dt": utc_now_iso()},
                )
        return record

    with patch.object(RunbookRunApprovalsRepository, "get", new=_pending_get_then_decide):
        response = await authenticated_client.post(
            f"/api/autofix/approvals/{approval_id}/approve",
            json={"confirm_phrase": "approve"},
            headers=_csrf(authenticated_client),
        )

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["error"]["code"] == "approval_not_pending"
    assert response.json()["error"]["message"] == "approval is not pending"


@pytest.mark.asyncio
async def test_approve_extra_field_forbidden(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """R11: POST /approve with extra field → 422 (ConfigDict extra=forbid)."""
    # Seed runbook + alert + runbook_runs so the approval FK is satisfied.
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={"confirm_phrase": "approve", "extra_field": "x"},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_approve_csrf_missing(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """R12: POST /approve without X-CSRF-Token → 403."""
    # Seed runbook + alert + runbook_runs so the approval FK is satisfied.
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={"confirm_phrase": "approve"},
    )

    assert response.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_reject_happy(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """R13: POST /reject happy path → approval rejected."""
    # Seed runbook + alert + runbook_runs so the approval FK is satisfied.
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/reject",
        json={},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 200  # noqa: PLR2004
    data = response.json()
    assert data["approval_id"] == approval_id
    assert data["status"] == "rejected"

    # Verify audit
    audits = await repo.fetch_all(
        text("SELECT what FROM audit_log WHERE what = 'autofix.rejected'"),
        {},
    )
    assert len(audits) >= 1


@pytest.mark.asyncio
async def test_reject_not_found(
    authenticated_client: AsyncClient,
) -> None:
    """R14: POST /reject unknown approval → 404."""
    response = await authenticated_client.post(
        "/api/autofix/approvals/nonexistent/reject",
        json={},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_reject_not_pending(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """R15: POST /reject status != pending → 409."""
    # Seed runbook + alert + runbook_runs so the approval FK is satisfied.
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )
        await approvals_repo.mark_rejected_conn(
            conn,
            approval_id=approval_id,
            approved_by="admin",
            when=utc_now_iso(),
        )

    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/reject",
        json={},
        headers=_csrf(authenticated_client),
    )

    assert response.status_code == 409  # noqa: PLR2004


@pytest.mark.asyncio
async def test_router_reject_already_decided_returns_409(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """I1 (router): POST /reject on an approval that was decided concurrently
    (between the pre-check read and the UPDATE) returns 409 with
    code='approval_not_pending'.

    Simulated here by pre-mutating the approval to 'approved' between the
    router's pre-check read and its ``mark_rejected`` call — the ``AND
    status='pending'`` SQL guard makes ``mark_rejected_conn`` return rowcount=0,
    which the router surfaces as 409.
    """
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    # Simulate the race: patch approvals.get to return the pending record
    # (so the pre-check thinks it's still pending), but the actual UPDATE will
    # find the row already decided and return rowcount=0.
    original_get = RunbookRunApprovalsRepository.get
    call_count = 0

    async def _pending_get_then_decide(self: RunbookRunApprovalsRepository, aid: str) -> object:
        """Return pending on the pre-check read, then mutate the row to
        'approved' so the subsequent UPDATE guard fails."""
        nonlocal call_count
        call_count += 1
        record = await original_get(self, aid)
        if call_count == 1 and record is not None:
            # After the router's read but before its UPDATE, someone else
            # decides the approval.
            async with repo.transaction() as conn:
                await conn.execute(
                    text(
                        "UPDATE runbook_run_approvals SET status = 'approved', "
                        "approved_by = 'other-admin', decided_at = :dt WHERE id = :id"
                    ),
                    {"id": aid, "dt": utc_now_iso()},
                )
        return record

    with patch.object(RunbookRunApprovalsRepository, "get", new=_pending_get_then_decide):
        response = await authenticated_client.post(
            f"/api/autofix/approvals/{approval_id}/reject",
            json={},
            headers=_csrf(authenticated_client),
        )

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["error"]["code"] == "approval_not_pending"


@pytest.mark.asyncio
async def test_get_plan_orchestrator_unavailable(
    authenticated_client: AsyncClient,
) -> None:
    """R16: GET /plan without orchestrator on app.state → 503."""
    # Ensure no orchestrator
    if hasattr(authenticated_client.app.state, "autofix_orchestrator"):  # type: ignore[attr-defined]
        delattr(authenticated_client.app.state, "autofix_orchestrator")  # type: ignore[attr-defined]

    response = await authenticated_client.get("/api/autofix/approvals/test-id/plan")
    assert response.status_code == 503  # noqa: PLR2004
    assert response.json()["error"]["code"] == "autofix_unavailable"


@pytest.mark.asyncio
async def test_list_approvals_invalid_status_filter_rejected(
    authenticated_client: AsyncClient,
) -> None:
    """I4: GET /approvals?status_filter=<bogus> → 422 (Literal validation).

    The router's status_filter is a Literal['pending','approved','rejected'];
    FastAPI rejects any other value at the query-param layer before the handler
    is entered.
    """
    response = await authenticated_client.get(
        "/api/autofix/approvals", params={"status_filter": "bogus"}
    )
    assert response.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_approve_confirm_phrase_case_sensitive_rejects_uppercase(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """N1: POST /approve confirm_phrase is now compared byte-for-byte to
    ``'approve'`` — variants like ``'APPROVE'`` or ``' approve '`` are rejected
    with 400. The prior implementation strip+lower-cased first and let those
    variants through.
    """
    # Wire an orchestrator so the endpoint isn't 503 (autofix_unavailable) —
    # test_get_plan_orchestrator_unavailable earlier in the file delattr's it
    # from app.state, and _per_test_db does not restore it.
    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    # Seed runbook + alert + runbook_runs so the approval FK is satisfied.
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    # Uppercase.
    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={"confirm_phrase": "APPROVE"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004

    # Surrounding whitespace.
    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={"confirm_phrase": " approve "},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004


# ---------------------------------------------------------------------------
# STAGE-009-010B: confirm_pin as an alternative to confirm_phrase
# ---------------------------------------------------------------------------


async def _seed_pin(repo: SqliteRepository, pin: str = "1234") -> None:
    """Seed a PIN hash directly via app_settings (bypassing the set endpoint)."""
    app_settings = AppSettingsRepository(repo)
    await app_settings.set(PIN_HASH_KEY, hash_pin(pin, cost=4))


@pytest.mark.asyncio
async def test_approve_with_confirm_pin_success(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """A PIN configured + correct confirm_pin succeeds; orchestrator invoked
    with credential_type='pin' (asserted via the autofix.approved audit row's
    after_json.credential_type, since the router passes credential_type
    straight through to execute_approved)."""
    await _seed_pin(repo, pin="1234")

    rb = _make_runbook_record(
        alertname="Test",
        dry_run_required=True,
        content_hash="hash-v1",
        runbook_dir=tmp_path / "runbook",
    )
    await _insert_runbook(repo, rb)

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
        secrets_repo,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    alert = _make_alert(alertname="Test")
    await _insert_alert(repo, alert)

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        dry_result = await orch.handle_alert(alert)
    assert dry_result is not None
    approval_id = dry_result.approval_id
    assert approval_id is not None

    docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
    docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        response = await authenticated_client.post(
            f"/api/autofix/approvals/{approval_id}/approve",
            json={"confirm_pin": "1234"},
            headers=_csrf(authenticated_client),
        )
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["ran"] is True

    rows = await repo.fetch_all(
        text("SELECT after_json FROM audit_log WHERE what = 'autofix.approved'"),
        {},
    )
    assert len(rows) == 1
    after = json.loads(rows[0][0])
    assert after["credential_type"] == "pin"


@pytest.mark.asyncio
async def test_approve_wrong_pin_400_ticks_limiter(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Wrong confirm_pin -> 400; the PIN rate limiter is ticked (verified by
    repeating until lockout returns 429 within a few attempts)."""
    await _seed_pin(repo, pin="1234")
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    docker = _FakeDockerClient()
    orch = _make_orchestrator(repo, secrets_repo, docker)
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    response: Response | None = None
    for _ in range(3):
        response = await authenticated_client.post(
            f"/api/autofix/approvals/{approval_id}/approve",
            json={"confirm_pin": "9999"},
            headers=_csrf(authenticated_client),
        )
        assert response.status_code == 400  # noqa: PLR2004
    # 4th attempt: check() sees 3 fails → 5s lock → 429
    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={"confirm_pin": "9999"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 429  # noqa: PLR2004


@pytest.mark.asyncio
async def test_approve_no_pin_configured_falls_back_to_phrase(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """No PIN row present; confirm_phrase still succeeds."""
    rb = _make_runbook_record(
        alertname="Test",
        dry_run_required=True,
        content_hash="hash-v1",
        runbook_dir=tmp_path / "runbook",
    )
    await _insert_runbook(repo, rb)

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
        secrets_repo,
        docker,
        transcript_dir=transcript_dir,
        exec_log_dir=exec_log_dir,
    )
    authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

    alert = _make_alert(alertname="Test")
    await _insert_alert(repo, alert)

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        dry_result = await orch.handle_alert(alert)
    assert dry_result is not None
    approval_id = dry_result.approval_id
    assert approval_id is not None

    docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
    docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"

    with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
        response = await authenticated_client.post(
            f"/api/autofix/approvals/{approval_id}/approve",
            json={"confirm_phrase": "approve"},
            headers=_csrf(authenticated_client),
        )
    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["ran"] is True


@pytest.mark.asyncio
async def test_approve_both_credentials_400(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Both confirm_pin and confirm_phrase supplied simultaneously -> 400."""
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={"confirm_phrase": "approve", "confirm_pin": "1234"},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_approve_no_credentials_400(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Neither confirm_pin nor confirm_phrase supplied -> 400."""
    rb_id, _alert_id, dry_run_id = await _seed_dry_run_chain(repo)

    approvals_repo = RunbookRunApprovalsRepository(repo)
    async with repo.transaction() as conn:
        approval_id = await approvals_repo.insert_pending(
            conn,
            dry_run_id=dry_run_id,
            runbook_id=rb_id,
            alert_id=None,
            pinned_runbook_hash="hash-v1",
        )

    response = await authenticated_client.post(
        f"/api/autofix/approvals/{approval_id}/approve",
        json={},
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_approve_audit_includes_credential_type(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
) -> None:
    """autofix.approved audit after_json.credential_type reflects the phrase
    path ('phrase') and the pin path ('pin') respectively.

    credential_type is now included in BOTH approval and trigger audit paths:
    - autofix.approved (approve path): credential_type ('phrase' or 'pin')
    - autofix.ran (trigger path): credential_type ('phrase' or 'pin')
    - autofix.trigger_rejected (risky trigger rejection): credential_type
    """

    async def _run_one(credential_kwargs: dict[str, str], content_hash: str) -> str:
        alertname = f"Test-{content_hash}"
        rb = _make_runbook_record(
            alertname=alertname,
            dry_run_required=True,
            content_hash=content_hash,
            runbook_dir=tmp_path / f"runbook-{content_hash}",
        )
        await _insert_runbook(repo, rb)
        app_settings = AppSettingsRepository(repo)
        await app_settings.set("autofix_enabled", "true")

        transcript_dir = str(tmp_path / f"transcripts-{content_hash}")
        os.makedirs(transcript_dir, exist_ok=True)
        exec_log_dir = str(tmp_path / f"exec-logs-{content_hash}")
        os.makedirs(exec_log_dir, exist_ok=True)

        docker = _FakeDockerClient(
            result=ExecResult(exit_code=0, stdout="plan", stderr=""),
            transcript_to_write=f"{transcript_dir}/dry-{uuid7()}.transcript",
        )
        orch = _make_orchestrator(
            repo,
            secrets_repo,
            docker,
            transcript_dir=transcript_dir,
            exec_log_dir=exec_log_dir,
        )
        authenticated_client.app.state.autofix_orchestrator = orch  # type: ignore[attr-defined]

        alert = _make_alert(alertname=alertname)
        await _insert_alert(repo, alert)

        with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
            dry_result = await orch.handle_alert(alert)
        assert dry_result is not None
        approval_id = dry_result.approval_id
        assert approval_id is not None

        docker.result = ExecResult(exit_code=0, stdout="fixed", stderr="")
        docker.transcript_to_write = f"{transcript_dir}/real-{uuid7()}.transcript"

        with patch.object(RunbookRunsRepository, "count_inflight", new=AsyncMock(return_value=0)):
            response = await authenticated_client.post(
                f"/api/autofix/approvals/{approval_id}/approve",
                json=credential_kwargs,
                headers=_csrf(authenticated_client),
            )
        assert response.status_code == 200  # noqa: PLR2004
        return approval_id

    # Phrase path.
    approval_phrase = await _run_one({"confirm_phrase": "approve"}, "hash-phrase")
    rows_phrase = await repo.fetch_all(
        text(
            "SELECT after_json FROM audit_log WHERE what = 'autofix.approved' "
            "AND after_json LIKE :like"
        ),
        {"like": f'%"approval_id": "{approval_phrase}"%'},
    )
    assert len(rows_phrase) == 1
    assert json.loads(rows_phrase[0][0])["credential_type"] == "phrase"

    # PIN path.
    await _seed_pin(repo, pin="4321")
    approval_pin = await _run_one({"confirm_pin": "4321"}, "hash-pin")
    rows_pin = await repo.fetch_all(
        text(
            "SELECT after_json FROM audit_log WHERE what = 'autofix.approved' "
            "AND after_json LIKE :like"
        ),
        {"like": f'%"approval_id": "{approval_pin}"%'},
    )
    assert len(rows_pin) == 1
    assert json.loads(rows_pin[0][0])["credential_type"] == "pin"
