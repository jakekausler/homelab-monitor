"""Tests for POST /api/auth/logout endpoint."""

from __future__ import annotations

import base64

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_logout_returns_204(authenticated_client: AsyncClient) -> None:
    """Logout after login returns 204."""
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf},  # type: ignore[reportArgumentType]
    )
    assert resp.status_code == 204  # noqa: PLR2004


@pytest.mark.asyncio
async def test_logout_deletes_session_row(
    authenticated_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """After logout, session row is deleted from DB."""
    from homelab_monitor.kernel.db.repository import SqliteRepository  # noqa: PLC0415

    repo = SqliteRepository(engine=db_engine)

    # Count sessions before logout
    result = await repo.fetch_all(text("SELECT COUNT(*) FROM sessions"))
    count_before = result[0][0] if result else 0
    assert count_before > 0

    # Logout
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf},  # type: ignore[reportArgumentType]
    )
    assert resp.status_code == 204  # noqa: PLR2004

    # Count sessions after logout
    result = await repo.fetch_all(text("SELECT COUNT(*) FROM sessions"))
    count_after = result[0][0] if result else 0
    assert count_after == 0


@pytest.mark.asyncio
async def test_logout_clears_cookies(authenticated_client: AsyncClient) -> None:
    """Logout clears auth cookies (Set-Cookie with empty value or expiry=0)."""
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf},  # type: ignore[reportArgumentType]
    )
    assert resp.status_code == 204  # noqa: PLR2004
    # Check that Set-Cookie headers are present
    set_cookie = resp.headers.get_list("set-cookie")
    # Should have at least one Set-Cookie header for clearing
    assert len(set_cookie) > 0


@pytest.mark.asyncio
async def test_logout_without_session_returns_401(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Logout without session returns 401 (require_session() rejects)."""
    from httpx import ASGITransport  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    app = create_app(lifespan_enabled=True)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        resp = await client.post("/api/auth/logout")
        assert resp.status_code == 401  # noqa: PLR2004
