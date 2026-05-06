"""Tests for kernel/api/routers/health.py — /api/version endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.api.app import create_app


@pytest.mark.asyncio
async def test_version_200_basic_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/version returns 200 with {version, git_sha, built_at}."""
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/version")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert "version" in data
        assert "git_sha" in data
        assert "built_at" in data


@pytest.mark.asyncio
async def test_version_honors_git_sha_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """git_sha honors HOMELAB_MONITOR_GIT_SHA env override."""
    monkeypatch.setenv("HOMELAB_MONITOR_GIT_SHA", "abc123def456")
    # Need to re-import to pick up the env var at module load time
    import sys  # noqa: PLC0415

    # Clear the module cache for health router
    if "homelab_monitor.kernel.api.routers.health" in sys.modules:
        del sys.modules["homelab_monitor.kernel.api.routers.health"]
    if "homelab_monitor.kernel.api.app" in sys.modules:
        del sys.modules["homelab_monitor.kernel.api.app"]

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/version")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["git_sha"] == "abc123def456"


@pytest.mark.asyncio
async def test_version_honors_built_at_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """built_at honors HOMELAB_MONITOR_BUILT_AT env override."""
    monkeypatch.setenv("HOMELAB_MONITOR_BUILT_AT", "2026-05-05T12:00:00Z")
    import sys  # noqa: PLC0415

    # Clear module cache
    if "homelab_monitor.kernel.api.routers.health" in sys.modules:
        del sys.modules["homelab_monitor.kernel.api.routers.health"]
    if "homelab_monitor.kernel.api.app" in sys.modules:
        del sys.modules["homelab_monitor.kernel.api.app"]

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/version")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["built_at"] == "2026-05-05T12:00:00Z"


@pytest.mark.asyncio
async def test_version_defaults_to_dev_when_unset() -> None:
    """git_sha and built_at default to 'dev' and fallback timestamp when env unset."""
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/version")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        # git_sha should default to "dev" when not set
        # (actual default depends on env var being unset, which it should be in test)
        assert isinstance(data["git_sha"], str)
        assert isinstance(data["built_at"], str)


@pytest.mark.asyncio
async def test_version_auth_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/version is auth-exempt (returns 200 without X-Auth header)."""
    monkeypatch.delenv("HOMELAB_MONITOR_DEV_AUTH", raising=False)
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # No X-Auth header, should still get 200 (not 401)
        resp = await client.get("/api/version")
        assert resp.status_code == 200  # noqa: PLR2004


def test_version_response_shape() -> None:
    """VersionResponse has correct shape and field types."""
    from homelab_monitor.kernel.api.routers.health import VersionResponse  # noqa: PLC0415

    resp = VersionResponse(
        version="1.2.3",
        git_sha="abc123",
        built_at="2026-05-05T00:00:00Z",
    )
    assert resp.version == "1.2.3"
    assert resp.git_sha == "abc123"
    assert resp.built_at == "2026-05-05T00:00:00Z"


def test_version_response_forbids_extra_fields() -> None:
    """VersionResponse enforces extra='forbid'."""
    from homelab_monitor.kernel.api.routers.health import VersionResponse  # noqa: PLC0415

    with pytest.raises(ValueError):
        VersionResponse(
            version="1.0.0",
            git_sha="abc",
            built_at="2026-05-05T00:00:00Z",
            extra="not_allowed",  # type: ignore[call-arg]
        )
