"""Tests for audit immutability (STAGE-009-012)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient

from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import RunMode
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from tests.kernel.autofix.conftest import insert_run


@pytest.mark.asyncio
async def test_no_delete_route_for_runbook_runs(_shared_app: FastAPI) -> None:
    """Assert no DELETE route for runbook_runs."""
    has_delete = False
    for route in _shared_app.routes:
        if (
            isinstance(route, APIRoute)
            and route.path.startswith("/api/autofix/runs/")
            and "DELETE" in route.methods
        ):
            has_delete = True
            break
    assert not has_delete


@pytest.mark.asyncio
async def test_no_delete_route_for_audit_log(_shared_app: FastAPI) -> None:
    """Assert no DELETE route for audit_log."""
    has_delete = False
    for route in _shared_app.routes:
        if (
            isinstance(route, APIRoute)
            and route.path.startswith("/api/audit-log/")
            and "DELETE" in route.methods
        ):
            has_delete = True
            break
    assert not has_delete


@pytest.mark.asyncio
async def test_delete_on_runs_returns_405_or_404(
    authenticated_client: AsyncClient,
) -> None:
    """DELETE /api/autofix/runs/<uuid> returns 404 or 405."""
    response = await authenticated_client.delete("/api/autofix/runs/test-uuid")
    assert response.status_code in {404, 405}


@pytest.mark.asyncio
async def test_delete_on_audit_log_returns_405_or_404(
    authenticated_client: AsyncClient,
) -> None:
    """DELETE /api/audit-log/<id> returns 404 or 405."""
    response = await authenticated_client.delete("/api/audit-log/test-id")
    assert response.status_code in {404, 405}


@pytest.mark.asyncio
async def test_mark_transcript_pruned_conn_only_updates_marker_columns(
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    """mark_transcript_pruned_conn updates only transcript_path and
    transcript_pruned_at; all other columns unchanged."""
    # Insert run with all columns populated
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="alert",
        prompt="test prompt",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:01:00Z",
        exit_code=0,
        killed_at=None,
        transcript_path="/data/runbook-transcripts/test.txt",
        fixer_user="test-user",
        host="test-host",
        runbook_hash="hash123",
    )

    # Get before state
    before_raw = await repo.fetch_one(
        __import__("sqlalchemy").text("SELECT * FROM runbook_runs WHERE id = :id"),
        {"id": run_id},
    )
    assert before_raw is not None
    before = before_raw._mapping  # pyright: ignore[reportPrivateUsage]

    # Mark as pruned
    runs_repo = RunbookRunsRepository(repo)
    async with repo.transaction() as conn:
        await runs_repo.mark_transcript_pruned_conn(
            conn,
            run_id=run_id,
            pruned_at="2026-01-02T00:00:00Z",
        )

    # Get after state
    after_raw = await repo.fetch_one(
        __import__("sqlalchemy").text("SELECT * FROM runbook_runs WHERE id = :id"),
        {"id": run_id},
    )
    assert after_raw is not None
    after = after_raw._mapping  # pyright: ignore[reportPrivateUsage]

    # Check only transcript_path and transcript_pruned_at changed
    assert before["transcript_path"] == "/data/runbook-transcripts/test.txt"
    assert before["transcript_pruned_at"] is None
    assert after["transcript_path"] is None
    assert after["transcript_pruned_at"] == "2026-01-02T00:00:00Z"

    # Check all other columns unchanged
    assert before["id"] == after["id"]
    assert before["runbook_id"] == after["runbook_id"]
    assert before["created_at"] == after["created_at"]
    assert before["alert_id"] == after["alert_id"]
    assert before["mode"] == after["mode"]
    assert before["prompt"] == after["prompt"]
    assert before["started_at"] == after["started_at"]
    assert before["ended_at"] == after["ended_at"]
    assert before["exit_code"] == after["exit_code"]
    assert before["fixer_user"] == after["fixer_user"]
    assert before["host"] == after["host"]
    assert before["runbook_hash"] == after["runbook_hash"]
    assert before["initiated_by"] == after["initiated_by"]
    assert before["killed_at"] == after["killed_at"]


@pytest.mark.asyncio
async def test_mark_transcript_pruned_conn_is_idempotent(
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    """Calling mark_transcript_pruned_conn twice is idempotent."""
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="alert",
        transcript_path="/data/runbook-transcripts/test.txt",
    )

    runs_repo = RunbookRunsRepository(repo)
    pruned_at_1 = utc_now_iso()
    async with repo.transaction() as conn:
        await runs_repo.mark_transcript_pruned_conn(
            conn,
            run_id=run_id,
            pruned_at=pruned_at_1,
        )

    pruned_at_2 = utc_now_iso()
    async with repo.transaction() as conn:
        await runs_repo.mark_transcript_pruned_conn(
            conn,
            run_id=run_id,
            pruned_at=pruned_at_2,
        )

    # Second call should have updated to pruned_at_2
    row_raw = await repo.fetch_one(
        __import__("sqlalchemy").text(
            "SELECT transcript_pruned_at FROM runbook_runs WHERE id = :id"
        ),
        {"id": run_id},
    )
    assert row_raw is not None
    row = row_raw._mapping  # pyright: ignore[reportPrivateUsage]
    assert row["transcript_pruned_at"] == pruned_at_2
