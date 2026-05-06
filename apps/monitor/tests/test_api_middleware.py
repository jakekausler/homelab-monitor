"""Tests for kernel/api/middleware.py — RequestId, AccessLog, and Auth."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.api.app import create_app


@pytest.mark.asyncio
async def test_request_id_generated_when_not_present() -> None:
    """RequestIdMiddleware generates uuid4 hex if no X-Request-Id header."""
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/version")
        # Response should have X-Request-Id header
        assert "X-Request-Id" in resp.headers
        request_id = resp.headers["X-Request-Id"]
        # Should be hex32
        assert len(request_id) == 32  # noqa: PLR2004
        assert all(c in "0123456789abcdef" for c in request_id)


@pytest.mark.asyncio
async def test_request_id_echoed_when_valid() -> None:
    """RequestIdMiddleware echoes valid hex32 X-Request-Id header."""
    app = create_app(lifespan_enabled=False)
    test_id = "a" * 32
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/version", headers={"X-Request-Id": test_id})
        # Should echo the same ID
        assert resp.headers["X-Request-Id"] == test_id


@pytest.mark.asyncio
async def test_request_id_replaced_when_invalid() -> None:
    """RequestIdMiddleware replaces invalid X-Request-Id (non-hex or wrong length)."""
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Invalid: wrong length
        resp = await client.get("/api/version", headers={"X-Request-Id": "short"})
        response_id = resp.headers["X-Request-Id"]
        assert response_id != "short"
        assert len(response_id) == 32  # noqa: PLR2004

        # Invalid: non-hex
        resp = await client.get("/api/version", headers={"X-Request-Id": "z" * 32})
        response_id = resp.headers["X-Request-Id"]
        assert response_id != "z" * 32


@pytest.mark.asyncio
async def test_auth_401_when_no_session() -> None:
    """AuthMiddleware returns 401 when no valid session cookie on protected endpoint."""
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/collectors")
        assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_auth_exempt_paths_bypass() -> None:
    """AuthMiddleware exempt paths bypass auth check."""
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # /api/healthz should bypass auth
        resp = await client.get("/api/healthz")
        # Will get 200 (degraded healthz) not 401 (auth)
        assert resp.status_code == 200  # noqa: PLR2004

        # /api/version should bypass auth
        resp = await client.get("/api/version")
        # Should succeed (200) since it doesn't need scheduler
        assert resp.status_code == 200  # noqa: PLR2004

        # /api/openapi.json should bypass auth
        resp = await client.get("/api/openapi.json")
        assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_auth_401_error_envelope() -> None:
    """AuthMiddleware 401 response uses error envelope."""
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/collectors")
        assert resp.status_code == 401  # noqa: PLR2004
        data = resp.json()
        assert "error" in data
        assert "code" in data["error"]
        assert "message" in data["error"]


@pytest.mark.asyncio
async def test_access_log_middleware_exception_handler() -> None:
    """AccessLogMiddleware exception handler logs and re-raises exceptions."""
    app = create_app(lifespan_enabled=False)

    # Add a route that raises an exception
    @app.get("/api/test_error")
    async def test_error_route() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("test exception")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="test exception"):
            await client.get("/api/test_error")


@pytest.mark.asyncio
async def test_auth_with_valid_session(authenticated_client: AsyncClient) -> None:
    """AuthMiddleware allows requests with valid session cookie."""
    # authenticated_client has a valid session, so exempt-path-free endpoints work
    # The SSE broker is available, so /api/collectors succeeds
    resp = await authenticated_client.get("/api/collectors")
    assert resp.status_code == 200  # noqa: PLR2004
