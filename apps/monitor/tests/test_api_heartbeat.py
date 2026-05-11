"""Unit tests for /api/hb/{path} heartbeat stub endpoint."""

from __future__ import annotations

import base64

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_heartbeat_returns_401_without_auth(authenticated_client: AsyncClient) -> None:
    """POST /api/hb/test/ok without Authorization returns 401.

    Note: authenticated_client carries a session cookie. The heartbeat router
    is token-only (no session path) so the cookie is rejected and 401 is
    returned. We use authenticated_client only to share the lifespan setup.
    """
    # Strip session cookies so the request is anonymous.
    authenticated_client.cookies.clear()
    resp = await authenticated_client.post("/api/hb/test/ok", json={})
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_heartbeat_returns_401_with_session_only(
    authenticated_client: AsyncClient,
) -> None:
    """POST /api/hb/test/ok with session cookie ONLY (no token) returns 401.

    Heartbeat is token-only. Session is not accepted.
    """
    csrf_cookie = authenticated_client.cookies.get("homelab_monitor_csrf", "")
    headers: dict[str, str] = {}
    if csrf_cookie:
        headers["X-CSRF-Token"] = csrf_cookie
    resp = await authenticated_client.post(
        "/api/hb/test/ok",
        json={},
        headers=headers,
    )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_heartbeat_returns_204_with_valid_token(api_token_client: AsyncClient) -> None:
    """POST /api/hb/test/ok with a HEARTBEAT_WRITE token returns 204."""
    resp = await api_token_client.post("/api/hb/test/ok", json={})
    assert resp.status_code == 204  # noqa: PLR2004
    assert resp.content == b""


@pytest.mark.asyncio
async def test_heartbeat_returns_204_with_arbitrary_subpath(
    api_token_client: AsyncClient,
) -> None:
    """The path after /hb/ is accepted as-is (EPIC-002 will validate against registry)."""
    resp = await api_token_client.post("/api/hb/foo/bar/baz/qux", json={"any": "payload"})
    assert resp.status_code == 204  # noqa: PLR2004


@pytest.mark.asyncio
async def test_heartbeat_returns_403_with_wrong_scope_token(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token without HEARTBEAT_WRITE scope returns 403 insufficient_scope."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        plaintext, _ = make_api_token(prefix="test")
        await app.state.auth_repo.create_api_token(
            name="no-hb-token",
            scopes={Scope.READ_STATUS},  # NOT HEARTBEAT_WRITE
            plaintext_token=plaintext,
        )
        headers: dict[str, str] = {"Authorization": f"Bearer {plaintext}"}
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client:
            resp = await client.post("/api/hb/test/ok", json={})
            assert resp.status_code == 403  # noqa: PLR2004
