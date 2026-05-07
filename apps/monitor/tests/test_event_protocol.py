"""Tests for the BaseEvent Protocol and alert event conformance."""

from __future__ import annotations

import pytest
import structlog

from homelab_monitor.kernel.alerts.events import AlertFiringEvent, AlertResolvedEvent
from homelab_monitor.kernel.api.sse import SseBroker
from homelab_monitor.kernel.events import BaseEvent, SchedulerTickEvent


def test_scheduler_tick_event_conforms_to_baseevent() -> None:
    event = SchedulerTickEvent(
        collector="c", tick_id="t", outcome="success", ts="2026-05-07T00:00:00+00:00"
    )
    assert hasattr(event, "kind")
    assert callable(getattr(event, "model_dump", None))
    dumped = event.model_dump(mode="json")
    assert dumped["kind"] == "collector.tick"


def test_alert_firing_event_conforms_to_baseevent() -> None:
    event = AlertFiringEvent(
        alert_id="a",
        fingerprint="fp",
        source_tool="alertmanager",
        severity="warning",
        opened_at="2026-05-07T00:00:00+00:00",
        last_seen_at="2026-05-07T00:00:00+00:00",
        labels={},
        ts="2026-05-07T00:00:00+00:00",
    )
    assert event.kind == "alert.firing"
    dumped = event.model_dump(mode="json")
    assert dumped["kind"] == "alert.firing"


def test_alert_resolved_event_conforms_to_baseevent() -> None:
    event = AlertResolvedEvent(
        alert_id="a",
        fingerprint="fp",
        source_tool="alertmanager",
        severity="warning",
        resolved_at="2026-05-07T00:00:00+00:00",
        labels={},
        ts="2026-05-07T00:00:00+00:00",
    )
    assert event.kind == "alert.resolved"


def test_baseevent_protocol_runtime_checkable() -> None:
    """BaseEvent is @runtime_checkable; isinstance() works."""
    tick = SchedulerTickEvent(
        collector="c", tick_id="t", outcome="success", ts="2026-05-07T00:00:00+00:00"
    )
    firing = AlertFiringEvent(
        alert_id="a",
        fingerprint="fp",
        source_tool="alertmanager",
        severity="warning",
        opened_at="2026-05-07T00:00:00+00:00",
        last_seen_at="2026-05-07T00:00:00+00:00",
        labels={},
        ts="2026-05-07T00:00:00+00:00",
    )
    assert isinstance(tick, BaseEvent)
    assert isinstance(firing, BaseEvent)


@pytest.mark.asyncio
async def test_sse_broker_publishes_alert_firing_event() -> None:
    """SseBroker.publish accepts AlertFiringEvent (was SchedulerTickEvent only)."""
    broker = SseBroker(log=structlog.get_logger())
    event = AlertFiringEvent(
        alert_id="a",
        fingerprint="fp",
        source_tool="alertmanager",
        severity="warning",
        opened_at="2026-05-07T00:00:00+00:00",
        last_seen_at="2026-05-07T00:00:00+00:00",
        labels={"alertname": "Foo"},
        ts="2026-05-07T00:00:00+00:00",
    )
    await broker.publish(event)  # MUST NOT raise

    # Verify the ring buffer captured the alert event with the correct kind.
    # Access the private ring via pyright suppression; this is a unit test.
    ring = list(broker._ring)  # pyright: ignore[reportPrivateUsage]
    assert len(ring) == 1
    assert ring[0].kind == "alert.firing"
    assert ring[0].payload["alert_id"] == "a"
