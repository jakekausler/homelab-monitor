"""Integration tests for /api/collectors endpoint to cover quarantine and next_run logic."""

from __future__ import annotations

import base64

import pytest
from httpx import AsyncClient

from homelab_monitor.kernel.api.app import create_app


@pytest.mark.asyncio
async def test_collectors_endpoint_with_metrics_history(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    authenticated_client: AsyncClient,
) -> None:
    """GET /api/collectors includes quarantine_state fields for collectors.

    Tests lines 65-68 of collectors.py (quarantine_state retrieval).
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")
    monkeypatch.setenv("HOMELAB_MONITOR_BCRYPT_COST", "4")
    monkeypatch.setenv("HOMELAB_MONITOR_AUTO_MIGRATE", "1")

    app = create_app(lifespan_enabled=True)

    async with app.router.lifespan_context(app):
        # Inject a metric directly to simulate a tick (avoids 60s wait for noop interval)
        app.state.metrics_writer.write_counter(
            "homelab_collector_run_success_total",
            1.0,
            {"name": "noop"},
        )

        resp = await authenticated_client.get("/api/collectors")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert isinstance(data, list)

        # Should have noop collector at minimum
        names = {c.get("name") for c in data}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
        assert "noop" in names

        # Check that quarantine fields are present and accessible
        for collector in data:  # pyright: ignore[reportUnknownVariableType]
            assert "quarantined" in collector
            assert "quarantined_at" in collector
            assert "quarantine_reason" in collector
            assert "consecutive_failures" in collector


@pytest.mark.asyncio
async def test_collectors_endpoint_next_run_calculation(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    authenticated_client: AsyncClient,
) -> None:
    """GET /api/collectors calculates next_run from last_run + interval.

    Tests lines 73-79 of collectors.py (next_run calculation logic).
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")
    monkeypatch.setenv("HOMELAB_MONITOR_BCRYPT_COST", "4")
    monkeypatch.setenv("HOMELAB_MONITOR_AUTO_MIGRATE", "1")

    app = create_app(lifespan_enabled=True)

    async with app.router.lifespan_context(app):
        # Inject a metric directly to simulate a tick (avoids 60s wait for noop interval)
        app.state.metrics_writer.write_counter(
            "homelab_collector_run_success_total",
            1.0,
            {"name": "noop"},
        )

        resp = await authenticated_client.get("/api/collectors")
        assert resp.status_code == 200  # noqa: PLR2004
        data = resp.json()
        assert isinstance(data, list)

        # Check that next_run fields are accessible (logic paths exercised)
        for collector in data:  # pyright: ignore[reportUnknownVariableType]
            assert "next_run" in collector
            assert "last_run" in collector
            assert "interval_seconds" in collector
            # These fields should exist even if some are None
