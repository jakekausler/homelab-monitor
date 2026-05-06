"""End-to-end auth flow tests with real lifespan and DB."""

from __future__ import annotations

import base64

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_auth_e2e_login_to_logout(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E flow: login → me → logout → me (401)."""
    # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType, reportArgumentType]  # test fixtures

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")

    from httpx import ASGITransport  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.passwords import hash_password  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        # Create test user
        await app.state.auth_repo.create_user("testuser", hash_password("testpass1234", cost=4))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Login
            resp = await client.post(
                "/api/auth/login",
                json={"username": "testuser", "password": "testpass1234"},
            )
            assert resp.status_code == 200  # noqa: PLR2004
            login_data = resp.json()
            assert login_data["user"]["username"] == "testuser"

            # Call /me — should work
            resp = await client.get("/api/auth/me")
            assert resp.status_code == 200  # noqa: PLR2004
            me_data = resp.json()
            assert me_data["user"]["username"] == "testuser"

            # Call protected endpoint with CSRF
            csrf = client.cookies.get("homelab_monitor_csrf")
            resp = await client.post(
                "/api/auth/logout",
                headers={"X-CSRF-Token": csrf},  # type: ignore[reportArgumentType]
            )
            assert resp.status_code == 204  # noqa: PLR2004

            # Call /me after logout — should 401
            resp = await client.get("/api/auth/me")
            assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_version_users_configured_flag(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """users_configured flag flips to true after creating first user."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    from httpx import ASGITransport  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.passwords import hash_password  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):  # noqa: SIM117
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Before creating user, users_configured should be false
            resp = await client.get("/api/version")
            assert resp.status_code == 200  # noqa: PLR2004
            data = resp.json()
            assert data["users_configured"] is False

            # Create user
            await app.state.auth_repo.create_user("alice", hash_password("password1234", cost=4))

            # After creating user, users_configured should be true
            resp = await client.get("/api/version")
            assert resp.status_code == 200  # noqa: PLR2004
            data = resp.json()
            assert data["users_configured"] is True
