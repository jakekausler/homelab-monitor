"""API tests for /api/settings/logs/retention (STAGE-004-022)."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

_URL = "/api/settings/logs/retention"


def _csrf(client: AsyncClient) -> dict[str, str]:
    csrf: str = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


@pytest.mark.asyncio
async def test_get_requires_session(authenticated_client: AsyncClient) -> None:
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(_URL)
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_default_no_env_no_override(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", raising=False)
    resp = await authenticated_client.get(_URL)
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["retention_days"] == 30  # noqa: PLR2004
    assert body["pending_retention_days"] is None
    assert body["retention_source"] == "default"
    assert body["restart_required"] is False
    # disk + thresholds present
    assert "disk_used_gb" in body
    assert "disk_used_pct" in body
    assert "disk_budget_available" in body
    assert body["warn_pct"] == 70  # noqa: PLR2004
    assert body["crit_pct"] == 85  # noqa: PLR2004


@pytest.mark.asyncio
async def test_get_env_source(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", "14")
    resp = await authenticated_client.get(_URL)
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["retention_days"] == 14  # noqa: PLR2004
    assert body["retention_source"] == "env"


@pytest.mark.asyncio
async def test_get_env_set_to_default_value_source_is_env(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET with env=30 (equals default) must return retention_source='env'."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", "30")
    resp = await authenticated_client.get(_URL)
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["retention_days"] == 30  # noqa: PLR2004
    assert body["retention_source"] == "env"


@pytest.mark.asyncio
async def test_patch_persists_pending(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", raising=False)
    resp = await authenticated_client.patch(
        _URL, json={"retention_days": 90}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["retention_days"] == 30  # effective unchanged  # noqa: PLR2004
    assert body["pending_retention_days"] == 90  # noqa: PLR2004
    assert body["restart_required"] is True
    assert body["retention_source"] == "runtime"
    # GET reflects the persisted pending
    get_body = (await authenticated_client.get(_URL)).json()
    assert get_body["pending_retention_days"] == 90  # noqa: PLR2004
    assert get_body["restart_required"] is True


@pytest.mark.asyncio
async def test_patch_equals_effective_no_restart(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_VL_RETENTION_DAYS", raising=False)
    # stash a pending first
    await authenticated_client.patch(
        _URL, json={"retention_days": 90}, headers=_csrf(authenticated_client)
    )
    resp = await authenticated_client.patch(
        _URL, json={"retention_days": 30}, headers=_csrf(authenticated_client)
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["pending_retention_days"] is None
    assert body["restart_required"] is False


@pytest.mark.asyncio
async def test_patch_out_of_range_returns_422(authenticated_client: AsyncClient) -> None:
    csrf = _csrf(authenticated_client)
    for bad in (0, 366):
        resp = await authenticated_client.patch(_URL, json={"retention_days": bad}, headers=csrf)
        assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_patch_requires_session(authenticated_client: AsyncClient) -> None:
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.patch(_URL, json={"retention_days": 90})
    assert resp.status_code == 401  # noqa: PLR2004
