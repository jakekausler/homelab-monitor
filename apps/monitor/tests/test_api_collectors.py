"""Tests for kernel/api/routers/collectors.py — /api/collectors endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.api.app import create_app


@pytest.mark.asyncio
async def test_collectors_list_shape(authenticated_client: AsyncClient) -> None:
    """GET /api/collectors returns 200 with list of CollectorStatus."""
    resp = await authenticated_client.get("/api/collectors")
    # authenticated_client boots full lifespan, so dependencies are wired
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_collectors_401_without_auth_header() -> None:
    """GET /api/collectors returns 401 without session cookie."""
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/collectors")
        # require_session() runs before get_loader, so 401 wins over the 503
        assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_collectors_401_when_no_session() -> None:
    """GET /api/collectors returns 401 when no valid session cookie."""
    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/collectors")
        # Should reject due to missing session
        assert resp.status_code == 401  # noqa: PLR2004


def test_collector_status_response_shape() -> None:
    """CollectorStatus has all required fields."""
    from homelab_monitor.kernel.api.routers.collectors import CollectorStatus  # noqa: PLC0415

    status = CollectorStatus(
        name="test-collector",
        status="healthy",
        last_run="2026-05-05T00:00:00Z",
        last_error=None,
        quarantined=False,
        quarantined_at=None,
        quarantine_reason=None,
        next_run="2026-05-05T01:00:00Z",
        run_kind="async",
        interval_seconds=3600.0,
        consecutive_failures=0,
    )
    assert status.name == "test-collector"
    assert status.status == "healthy"
    assert status.run_kind == "async"
    assert status.interval_seconds == 3600.0  # noqa: PLR2004


def test_collector_status_status_field_literal() -> None:
    """CollectorStatus.status accepts only 'healthy', 'quarantined', or 'degraded'."""
    from homelab_monitor.kernel.api.routers.collectors import CollectorStatus  # noqa: PLC0415

    # Valid: healthy
    s1 = CollectorStatus(
        name="test",
        status="healthy",
        last_run=None,
        last_error=None,
        quarantined=False,
        quarantined_at=None,
        quarantine_reason=None,
        next_run=None,
        run_kind="async",
        interval_seconds=60.0,
        consecutive_failures=0,
    )
    assert s1.status == "healthy"

    # Valid: quarantined
    s2 = CollectorStatus(
        name="test",
        status="quarantined",
        last_run=None,
        last_error=None,
        quarantined=True,
        quarantined_at="2026-05-05T00:00:00Z",
        quarantine_reason="too many failures",
        next_run=None,
        run_kind="async",
        interval_seconds=60.0,
        consecutive_failures=5,
    )
    assert s2.status == "quarantined"

    # Valid: degraded
    s3 = CollectorStatus(
        name="test",
        status="degraded",
        last_run=None,
        last_error="failed to load",
        quarantined=False,
        quarantined_at=None,
        quarantine_reason=None,
        next_run=None,
        run_kind="async",
        interval_seconds=60.0,
        consecutive_failures=0,
    )
    assert s3.status == "degraded"


def test_collector_status_forbids_extra_fields() -> None:
    """CollectorStatus enforces extra='forbid'."""
    from homelab_monitor.kernel.api.routers.collectors import CollectorStatus  # noqa: PLC0415

    with pytest.raises(ValueError):
        CollectorStatus(
            name="test",
            status="healthy",
            last_run=None,
            last_error=None,
            quarantined=False,
            quarantined_at=None,
            quarantine_reason=None,
            next_run=None,
            run_kind="async",
            interval_seconds=60.0,
            consecutive_failures=0,
            extra_field="not_allowed",  # type: ignore[call-arg]
        )


def test_collector_status_run_kind_values() -> None:
    """CollectorStatus.run_kind accepts 'async', 'thread', 'process', 'subprocess'."""
    from homelab_monitor.kernel.api.routers.collectors import CollectorStatus  # noqa: PLC0415

    for run_kind in ("async", "thread", "process", "subprocess"):
        status = CollectorStatus(
            name="test",
            status="healthy",
            last_run=None,
            last_error=None,
            quarantined=False,
            quarantined_at=None,
            quarantine_reason=None,
            next_run=None,
            run_kind=run_kind,  # type: ignore[assignment]
            interval_seconds=60.0,
            consecutive_failures=0,
        )
        assert status.run_kind == run_kind


def test_collector_next_run_calculation_from_last_run() -> None:
    """next_run is calculated as max(last_run + interval, now) when last_run present."""
    from datetime import datetime, timedelta  # noqa: PLC0415

    from homelab_monitor.kernel.api.routers.collectors import CollectorStatus  # noqa: PLC0415

    last_run_iso = "2026-05-05T12:00:00Z"
    interval_s = 3600.0
    # next_run should be at least last_run + interval
    status = CollectorStatus(
        name="test",
        status="healthy",
        last_run=last_run_iso,
        last_error=None,
        quarantined=False,
        quarantined_at=None,
        quarantine_reason=None,
        next_run="2026-05-05T13:00:00Z",
        run_kind="async",
        interval_seconds=interval_s,
        consecutive_failures=0,
    )
    assert status.next_run == "2026-05-05T13:00:00Z"
    # next_run must never be in the past
    last_run_dt = datetime.fromisoformat(last_run_iso.replace("Z", "+00:00"))
    next_run_dt = datetime.fromisoformat(status.next_run.replace("Z", "+00:00"))
    expected_min = last_run_dt + timedelta(seconds=int(interval_s))
    assert next_run_dt >= expected_min


def test_collector_quarantine_state_retrieved_when_quarantined() -> None:
    """quarantined_at and quarantine_reason are set when collector is quarantined."""
    from homelab_monitor.kernel.api.routers.collectors import CollectorStatus  # noqa: PLC0415

    status = CollectorStatus(
        name="quarantined_test",
        status="quarantined",
        last_run="2026-05-05T10:00:00Z",
        last_error="persistent failures",
        quarantined=True,
        quarantined_at="2026-05-05T11:00:00Z",
        quarantine_reason="exceeded_failure_budget",
        next_run=None,
        run_kind="async",
        interval_seconds=60.0,
        consecutive_failures=10,
    )
    assert status.quarantined is True
    assert status.quarantined_at == "2026-05-05T11:00:00Z"
    assert status.quarantine_reason == "exceeded_failure_budget"
