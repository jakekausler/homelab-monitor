"""Tests for lifespan alert repo/dispatcher wiring.

Verifies that create_app(lifespan_enabled=True) correctly populates
app.state.alert_repo, app.state.alert_dispatcher, and that the
scheduler's failure_budget has both wired.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.dispatch.dispatcher import AlertDispatcher


@pytest_asyncio.fixture
async def app_with_alerts(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FastAPI]:
    """Bootstrap a full-lifespan app and yield it."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_HTTPS_ONLY_COOKIES", "false")
    monkeypatch.setenv("HOMELAB_MONITOR_BCRYPT_COST", "4")
    monkeypatch.setenv("HOMELAB_MONITOR_AUTO_MIGRATE", "1")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        yield app


@pytest.mark.asyncio
async def test_lifespan_constructs_alert_repo(app_with_alerts: FastAPI) -> None:
    """app.state.alert_repo is an AlertRepository after lifespan boot."""
    assert hasattr(app_with_alerts.state, "alert_repo")
    assert isinstance(app_with_alerts.state.alert_repo, AlertRepository)


@pytest.mark.asyncio
async def test_lifespan_constructs_alert_dispatcher(app_with_alerts: FastAPI) -> None:
    """app.state.alert_dispatcher is an AlertDispatcher after lifespan boot."""
    assert hasattr(app_with_alerts.state, "alert_dispatcher")
    assert isinstance(app_with_alerts.state.alert_dispatcher, AlertDispatcher)


@pytest.mark.asyncio
async def test_lifespan_passes_dispatcher_to_failure_budget(app_with_alerts: FastAPI) -> None:
    """The scheduler's failure_budget has alert_repo and dispatcher wired."""
    failure_budget = app_with_alerts.state.failure_budget
    assert failure_budget is not None

    alert_repo = failure_budget._alert_repo  # pyright: ignore[reportPrivateUsage]
    dispatcher = failure_budget._dispatcher  # pyright: ignore[reportPrivateUsage]

    assert isinstance(alert_repo, AlertRepository)
    assert isinstance(dispatcher, AlertDispatcher)

    # The wired instances must be the same objects as app.state
    assert alert_repo is app_with_alerts.state.alert_repo
    assert dispatcher is app_with_alerts.state.alert_dispatcher
