"""Tests for /api/runbooks/stats endpoint (STAGE-009-011)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from starlette.status import HTTP_200_OK, HTTP_401_UNAUTHORIZED

from homelab_monitor.kernel.autofix.types import RunMode
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from tests.kernel.autofix.conftest import insert_run

# Magic constants for stats tests
SEED_RUN_COUNT = 3
SEED_SUCCESS_COUNT = 2
OTHER_RUN_COUNT = 2
ITEMS_EXPECTED = 2


@pytest_asyncio.fixture
async def seed_runbook(repo: SqliteRepository) -> str:
    """Create and return a test runbook ID."""
    runbook_id = uuid7()
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO runbooks "
                "(id, path, created_at, alert_match_patterns, risk_tag, "
                " dry_run_required, rate_limit_per_hour, cooldown_seconds, "
                " enabled, auto_trigger, content_hash) "
                "VALUES (:id, :path, :created_at, :alert_match_patterns, :risk_tag, "
                " :dry_run_required, :rate_limit_per_hour, :cooldown_seconds, "
                " :enabled, :auto_trigger, :content_hash)"
            ),
            {
                "id": runbook_id,
                "path": f"test/{runbook_id}.yaml",
                "created_at": utc_now_iso(),
                "alert_match_patterns": "[]",
                "risk_tag": "safe",
                "dry_run_required": False,
                "rate_limit_per_hour": None,
                "cooldown_seconds": None,
                "enabled": True,
                "auto_trigger": False,
                "content_hash": None,
            },
        )
    return runbook_id


@pytest_asyncio.fixture
async def another_runbook(repo: SqliteRepository) -> str:
    """Create and return a second test runbook ID."""
    runbook_id = uuid7()
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO runbooks "
                "(id, path, created_at, alert_match_patterns, risk_tag, "
                " dry_run_required, rate_limit_per_hour, cooldown_seconds, "
                " enabled, auto_trigger, content_hash) "
                "VALUES (:id, :path, :created_at, :alert_match_patterns, :risk_tag, "
                " :dry_run_required, :rate_limit_per_hour, :cooldown_seconds, "
                " :enabled, :auto_trigger, :content_hash)"
            ),
            {
                "id": runbook_id,
                "path": f"test/{runbook_id}.yaml",
                "created_at": utc_now_iso(),
                "alert_match_patterns": "[]",
                "risk_tag": "safe",
                "dry_run_required": False,
                "rate_limit_per_hour": None,
                "cooldown_seconds": None,
                "enabled": True,
                "auto_trigger": False,
                "content_hash": None,
            },
        )
    return runbook_id


@pytest.mark.asyncio
async def test_stats_requires_auth(
    unauthenticated_client: AsyncClient,
) -> None:
    response = await unauthenticated_client.get("/api/runbooks/stats")
    assert response.status_code == HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_stats_empty_when_no_runbooks_registered(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    response = await authenticated_client.get("/api/runbooks/stats")
    assert response.status_code == HTTP_200_OK
    data = response.json()
    assert data["items"] == []


@pytest.mark.asyncio
async def test_stats_runbook_with_zero_runs_appears_with_nulls(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    response = await authenticated_client.get("/api/runbooks/stats")
    assert response.status_code == HTTP_200_OK
    data = response.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["runbook_id"] == seed_runbook
    assert data["items"][0]["run_count_30d"] == 0
    assert data["items"][0]["last_run_at"] is None
    assert data["items"][0]["last_run_status"] is None
    assert data["items"][0]["success_rate_30d"] is None


@pytest.mark.asyncio
async def test_stats_response_shape(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    now = datetime.fromisoformat(utc_now_iso())
    within_window = (now - timedelta(days=15)).isoformat()

    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at=within_window,
        ended_at=within_window,
        exit_code=0,
    )

    response = await authenticated_client.get("/api/runbooks/stats")
    assert response.status_code == HTTP_200_OK
    data = response.json()
    item = data["items"][0]
    assert isinstance(item["runbook_id"], str)
    assert isinstance(item["run_count_30d"], int)
    assert item["success_rate_30d"] is None or isinstance(item["success_rate_30d"], float)
    assert item["last_run_at"] is None or isinstance(item["last_run_at"], str)
    assert item["last_run_status"] in (
        None,
        "success",
        "failure",
        "killed",
        "in_flight",
        "dry_run",
    )


@pytest.mark.asyncio
async def test_stats_integration_smoke(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
    another_runbook: str,
) -> None:
    now = datetime.fromisoformat(utc_now_iso())
    within_window = (now - timedelta(days=15)).isoformat()

    # Insert 2 successful runs on seed_runbook
    for _i in range(2):
        await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.REAL,
            initiated_by="operator",
            started_at=within_window,
            ended_at=within_window,
            exit_code=0,
        )

    # Insert 1 failed run on seed_runbook
    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at=within_window,
        ended_at=within_window,
        exit_code=1,
    )

    # another_runbook has no runs

    response = await authenticated_client.get("/api/runbooks/stats")
    assert response.status_code == HTTP_200_OK
    data = response.json()
    assert len(data["items"]) == ITEMS_EXPECTED

    by_id = {item["runbook_id"]: item for item in data["items"]}

    # seed_runbook: 3 runs, 2/3 success rate
    seed_stats = by_id[seed_runbook]
    assert seed_stats["run_count_30d"] == SEED_RUN_COUNT
    assert seed_stats["success_rate_30d"] == pytest.approx(SEED_SUCCESS_COUNT / SEED_RUN_COUNT)  # pyright: ignore[reportUnknownMemberType]
    assert seed_stats["last_run_status"] in ("success", "failure")

    # another_runbook: 0 runs
    other_stats = by_id[another_runbook]
    assert other_stats["run_count_30d"] == 0
    assert other_stats["success_rate_30d"] is None
    assert other_stats["last_run_status"] is None
