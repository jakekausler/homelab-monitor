"""Tests for GET /api/auth/me endpoint."""
# pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType, reportArgumentType]

from __future__ import annotations

import base64

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_me_with_session_returns_user(authenticated_client: AsyncClient) -> None:

    resp = await authenticated_client.get("/api/auth/me")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["user"]["username"] == "testuser"
    assert "id" in data["user"]


@pytest.mark.asyncio
async def test_me_without_cookie_returns_401(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without session cookie, /me returns 401."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    from httpx import ASGITransport  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):  # noqa: SIM117
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/auth/me")
            assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_me_with_tampered_session_cookie_returns_401(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With tampered session cookie, /me returns 401."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    from httpx import ASGITransport  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):  # noqa: SIM117
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Set a tampered cookie
            client.cookies.set("homelab_monitor_session", "tampered_value")
            resp = await client.get("/api/auth/me")
            assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_me_with_token_auth_returns_401(api_token_client: AsyncClient) -> None:
    """With Bearer token (not session), /me returns 401 (requires session)."""
    resp = await api_token_client.get("/api/auth/me")
    # /me requires session auth, not token auth
    assert resp.status_code == 401  # noqa: PLR2004
