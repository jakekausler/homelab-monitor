"""Tests for POST /api/logs/signatures/refresh and GET /api/logs/signatures/refresh/{cycle_id}.

STAGE-004-027 endpoints: manual drain-cycle trigger + polling.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.logs.cycle_status import CycleStatusStore
from homelab_monitor.kernel.logs.drain_consumer import (
    CycleInProgressError,
    DrainCycleResult,
)

_POST_URL = "/api/logs/signatures/refresh"
_GET_URL = "/api/logs/signatures/refresh/{cycle_id}"


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Return X-CSRF-Token header extracted from the session cookie."""
    csrf: str = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


def _good_result() -> DrainCycleResult:
    return DrainCycleResult(
        started_at=1000,
        finished_at=2000,
        lines_processed=3,
        new_templates=1,
        models_touched=1,
        cycle_status="ok",
        error=None,
    )


class _FakeConsumer:
    """Minimal fake DrainConsumer exposing the surface used by the endpoints."""

    def __init__(
        self,
        *,
        running: bool = False,
        result: DrainCycleResult | None = None,
        raises: BaseException | None = None,
        started_at: int | None = None,
    ) -> None:
        self._running = running
        self._result = result
        self._raises = raises
        self.cycle_started_at: int | None = started_at

    def is_cycle_running(self) -> bool:
        return self._running

    async def run_once(self) -> DrainCycleResult:
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


async def _poll_status(
    client: AsyncClient,
    cycle_id: str,
    *,
    max_iters: int = 20,
) -> dict[str, Any]:
    """Poll GET /{cycle_id} until status != 'running', bounded by max_iters."""
    data: dict[str, Any] = {}
    for _ in range(max_iters):
        await asyncio.sleep(0)
        resp = await client.get(_GET_URL.format(cycle_id=cycle_id))
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        if data["status"] != "running":
            return data
    return data


@pytest.mark.asyncio
async def test_post_503_when_consumer_absent(
    authenticated_client: AsyncClient,
) -> None:
    """POST 503 when app.state.drain_consumer is None (the per-test default)."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(_POST_URL, headers=csrf)
    assert resp.status_code == 503  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "drain_unavailable"


@pytest.mark.asyncio
async def test_post_409_when_running(
    authenticated_client: AsyncClient,
) -> None:
    """POST 409 when a cycle is already in progress."""
    app = cast(
        FastAPI,
        authenticated_client._transport.app,  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
    )
    app.state.drain_consumer = _FakeConsumer(running=True, started_at=123)  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]

    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(_POST_URL, headers=csrf)
    assert resp.status_code == 409  # noqa: PLR2004
    body = resp.json()
    assert body["error"]["code"] == "conflict"
    assert body["error"]["details"]["cycle_started_at"] == 123  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_202_and_records_done(
    authenticated_client: AsyncClient,
) -> None:
    """POST 202 + cycle_id; background task records 'done'."""
    app = cast(
        FastAPI,
        authenticated_client._transport.app,  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
    )
    result = _good_result()
    app.state.drain_consumer = _FakeConsumer(result=result)  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]

    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(_POST_URL, headers=csrf)
    assert resp.status_code == 202  # noqa: PLR2004
    cycle_id: str = resp.json()["cycle_id"]
    assert cycle_id

    status_data = await _poll_status(authenticated_client, cycle_id)
    assert status_data["status"] == "done"
    assert status_data["result"]["lines_processed"] == 3  # noqa: PLR2004
    assert status_data["result"]["cycle_status"] == "ok"
    assert status_data["error"] is None


@pytest.mark.asyncio
async def test_post_requires_csrf(
    authenticated_client: AsyncClient,
) -> None:
    """POST without CSRF header returns 403."""
    app = cast(
        FastAPI,
        authenticated_client._transport.app,  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
    )
    app.state.drain_consumer = _FakeConsumer(result=_good_result())  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
    resp = await authenticated_client.post(_POST_URL)
    assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_post_requires_session(
    authenticated_client: AsyncClient,
) -> None:
    """Anonymous POST returns 401."""
    app = cast(
        FastAPI,
        authenticated_client._transport.app,  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.post(_POST_URL)
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_200_running(
    authenticated_client: AsyncClient,
) -> None:
    """GET /{cycle_id} 200 with status='running' when seeded directly."""
    app = cast(
        FastAPI,
        authenticated_client._transport.app,  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
    )
    store: CycleStatusStore = app.state.cycle_status_store
    store.begin("abc")
    resp = await authenticated_client.get(_GET_URL.format(cycle_id="abc"))
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["status"] == "running"
    assert body["result"] is None
    assert body["error"] is None


@pytest.mark.asyncio
async def test_get_200_done(
    authenticated_client: AsyncClient,
) -> None:
    """GET /{cycle_id} 200 with status='done' + result fields."""
    app = cast(
        FastAPI,
        authenticated_client._transport.app,  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
    )
    store: CycleStatusStore = app.state.cycle_status_store
    result = _good_result()
    store.begin("abc")
    store.complete("abc", result)
    resp = await authenticated_client.get(_GET_URL.format(cycle_id="abc"))
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["status"] == "done"
    assert body["result"]["lines_processed"] == 3  # noqa: PLR2004
    assert body["result"]["cycle_status"] == "ok"
    assert body["error"] is None


@pytest.mark.asyncio
async def test_get_200_failed(
    authenticated_client: AsyncClient,
) -> None:
    """GET /{cycle_id} 200 with status='failed' + error string."""
    app = cast(
        FastAPI,
        authenticated_client._transport.app,  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
    )
    store: CycleStatusStore = app.state.cycle_status_store
    store.begin("abc")
    store.fail("abc", "boom")
    resp = await authenticated_client.get(_GET_URL.format(cycle_id="abc"))
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "boom"
    assert body["result"] is None


@pytest.mark.asyncio
async def test_get_404_unknown(
    authenticated_client: AsyncClient,
) -> None:
    """GET /does-not-exist returns 404."""
    resp = await authenticated_client.get(_GET_URL.format(cycle_id="does-not-exist"))
    assert resp.status_code == 404  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_background_task_records_failed_on_run_once_error(
    authenticated_client: AsyncClient,
) -> None:
    """Background task records status='failed' when run_once raises a generic error."""
    app = cast(
        FastAPI,
        authenticated_client._transport.app,  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
    )
    app.state.drain_consumer = _FakeConsumer(raises=RuntimeError("kaboom"))  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]

    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(_POST_URL, headers=csrf)
    assert resp.status_code == 202  # noqa: PLR2004
    cycle_id = resp.json()["cycle_id"]

    status_data = await _poll_status(authenticated_client, cycle_id)
    assert status_data["status"] == "failed"
    assert status_data["error"] == "kaboom"


@pytest.mark.asyncio
async def test_background_task_records_failed_on_cycle_in_progress(
    authenticated_client: AsyncClient,
) -> None:
    """Background task records status='failed' with 'cycle_in_progress' on CycleInProgressError."""
    app = cast(
        FastAPI,
        authenticated_client._transport.app,  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
    )
    app.state.drain_consumer = _FakeConsumer(  # type: ignore[reportPrivateUsage,reportAttributeAccessIssue,reportUnknownMemberType]
        raises=CycleInProgressError(started_at=None)
    )

    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(_POST_URL, headers=csrf)
    assert resp.status_code == 202  # noqa: PLR2004
    cycle_id = resp.json()["cycle_id"]

    status_data = await _poll_status(authenticated_client, cycle_id)
    assert status_data["status"] == "failed"
    assert status_data["error"] == "cycle_in_progress"
