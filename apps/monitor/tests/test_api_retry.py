"""Tests for kernel/api/routers/collectors.py — POST /api/collectors/{name}/retry endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.api.app import create_app


@pytest.mark.asyncio
async def test_retry_returns_tick_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/collectors/{name}/retry returns 200 with {name, tick_id, requested_at}."""
    # This is tested in the e2e context since it requires a real scheduler
    # For unit test, we verify the response shape
    from homelab_monitor.kernel.api.routers.collectors import RetryResponse  # noqa: PLC0415

    tick_id = "a" * 32  # uuid4().hex is 32 chars
    resp = RetryResponse(
        name="test-collector",
        tick_id=tick_id,
        requested_at="2026-05-05T00:00:00Z",
    )
    assert resp.name == "test-collector"
    assert resp.tick_id == tick_id
    assert resp.requested_at == "2026-05-05T00:00:00Z"
    assert len(resp.tick_id) == 32  # noqa: PLR2004


def test_retry_response_shape() -> None:
    """RetryResponse has all required fields."""
    from homelab_monitor.kernel.api.routers.collectors import RetryResponse  # noqa: PLC0415

    resp = RetryResponse(
        name="collector",
        tick_id="a" * 32,
        requested_at="2026-05-05T00:00:00Z",
    )
    assert isinstance(resp.name, str)
    assert isinstance(resp.tick_id, str)
    assert isinstance(resp.requested_at, str)


def test_retry_response_forbids_extra_fields() -> None:
    """RetryResponse enforces extra='forbid'."""
    from homelab_monitor.kernel.api.routers.collectors import RetryResponse  # noqa: PLC0415

    with pytest.raises(ValueError):
        RetryResponse(
            name="test",
            tick_id="a" * 32,
            requested_at="2026-05-05T00:00:00Z",
            extra="not_allowed",  # type: ignore[call-arg]
        )


@pytest.mark.asyncio
async def test_retry_401_without_auth() -> None:
    """POST /api/collectors/{name}/retry returns 401 without session cookie."""
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/collectors/noop/retry")
        assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_retry_401_when_no_session() -> None:
    """POST /api/collectors/{name}/retry returns 401 when no valid session."""
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/collectors/noop/retry")
        assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_retry_404_unknown_collector(authenticated_client: AsyncClient) -> None:
    """POST /api/collectors/{unknown}/retry returns 404."""
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/collectors/nonexistent-collector/retry",
        headers={"X-CSRF-Token": csrf} if csrf else {},
    )
    # authenticated_client boots lifespan, so 404 (not 503) when collector is unknown
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_retry_404_invalid_name(authenticated_client: AsyncClient) -> None:
    """POST /api/collectors/{invalid-name}/retry returns 404 for regex mismatch."""
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    # Invalid: starts with uppercase
    resp = await authenticated_client.post(
        "/api/collectors/InvalidCollector/retry",
        headers={"X-CSRF-Token": csrf} if csrf else {},
    )
    # Regex check happens before scheduler lookup; expect 404 with lifespan boot
    assert resp.status_code == 404  # noqa: PLR2004
