"""Tests for kernel/api/middleware.py — RequestId, AccessLog, and DevAuth."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.api.app import create_app


@pytest.mark.asyncio
async def test_request_id_generated_when_not_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RequestIdMiddleware generates uuid4 hex if no X-Request-Id header."""
    monkeypatch.delenv("HOMELAB_MONITOR_DEV_AUTH", raising=False)
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/version")
        # Response should have X-Request-Id header
        assert "X-Request-Id" in resp.headers
        request_id = resp.headers["X-Request-Id"]
        # Should be hex32
        assert len(request_id) == 32  # noqa: PLR2004  # noqa: PLR2004
        assert all(c in "0123456789abcdef" for c in request_id)


@pytest.mark.asyncio
async def test_request_id_echoed_when_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RequestIdMiddleware echoes valid hex32 X-Request-Id header."""
    monkeypatch.delenv("HOMELAB_MONITOR_DEV_AUTH", raising=False)
    app = create_app(lifespan_enabled=False)
    test_id = "a" * 32
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/version", headers={"X-Request-Id": test_id})
        # Should echo the same ID
        assert resp.headers["X-Request-Id"] == test_id


@pytest.mark.asyncio
async def test_request_id_replaced_when_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RequestIdMiddleware replaces invalid X-Request-Id (non-hex or wrong length)."""
    monkeypatch.delenv("HOMELAB_MONITOR_DEV_AUTH", raising=False)
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Invalid: wrong length
        resp = await client.get("/api/version", headers={"X-Request-Id": "short"})
        response_id = resp.headers["X-Request-Id"]
        assert response_id != "short"
        assert len(response_id) == 32  # noqa: PLR2004  # noqa: PLR2004

        # Invalid: non-hex
        resp = await client.get("/api/version", headers={"X-Request-Id": "z" * 32})
        response_id = resp.headers["X-Request-Id"]
        assert response_id != "z" * 32


@pytest.mark.asyncio
async def test_dev_auth_401_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DevAuthMiddleware returns 401 when HOMELAB_MONITOR_DEV_AUTH unset."""
    monkeypatch.delenv("HOMELAB_MONITOR_DEV_AUTH", raising=False)
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/collectors", headers={"X-Auth": "dev"})
        assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_dev_auth_401_when_header_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DevAuthMiddleware returns 401 when X-Auth header missing."""
    monkeypatch.setenv("HOMELAB_MONITOR_DEV_AUTH", "1")
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/collectors")
        assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_dev_auth_200_when_enabled_and_header_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DevAuthMiddleware passes when env set and header is 'X-Auth: dev'."""
    monkeypatch.setenv("HOMELAB_MONITOR_DEV_AUTH", "1")
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/collectors", headers={"X-Auth": "dev"})
        # Will get 503 (dependency unavailable) not 401 (auth failed)
        assert resp.status_code != 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_dev_auth_header_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DevAuthMiddleware treats X-Auth header value case-insensitively."""
    monkeypatch.setenv("HOMELAB_MONITOR_DEV_AUTH", "1")
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Try with uppercase
        resp = await client.get("/api/collectors", headers={"X-Auth": "DEV"})
        assert resp.status_code != 401  # noqa: PLR2004
        # Try with mixed case
        resp = await client.get("/api/collectors", headers={"X-Auth": "Dev"})
        assert resp.status_code != 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_dev_auth_exempt_paths_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DevAuthMiddleware exempt paths bypass auth check."""
    monkeypatch.delenv("HOMELAB_MONITOR_DEV_AUTH", raising=False)
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
async def test_dev_auth_401_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DevAuthMiddleware 401 response uses error envelope."""
    monkeypatch.delenv("HOMELAB_MONITOR_DEV_AUTH", raising=False)
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/collectors", headers={"X-Auth": "dev"})
        assert resp.status_code == 401  # noqa: PLR2004
        data = resp.json()
        assert "error" in data
        assert "code" in data["error"]
        assert "message" in data["error"]


@pytest.mark.asyncio
async def test_access_log_middleware_exception_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AccessLogMiddleware exception handler logs and re-raises exceptions."""
    monkeypatch.setenv("HOMELAB_MONITOR_DEV_AUTH", "1")
    app = create_app(lifespan_enabled=False)

    # Add a route that raises an exception
    @app.get("/api/test_error")
    async def test_error_route() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("test exception")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="test exception"):
            await client.get("/api/test_error", headers={"X-Auth": "dev"})
