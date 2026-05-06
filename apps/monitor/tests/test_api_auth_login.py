"""Tests for POST /api/auth/login endpoint."""

from __future__ import annotations

import base64

import pytest
from httpx import AsyncClient

from homelab_monitor.kernel.api.app import create_app
from homelab_monitor.kernel.auth.passwords import hash_password


@pytest.mark.asyncio
async def test_login_valid_credentials_returns_200(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid credentials return 200 with user data and cookies set."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")

    from httpx import ASGITransport  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        await app.state.auth_repo.create_user("testuser", hash_password("testpass1234", cost=4))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/login",
                json={"username": "testuser", "password": "testpass1234"},
            )
            assert resp.status_code == 200  # noqa: PLR2004
            data = resp.json()
            assert data["user"]["username"] == "testuser"
            assert "id" in data["user"]
            # Verify cookies set
            assert "homelab_monitor_session" in client.cookies
            assert "homelab_monitor_csrf" in client.cookies


@pytest.mark.asyncio
async def test_login_wrong_password_returns_401(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong password returns 401 wrong_password error."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    from httpx import ASGITransport  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        await app.state.auth_repo.create_user("testuser", hash_password("testpass1234", cost=4))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/login",
                json={"username": "testuser", "password": "wrongpass"},
            )
            assert resp.status_code == 401  # noqa: PLR2004
            data = resp.json()
            assert data["error"]["code"] == "wrong_password"


@pytest.mark.asyncio
async def test_login_nonexistent_user_returns_401(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nonexistent user returns 401 (no enumeration leak)."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    from httpx import ASGITransport  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):  # noqa: SIM117
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/login",
                json={"username": "nobody", "password": "anypass"},
            )
            assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_login_missing_username_returns_422(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing username returns 422 validation error."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    from httpx import ASGITransport  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):  # noqa: SIM117
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/login",
                json={"password": "pass"},
            )
            assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_login_rate_limit_5_failed_attempts_then_429(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 failed login attempts from same IP exhaust the budget; 6th returns 429.

    Successful logins do NOT consume budget — only failed attempts do.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")

    from httpx import ASGITransport  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        await app.state.auth_repo.create_user("testuser", hash_password("testpass1234", cost=4))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 5 wrong-password attempts; each returns 401 wrong_password.
            for _ in range(5):
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "testuser", "password": "wrongpass1234"},
                )
                assert resp.status_code == 401  # noqa: PLR2004
                assert resp.json()["error"]["code"] == "wrong_password"

            # 6th wrong attempt is rate-limited.
            resp = await client.post(
                "/api/auth/login",
                json={"username": "testuser", "password": "wrongpass1234"},
            )
            assert resp.status_code == 429  # noqa: PLR2004
            assert resp.json()["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_login_successful_logins_do_not_consume_budget(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated successful logins do not consume the rate-limit budget."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")

    from httpx import ASGITransport  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        await app.state.auth_repo.create_user("testuser", hash_password("testpass1234", cost=4))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(7):
                resp = await client.post(
                    "/api/auth/login",
                    json={"username": "testuser", "password": "testpass1234"},
                )
                assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_login_cookie_attributes(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Login sets HttpOnly, SameSite=Lax, Path=/, Max-Age."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")

    from httpx import ASGITransport  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        await app.state.auth_repo.create_user("testuser", hash_password("testpass1234", cost=4))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/auth/login",
                json={"username": "testuser", "password": "testpass1234"},
            )
            assert resp.status_code == 200  # noqa: PLR2004
            # Check Set-Cookie headers
            set_cookie = resp.headers.get("set-cookie", "")
            # Should have HttpOnly, SameSite, Path, Max-Age
            assert "HttpOnly" in set_cookie or "httponly" in set_cookie.lower()
            assert "SameSite" in set_cookie or "samesite" in set_cookie.lower()
            assert "Path=/" in set_cookie
            assert "Max-Age" in set_cookie or "max-age" in set_cookie.lower()


@pytest.mark.asyncio
async def test_login_invalidates_prior_sessions(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second login by the same user invalidates the first session cookie."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")

    from httpx import ASGITransport  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        await app.state.auth_repo.create_user("testuser", hash_password("testpass1234", cost=4))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client1:
            resp = await client1.post(
                "/api/auth/login",
                json={"username": "testuser", "password": "testpass1234"},
            )
            assert resp.status_code == 200  # noqa: PLR2004
            old_session_cookie = client1.cookies.get("homelab_monitor_session")

        # Second login from a fresh client (simulating attacker with stolen creds)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client2:
            resp = await client2.post(
                "/api/auth/login",
                json={"username": "testuser", "password": "testpass1234"},
            )
            assert resp.status_code == 200  # noqa: PLR2004

        # Replay the OLD session cookie via a third client; should now be 401.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client3:
            client3.cookies.set("homelab_monitor_session", old_session_cookie)  # type: ignore[reportArgumentType]
            resp = await client3.get("/api/auth/me")
            assert resp.status_code == 401  # noqa: PLR2004
