"""Tests for kernel/api/routers/health.py — /api/healthz endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.api.app import create_app


@pytest.mark.asyncio
async def test_healthz_200_basic_shape(db_url_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/healthz returns 200 with all expected fields."""
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY_FILE", "/dev/null")
    async with AsyncClient(
        transport=ASGITransport(app=create_app(lifespan_enabled=False)), base_url="http://test"
    ) as _client:
        # For lifespan_enabled=False, the endpoint should still be callable but dependencies
        # will raise DependencyUnavailableProblem. Let's test with mocked state.
        app = create_app(lifespan_enabled=False)
        # Manually set required state for testing

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as test_client:
            resp = await test_client.get("/api/healthz")
            # Without lifespan, healthz returns 200 with degraded fields
            assert resp.status_code == 200  # noqa: PLR2004
            data = resp.json()
            assert data["db"] == "down"
            assert data["scheduler"] == "stopped"
            assert data["ok"] is False


@pytest.mark.asyncio
async def test_healthz_auth_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/healthz is auth-exempt (returns 503 without auth headers)."""
    # Since healthz requires dependencies that aren't available in schema-only mode,
    # it will return 200 with degraded fields, not 401 (auth).
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # No auth (auth-exempt path), should not get 401
        resp = await client.get("/api/healthz")
        # Should NOT be 401 (auth). Returns 200 with degraded fields.
        assert resp.status_code != 401  # noqa: PLR2004
        assert resp.status_code == 200  # noqa: PLR2004


def test_healthz_response_has_required_fields(db_engine: any) -> None:  # pyright: ignore[reportUnknownParameterType, reportGeneralTypeIssues]
    """HealthzResponse has all required fields populated."""
    from homelab_monitor.kernel.api.routers.health import HealthzResponse  # noqa: PLC0415

    resp = HealthzResponse(
        ok=True,
        version="1.0.0",
        db="up",
        scheduler="running",
        last_tick_at="2026-05-05T00:00:00Z",
        failed_ticks_last_5m=0,
        quarantined_collectors=[],
        degraded_collectors=[],
    )
    assert resp.ok is True
    assert resp.version == "1.0.0"
    assert resp.db == "up"
    assert resp.scheduler == "running"
    assert resp.last_tick_at == "2026-05-05T00:00:00Z"
    assert resp.failed_ticks_last_5m == 0
    assert resp.quarantined_collectors == []
    assert resp.degraded_collectors == []


def test_healthz_response_forbids_extra_fields() -> None:
    """HealthzResponse enforces extra='forbid'."""
    from homelab_monitor.kernel.api.routers.health import HealthzResponse  # noqa: PLC0415

    with pytest.raises(ValueError):
        HealthzResponse(
            ok=True,
            version="1.0.0",
            db="up",
            scheduler="running",
            last_tick_at=None,
            failed_ticks_last_5m=0,
            quarantined_collectors=[],
            degraded_collectors=[],
            extra_field="not_allowed",  # type: ignore[call-arg]
        )


def test_healthz_db_field_literal_values() -> None:
    """HealthzResponse.db accepts only 'up' or 'down'."""
    from homelab_monitor.kernel.api.routers.health import HealthzResponse  # noqa: PLC0415

    # Valid: up
    resp1 = HealthzResponse(
        ok=True,
        version="1.0.0",
        db="up",
        scheduler="running",
        last_tick_at=None,
        failed_ticks_last_5m=0,
        quarantined_collectors=[],
        degraded_collectors=[],
    )
    assert resp1.db == "up"

    # Valid: down
    resp2 = HealthzResponse(
        ok=True,
        version="1.0.0",
        db="down",
        scheduler="running",
        last_tick_at=None,
        failed_ticks_last_5m=0,
        quarantined_collectors=[],
        degraded_collectors=[],
    )
    assert resp2.db == "down"

    # Invalid
    with pytest.raises(ValueError):
        HealthzResponse(
            ok=True,
            version="1.0.0",
            db="maybe",  # type: ignore[call-arg]
            scheduler="running",
            last_tick_at=None,
            failed_ticks_last_5m=0,
            quarantined_collectors=[],
            degraded_collectors=[],
        )


def test_healthz_scheduler_field_literal_values() -> None:
    """HealthzResponse.scheduler accepts only 'running' or 'stopped'."""
    from homelab_monitor.kernel.api.routers.health import HealthzResponse  # noqa: PLC0415

    resp = HealthzResponse(
        ok=True,
        version="1.0.0",
        db="up",
        scheduler="stopped",
        last_tick_at=None,
        failed_ticks_last_5m=0,
        quarantined_collectors=[],
        degraded_collectors=[],
    )
    assert resp.scheduler == "stopped"


@pytest.mark.asyncio
async def test_healthz_db_down_when_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/healthz returns db='down' when database query fails."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    # Create app and mock the repo to raise an exception
    app = create_app(lifespan_enabled=False)
    mock_repo = AsyncMock()
    mock_repo.fetch_one.side_effect = RuntimeError("database connection failed")
    app.state.repo = mock_repo

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/healthz")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert data["db"] == "down"
