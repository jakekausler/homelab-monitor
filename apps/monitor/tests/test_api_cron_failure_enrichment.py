"""Tests for GET /api/crons/{fingerprint}/runs/{run_id}/failure-enrichment (STAGE-004-034).

Project test conventions:
- asyncio_mode=auto — bare async def
- noqa: PLR2004 for magic number assertions
- Uses authenticated_client + repo fixtures (shared app, per-test DB)
- httpx_mock autouse fixture suppresses background HTTP noise
"""

from __future__ import annotations

import re
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (
    CronRunFailureEnrichmentsRepository,
)
from homelab_monitor.kernel.logs.models import LogLine

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404


@pytest.fixture(autouse=True)
def _suppress_background_calls(httpx_mock: HTTPXMock) -> None:  # pyright: ignore[reportUnusedFunction]
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://localhost/events.*"),
        content=b"",
        is_optional=True,
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://localhost/containers/json.*"),
        json=[],
        is_optional=True,
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victorialogs:9428/.*"),
        json={},
        is_optional=True,
        is_reusable=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_log_line(msg: str = "fail line") -> LogLine:
    return LogLine(
        timestamp="2026-06-07T00:00:00Z",
        message=msg,
        stream="s",
        severity="error",
        host=None,
        service=None,
        fields={},
    )


async def _seed_failure_enrichment(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    cron_fingerprint: str,
    run_id: str,
    failure_id: str | None = None,
    exit_code: int | None = 1,
    lines: list[LogLine] | None = None,
    degraded: bool = False,
) -> str:
    """Seed a failure enrichment row via the repo (real insert path)."""
    if failure_id is None:
        failure_id = uuid.uuid4().hex
    if lines is None:
        lines = [_make_log_line("fail output")]
    failure_repo = CronRunFailureEnrichmentsRepository(repo)
    await failure_repo.insert(
        failure_id=failure_id,
        cron_fingerprint=cron_fingerprint,
        run_id=run_id,
        exit_code=exit_code,
        started_at="2026-06-07T00:00:00+00:00",
        ended_at="2026-06-07T00:00:10+00:00",
        lines=lines,
        truncated=False,
        degraded=degraded,
        window_start="2026-06-06T23:59:00+00:00",
        window_end="2026-06-07T00:00:15+00:00",
    )
    return failure_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_failure_enrichment_200(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """200: failure enrichment exists — response includes failure_id, lines, degraded."""
    fp: str = "fp-api-fe-200"
    run_id: str = str(uuid.uuid4())
    failure_id: str = await _seed_failure_enrichment(
        repo,
        cron_fingerprint=fp,
        run_id=run_id,
        lines=[_make_log_line("line one"), _make_log_line("line two")],
    )

    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/{run_id}/failure-enrichment")
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert body["failure_id"] == failure_id
    assert body["cron_fingerprint"] == fp
    assert body["run_id"] == run_id
    assert body["degraded"] is False
    assert len(body["lines"]) == 2  # noqa: PLR2004
    assert body["lines"][0]["message"] == "line one"
    assert body["window_start"] is not None
    assert body["window_end"] is not None


async def test_get_failure_enrichment_404_missing(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """404 when no failure enrichment exists for the given (fingerprint, run_id)."""
    resp = await authenticated_client.get(
        "/api/crons/fp-does-not-exist/runs/run-does-not-exist/failure-enrichment"
    )
    assert resp.status_code == HTTP_NOT_FOUND


async def test_get_failure_enrichment_401_without_session(repo: SqliteRepository) -> None:
    """No session cookie → 401."""
    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        resp = await anon.get("/api/crons/fp-any/runs/run-any/failure-enrichment")
        assert resp.status_code == HTTP_UNAUTHORIZED


async def test_get_failure_enrichment_real_di_no_override(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Exercises get_cron_run_failure_repo DI WITHOUT overriding it.

    The test harness wires app.state.cron_run_failure_repo via _test_lifespan.
    This test hits the real DI function body so it appears in the coverage report.
    Seed a row and verify 200 comes back via the real DI path.
    """
    fp: str = "fp-real-di"
    run_id: str = str(uuid.uuid4())
    await _seed_failure_enrichment(
        repo,
        cron_fingerprint=fp,
        run_id=run_id,
        degraded=True,
    )

    # No dependency_overrides for get_cron_run_failure_repo — uses real app.state path
    resp = await authenticated_client.get(f"/api/crons/{fp}/runs/{run_id}/failure-enrichment")
    assert resp.status_code == HTTP_OK
    body = resp.json()
    assert body["cron_fingerprint"] == fp
    assert body["degraded"] is True
