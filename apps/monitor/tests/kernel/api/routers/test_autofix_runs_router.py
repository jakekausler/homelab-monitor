"""Tests for autofix_runs.py router (STAGE-009-011)."""

from __future__ import annotations

from tempfile import TemporaryDirectory

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from starlette.status import (
    HTTP_200_OK,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_422_UNPROCESSABLE_ENTITY,
    HTTP_500_INTERNAL_SERVER_ERROR,
)

import homelab_monitor.kernel.config as config_module
from homelab_monitor.kernel.autofix.types import FeedbackKind, RunMode
from homelab_monitor.kernel.config import FixerRunnerConfig
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from tests.kernel.autofix.conftest import insert_feedback, insert_run

# Default API pagination limit
DEFAULT_LIMIT = 100
# Duration of one hour in milliseconds
ONE_HOUR_MS = 3600000


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


@pytest.mark.asyncio
async def test_list_runs_requires_auth_401(
    unauthenticated_client: AsyncClient,
) -> None:
    response = await unauthenticated_client.get("/api/autofix/runs")
    assert response.status_code == HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_list_runs_empty_returns_empty_items_total_zero(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    response = await authenticated_client.get("/api/autofix/runs")
    assert response.status_code == HTTP_200_OK
    data = response.json()
    assert data["items"] == []
    assert data["total_count"] == 0
    assert data["limit"] == DEFAULT_LIMIT
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_list_runs_default_limit_100_offset_0(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
    )

    response = await authenticated_client.get("/api/autofix/runs")
    assert response.status_code == HTTP_200_OK
    data = response.json()
    assert data["limit"] == DEFAULT_LIMIT
    assert data["offset"] == 0
    assert len(data["items"]) == 1


@pytest.mark.asyncio
async def test_list_runs_limit_ceiling_500(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    response = await authenticated_client.get("/api/autofix/runs?limit=1000")
    assert response.status_code == HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_list_runs_limit_zero_rejected(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    response = await authenticated_client.get("/api/autofix/runs?limit=0")
    assert response.status_code == HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_list_runs_negative_offset_rejected(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    response = await authenticated_client.get("/api/autofix/runs?offset=-1")
    assert response.status_code == HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_list_runs_response_shape(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at="2024-01-01T00:00:00",
        ended_at="2024-01-01T01:00:00",
        exit_code=0,
    )

    response = await authenticated_client.get("/api/autofix/runs")
    assert response.status_code == HTTP_200_OK
    data = response.json()
    item = data["items"][0]
    assert item["id"] == run_id
    assert item["outcome"] == "success"
    assert item["duration_ms"] == ONE_HOUR_MS
    assert item["alert_id"] is None


@pytest.mark.asyncio
async def test_get_run_by_id_returns_detail_with_prompt(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        prompt="Test prompt",
    )

    response = await authenticated_client.get(f"/api/autofix/runs/{run_id}")
    assert response.status_code == HTTP_200_OK
    data = response.json()
    assert data["id"] == run_id
    assert data["prompt"] == "Test prompt"
    assert "created_at" in data


@pytest.mark.asyncio
async def test_get_run_by_id_404_when_missing(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    response = await authenticated_client.get("/api/autofix/runs/nonexistent-id")
    assert response.status_code == HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_get_run_feedback_returns_list(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
    )
    feedback_id = await insert_feedback(
        repo,
        runbook_run_id=run_id,
        kind=FeedbackKind.MISSING_CAPABILITY,
        suggestion_text="Add docker support",
    )

    response = await authenticated_client.get(f"/api/autofix/runs/{run_id}/feedback")
    assert response.status_code == HTTP_200_OK
    data = response.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == feedback_id
    assert data["items"][0]["kind"] == "missing_capability"


@pytest.mark.asyncio
async def test_get_run_feedback_empty_list_when_no_feedback(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
    )

    response = await authenticated_client.get(f"/api/autofix/runs/{run_id}/feedback")
    assert response.status_code == HTTP_200_OK
    data = response.json()
    assert data["items"] == []


@pytest.mark.asyncio
async def test_get_run_feedback_404_when_run_missing(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    response = await authenticated_client.get("/api/autofix/runs/nonexistent/feedback")
    assert response.status_code == HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_get_transcript_returns_full_text_when_small(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    with TemporaryDirectory() as tmpdir:
        transcript_path = f"{tmpdir}/test.txt"
        with open(transcript_path, "w") as f:
            f.write("Test transcript")

        run_id = await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.REAL,
            initiated_by="operator",
            transcript_path=transcript_path,
        )

        # Patch the fixer config to use our temp dir
        original_load = config_module.load_fixer_runner_config

        def patched_load() -> FixerRunnerConfig:
            cfg = original_load()
            return cfg.__class__(
                container=cfg.container,
                transcript_dir=tmpdir,
                exec_log_dir=cfg.exec_log_dir,
                fixer_user=cfg.fixer_user,
                exec_timeout_seconds=cfg.exec_timeout_seconds,
            )

        config_module.load_fixer_runner_config = patched_load

        try:
            response = await authenticated_client.get(f"/api/autofix/runs/{run_id}/transcript")
            assert response.status_code == HTTP_200_OK
            data = response.json()
            assert data["text"] == "Test transcript"
            assert data["truncated"] is False
        finally:
            config_module.load_fixer_runner_config = original_load


@pytest.mark.asyncio
async def test_get_transcript_404_when_run_missing(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    response = await authenticated_client.get("/api/autofix/runs/nonexistent/transcript")
    assert response.status_code == HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_get_transcript_404_when_run_has_no_transcript_path(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        transcript_path=None,
    )

    response = await authenticated_client.get(f"/api/autofix/runs/{run_id}/transcript")
    assert response.status_code == HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_get_run_by_id_outcome_killed_when_killed_at_set(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        killed_at=utc_now_iso(),
        ended_at=utc_now_iso(),
        exit_code=None,
    )

    response = await authenticated_client.get(f"/api/autofix/runs/{run_id}")
    assert response.status_code == HTTP_200_OK
    assert response.json()["outcome"] == "killed"


@pytest.mark.asyncio
async def test_get_run_by_id_outcome_dry_run_when_mode_dry_run(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="operator",
        ended_at=utc_now_iso(),
        killed_at=None,
        exit_code=0,
    )

    response = await authenticated_client.get(f"/api/autofix/runs/{run_id}")
    assert response.status_code == HTTP_200_OK
    assert response.json()["outcome"] == "dry_run"


@pytest.mark.asyncio
async def test_get_run_by_id_outcome_failure_fallback(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        ended_at=utc_now_iso(),
        killed_at=None,
        exit_code=1,
    )

    response = await authenticated_client.get(f"/api/autofix/runs/{run_id}")
    assert response.status_code == HTTP_200_OK
    assert response.json()["outcome"] == "failure"


@pytest.mark.asyncio
async def test_get_transcript_500_when_transcript_dir_not_configured(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    with TemporaryDirectory() as tmpdir:
        transcript_path = f"{tmpdir}/test.txt"
        with open(transcript_path, "w") as f:
            f.write("Test transcript")

        run_id = await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.REAL,
            initiated_by="operator",
            transcript_path=transcript_path,
        )

        original_load = config_module.load_fixer_runner_config

        def patched_load() -> FixerRunnerConfig:
            cfg = original_load()
            return cfg.__class__(
                container=cfg.container,
                transcript_dir="",
                exec_log_dir=cfg.exec_log_dir,
                fixer_user=cfg.fixer_user,
                exec_timeout_seconds=cfg.exec_timeout_seconds,
            )

        config_module.load_fixer_runner_config = patched_load

        try:
            response = await authenticated_client.get(f"/api/autofix/runs/{run_id}/transcript")
            assert response.status_code == HTTP_500_INTERNAL_SERVER_ERROR
            data = response.json()
            assert data["error"]["code"] == "transcript_dir_not_configured"
        finally:
            config_module.load_fixer_runner_config = original_load


@pytest.mark.asyncio
async def test_get_transcript_404_when_path_escapes_base_dir(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    seed_runbook: str,
) -> None:
    with TemporaryDirectory() as tmpdir:
        # Create a file outside the base dir
        evil_file = f"{tmpdir}/../evil.txt"
        run_id = await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.REAL,
            initiated_by="operator",
            transcript_path=evil_file,
        )

        original_load = config_module.load_fixer_runner_config

        def patched_load() -> FixerRunnerConfig:
            cfg = original_load()
            return cfg.__class__(
                container=cfg.container,
                transcript_dir=tmpdir,
                exec_log_dir=cfg.exec_log_dir,
                fixer_user=cfg.fixer_user,
                exec_timeout_seconds=cfg.exec_timeout_seconds,
            )

        config_module.load_fixer_runner_config = patched_load

        try:
            response = await authenticated_client.get(f"/api/autofix/runs/{run_id}/transcript")
            assert response.status_code == HTTP_404_NOT_FOUND
        finally:
            config_module.load_fixer_runner_config = original_load
