"""Tests for kernel.dispatch.dispatcher.AlertDispatcher and InprocDashboardChannel."""

from __future__ import annotations

from typing import ClassVar

import pytest
import structlog

from homelab_monitor.kernel.alerts.events import AlertFiringEvent
from homelab_monitor.kernel.alerts.types import Severity
from homelab_monitor.kernel.dispatch.channels.inproc_dashboard import InprocDashboardChannel
from homelab_monitor.kernel.dispatch.dispatcher import AlertDispatcher
from homelab_monitor.kernel.dispatch.types import AlertEvent
from homelab_monitor.kernel.events import BaseEvent


def _make_firing_event() -> AlertFiringEvent:
    return AlertFiringEvent(
        alert_id="aid-1",
        fingerprint="fp-1",
        source_tool="alertmanager",
        severity=Severity.WARNING,
        opened_at="2026-05-07T00:00:00+00:00",
        last_seen_at="2026-05-07T00:00:00+00:00",
        labels={"alertname": "Foo"},
        ts="2026-05-07T00:00:00+00:00",
    )


class _RecordingChannel:
    kind: ClassVar[str] = "recorder"

    def __init__(self) -> None:
        self.received: list[AlertEvent] = []

    async def deliver(self, event: AlertEvent) -> None:
        self.received.append(event)

    def accepts(self, event: AlertEvent) -> bool:
        del event
        return True


class _FailingChannel:
    kind: ClassVar[str] = "boom"

    async def deliver(self, event: AlertEvent) -> None:
        del event
        msg = "kaboom"
        raise RuntimeError(msg)

    def accepts(self, event: AlertEvent) -> bool:
        del event
        return True


class _FakeBroker:
    def __init__(self) -> None:
        self.published: list[BaseEvent] = []

    async def publish(self, event: BaseEvent) -> None:
        self.published.append(event)


@pytest.mark.asyncio
async def test_dispatch_calls_each_channel() -> None:
    log = structlog.get_logger()
    c1 = _RecordingChannel()
    c2 = _RecordingChannel()
    dispatcher = AlertDispatcher(channels=[c1, c2], log=log)

    event = _make_firing_event()
    results = await dispatcher.dispatch(event)

    assert len(c1.received) == 1
    assert len(c2.received) == 1
    assert all(r.ok for r in results)


@pytest.mark.asyncio
async def test_dispatch_per_channel_failure_does_not_raise() -> None:
    log = structlog.get_logger()
    failing = _FailingChannel()
    healthy = _RecordingChannel()
    dispatcher = AlertDispatcher(channels=[failing, healthy], log=log)

    event = _make_firing_event()
    results = await dispatcher.dispatch(event)  # MUST NOT raise

    # Healthy channel still got the event despite failing's exception.
    assert len(healthy.received) == 1
    # First result is the failure, second is the success.
    by_kind = {r.channel_kind: r for r in results}
    assert by_kind["boom"].ok is False
    assert by_kind["boom"].error == "kaboom"
    assert by_kind["recorder"].ok is True


@pytest.mark.asyncio
async def test_dispatch_increments_failure_counter() -> None:
    log = structlog.get_logger()
    failing = _FailingChannel()
    dispatcher = AlertDispatcher(channels=[failing], log=log)

    event = _make_firing_event()
    await dispatcher.dispatch(event)
    await dispatcher.dispatch(event)

    counters = dispatcher.delivery_failures
    assert counters["boom"] == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_inproc_dashboard_publishes_to_broker() -> None:
    broker = _FakeBroker()
    channel = InprocDashboardChannel(broker=broker)
    event = _make_firing_event()

    await channel.deliver(event)

    assert broker.published == [event]
