"""Tests for POST /api/auth/change-password endpoint."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_change_password_valid_returns_200(
    authenticated_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Valid current_password changes to new_password and returns 200."""
    # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType, reportArgumentType]  # test fixtures

    from homelab_monitor.kernel.db.repository import SqliteRepository  # noqa: PLC0415

    repo = SqliteRepository(engine=db_engine)

    resp = await authenticated_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "testpassword123",
            "new_password": "newpassword123",
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf")},  # type: ignore[reportArgumentType]
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["user"]["username"] == "testuser"

    # Verify new password works
    from homelab_monitor.kernel.auth.passwords import verify_password  # noqa: PLC0415

    result = await repo.fetch_one(
        text("SELECT bcrypt_hash FROM users WHERE username = :user"),
        {"user": "testuser"},
    )
    assert result is not None
    hash_val = result[0]
    assert verify_password("newpassword123", hash_val)


@pytest.mark.asyncio
async def test_change_password_wrong_current_returns_401(authenticated_client: AsyncClient) -> None:
    """Wrong current_password returns 401."""
    resp = await authenticated_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "wrongpassword",
            "new_password": "newpassword123",
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf")},  # type: ignore[reportArgumentType]
    )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_change_password_weak_new_returns_400(authenticated_client: AsyncClient) -> None:
    """New password < MIN_PASSWORD_LENGTH returns 400 weak_password."""
    resp = await authenticated_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "testpassword123",
            "new_password": "short",
        },
        headers={"X-CSRF-Token": authenticated_client.cookies.get("homelab_monitor_csrf")},  # type: ignore[reportArgumentType]
    )
    assert resp.status_code == 400  # noqa: PLR2004
    data = resp.json()
    assert "weak_password" in data.get("error", {}).get("code", "")


@pytest.mark.asyncio
async def test_change_password_rotates_all_sessions(
    authenticated_client: AsyncClient,
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After password change, old session is invalid."""
    import base64  # noqa: PLC0415

    csrf_token = authenticated_client.cookies.get("homelab_monitor_csrf")
    session_cookie = authenticated_client.cookies.get("homelab_monitor_session")

    # Change password
    resp = await authenticated_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "testpassword123",
            "new_password": "newpassword123",
        },
        headers={"X-CSRF-Token": csrf_token},  # type: ignore[reportArgumentType]
    )
    assert resp.status_code == 200  # noqa: PLR2004

    # Try to use old session cookie
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):  # noqa: SIM117
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            client.cookies.set("homelab_monitor_session", session_cookie)  # type: ignore[reportArgumentType]
            resp = await client.get("/api/auth/me")
            # Old session should be invalid
            assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_change_password_new_session_works(authenticated_client: AsyncClient) -> None:
    """After password change, new session issued works."""
    csrf_token = authenticated_client.cookies.get("homelab_monitor_csrf")

    # Change password
    resp = await authenticated_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "testpassword123",
            "new_password": "newpassword123",
        },
        headers={"X-CSRF-Token": csrf_token},  # type: ignore[reportArgumentType]
    )
    assert resp.status_code == 200  # noqa: PLR2004

    # New session should work
    resp = await authenticated_client.get("/api/auth/me")
    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_change_password_audit_row(
    authenticated_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Password change writes audit row."""
    from homelab_monitor.kernel.db.repository import SqliteRepository  # noqa: PLC0415

    repo = SqliteRepository(engine=db_engine)

    csrf_token = authenticated_client.cookies.get("homelab_monitor_csrf")

    # Change password
    resp = await authenticated_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "testpassword123",
            "new_password": "newpassword123",
        },
        headers={"X-CSRF-Token": csrf_token},  # type: ignore[reportArgumentType]
    )
    assert resp.status_code == 200  # noqa: PLR2004

    # Verify audit row
    row = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = :verb ORDER BY id DESC LIMIT 1"),
        {"verb": "user.password_change"},
    )
    assert row is not None


@pytest.mark.asyncio
async def test_change_password_without_session_returns_401(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without session, change-password returns 401."""
    import base64  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):  # noqa: SIM117
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/change-password",
                json={
                    "current_password": "testpassword123",
                    "new_password": "newpassword123",
                },
            )
            assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_change_password_missing_csrf_returns_403(
    authenticated_client: AsyncClient,
) -> None:
    """Missing X-CSRF-Token returns 403 csrf_mismatch."""
    resp = await authenticated_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "testpassword123",
            "new_password": "newpassword123",
        },
        # No X-CSRF-Token header
    )
    assert resp.status_code == 403  # noqa: PLR2004
    data = resp.json()
    assert data["error"]["code"] == "csrf_mismatch"
