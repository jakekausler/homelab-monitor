"""Tests for STAGE-005-018 minimal severity routing (stopgap gate).

# TODO: STOPGAP — retire when EPIC-012 STAGE-012-005 lands (full routing engine)

Tests assert the PUBLIC API only. No _private symbols are imported in test
assertions; pyright-ignore annotations are inline where required.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from homelab_monitor.kernel.alerts.events import AlertFiringEvent, AlertResolvedEvent
from homelab_monitor.kernel.alerts.types import Severity
from homelab_monitor.kernel.dispatch.channels.ha_push import HAPushChannel
from homelab_monitor.kernel.dispatch.channels.inproc_dashboard import InprocDashboardChannel
from homelab_monitor.kernel.dispatch.dispatcher import AlertDispatcher
from homelab_monitor.kernel.dispatch.types import AlertEvent
from homelab_monitor.kernel.events import BaseEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _firing(
    severity: Severity = Severity.WARNING, labels: dict[str, str] | None = None
) -> AlertFiringEvent:
    base_labels: dict[str, str] = {"alertname": "TestAlert"}
    if labels is not None:
        base_labels.update(labels)
    return AlertFiringEvent(
        alert_id="aid-1",
        fingerprint="fp-1",
        source_tool="alertmanager",
        severity=severity,
        opened_at="2026-06-12T00:00:00+00:00",
        last_seen_at="2026-06-12T00:00:00+00:00",
        labels=base_labels,
        ts="2026-06-12T00:00:00+00:00",
    )


def _resolved(
    severity: Severity = Severity.WARNING, labels: dict[str, str] | None = None
) -> AlertResolvedEvent:
    base_labels: dict[str, str] = {"alertname": "TestAlert"}
    if labels is not None:
        base_labels.update(labels)
    return AlertResolvedEvent(
        alert_id="aid-2",
        fingerprint="fp-2",
        source_tool="alertmanager",
        severity=severity,
        resolved_at="2026-06-12T01:00:00+00:00",
        labels=base_labels,
        ts="2026-06-12T01:00:00+00:00",
    )


def _make_ha_push_channel() -> HAPushChannel:
    """Build an HAPushChannel with a fake client; notify_service non-empty."""
    fake_client = MagicMock()
    fake_client.call_service = AsyncMock(return_value=None)
    return HAPushChannel(  # pyright: ignore[reportArgumentType]
        client=fake_client,
        notify_service="mobile_app_test",
    )


class _RecordingChannel:
    """Accept-all recording channel for dispatcher integration tests."""

    kind: ClassVar[str] = "recorder"

    def __init__(self) -> None:
        self.received: list[AlertEvent] = []

    async def deliver(self, event: AlertEvent) -> None:
        self.received.append(event)

    def accepts(self, event: AlertEvent) -> bool:
        del event
        return True


class _GatedRecordingChannel:
    """Accept-only-error/critical recording channel (mirrors HAPushChannel.accepts logic)."""

    kind: ClassVar[str] = "gated_recorder"

    def __init__(self) -> None:
        self.received: list[AlertEvent] = []

    async def deliver(self, event: AlertEvent) -> None:
        self.received.append(event)

    def accepts(self, event: AlertEvent) -> bool:
        raw = (event.labels.get("severity") or "").strip().lower()
        return raw in {"error", "critical"}


class _FakeBroker:
    def __init__(self) -> None:
        self.published: list[BaseEvent] = []

    async def publish(self, event: BaseEvent) -> None:
        self.published.append(event)


# ---------------------------------------------------------------------------
# HAPushChannel.accepts unit tests
# ---------------------------------------------------------------------------


class TestHAPushChannelAccepts:
    def test_accepts_error_label(self) -> None:
        ch = _make_ha_push_channel()
        event = _firing(labels={"severity": "error"})
        assert ch.accepts(event) is True

    def test_accepts_critical_label(self) -> None:
        ch = _make_ha_push_channel()
        event = _firing(labels={"severity": "critical"})
        assert ch.accepts(event) is True

    def test_rejects_warning_label(self) -> None:
        ch = _make_ha_push_channel()
        event = _firing(labels={"severity": "warning"})
        assert ch.accepts(event) is False

    def test_rejects_info_label(self) -> None:
        ch = _make_ha_push_channel()
        event = _firing(labels={"severity": "info"})
        assert ch.accepts(event) is False

    def test_rejects_missing_severity_label(self) -> None:
        """Fail-closed: missing label -> False."""
        ch = _make_ha_push_channel()
        event = _firing(labels={})  # no severity key
        assert ch.accepts(event) is False

    def test_accepts_mixed_case_Error(self) -> None:
        ch = _make_ha_push_channel()
        event = _firing(labels={"severity": "Error"})
        assert ch.accepts(event) is True

    def test_accepts_mixed_case_CRITICAL(self) -> None:
        ch = _make_ha_push_channel()
        event = _firing(labels={"severity": "CRITICAL"})
        assert ch.accepts(event) is True

    def test_rejects_mixed_case_Warning(self) -> None:
        ch = _make_ha_push_channel()
        event = _firing(labels={"severity": "Warning"})
        assert ch.accepts(event) is False

    def test_accepts_resolved_error(self) -> None:
        """accepts() works uniformly for AlertResolvedEvent."""
        ch = _make_ha_push_channel()
        event = _resolved(labels={"severity": "error"})
        assert ch.accepts(event) is True

    def test_rejects_resolved_warning(self) -> None:
        ch = _make_ha_push_channel()
        event = _resolved(labels={"severity": "warning"})
        assert ch.accepts(event) is False


# ---------------------------------------------------------------------------
# InprocDashboardChannel.accepts unit tests
# ---------------------------------------------------------------------------


class TestInprocDashboardChannelAccepts:
    def test_accepts_all_severities(self) -> None:
        broker = _FakeBroker()
        ch = InprocDashboardChannel(broker=broker)
        for sev in [Severity.INFO, Severity.WARNING, Severity.CRITICAL]:
            event = _firing(severity=sev)
            assert ch.accepts(event) is True

    def test_accepts_resolved_event(self) -> None:
        broker = _FakeBroker()
        ch = InprocDashboardChannel(broker=broker)
        event = _resolved()
        assert ch.accepts(event) is True


# ---------------------------------------------------------------------------
# AlertDispatcher integration tests (severity routing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_error_event_reaches_both_channels() -> None:
    """error-labeled event: default-accept channel AND gated channel both receive it."""
    log = structlog.get_logger()
    dashboard = _RecordingChannel()
    gated = _GatedRecordingChannel()
    dispatcher = AlertDispatcher(channels=[dashboard, gated], log=log)

    event = _firing(labels={"severity": "error"})
    results = await dispatcher.dispatch(event)

    assert len(dashboard.received) == 1
    assert len(gated.received) == 1
    assert len(results) == 2  # noqa: PLR2004
    assert all(r.ok for r in results)


@pytest.mark.asyncio
async def test_dispatch_warning_event_reaches_only_dashboard() -> None:
    """warning-labeled event: gated channel is skipped, dashboard receives it."""
    log = structlog.get_logger()
    dashboard = _RecordingChannel()
    gated = _GatedRecordingChannel()
    dispatcher = AlertDispatcher(channels=[dashboard, gated], log=log)

    event = _firing(labels={"severity": "warning"})
    results = await dispatcher.dispatch(event)

    assert len(dashboard.received) == 1
    assert len(gated.received) == 0
    # Only 1 DeliveryResult: gated channel was skipped (no result appended)
    assert len(results) == 1
    assert results[0].channel_kind == "recorder"
    assert results[0].ok is True


@pytest.mark.asyncio
async def test_dispatch_warning_skips_real_ha_push_channel() -> None:
    """Skip semantics pinned against the REAL HAPushChannel (not a mirror):
    a warning-labeled event yields no DeliveryResult for ha_push and never
    calls the HA client.
    """
    log = structlog.get_logger()
    dashboard = _RecordingChannel()
    ha_push = _make_ha_push_channel()
    dispatcher = AlertDispatcher(channels=[dashboard, ha_push], log=log)

    event = _firing(labels={"severity": "warning"})
    results = await dispatcher.dispatch(event)

    assert len(dashboard.received) == 1
    assert len(results) == 1
    assert results[0].channel_kind == "recorder"
    ha_push._client.call_service.assert_not_awaited()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue, reportFunctionMemberAccess, reportUnknownMemberType]


@pytest.mark.asyncio
async def test_dispatch_error_delivers_to_real_ha_push_channel() -> None:
    """Deliver semantics pinned against the REAL HAPushChannel: an
    error-labeled event produces one ok result and calls the HA client once.
    """
    log = structlog.get_logger()
    ha_push = _make_ha_push_channel()
    dispatcher = AlertDispatcher(channels=[ha_push], log=log)

    event = _firing(labels={"severity": "error"})
    results = await dispatcher.dispatch(event)

    assert len(results) == 1
    assert results[0].channel_kind == "ha_push"
    assert results[0].ok is True
    ha_push._client.call_service.assert_awaited_once()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue, reportFunctionMemberAccess, reportUnknownMemberType]


@pytest.mark.asyncio
async def test_dispatch_skip_appends_no_delivery_result() -> None:
    """Skipped channel produces no DeliveryResult (not a success, not a failure)."""
    log = structlog.get_logger()
    gated = _GatedRecordingChannel()
    dispatcher = AlertDispatcher(channels=[gated], log=log)

    event = _firing(labels={"severity": "info"})
    results = await dispatcher.dispatch(event)

    assert results == []
    assert len(gated.received) == 0


@pytest.mark.asyncio
async def test_dispatch_resolved_error_reaches_gated_channel() -> None:
    """Resolved events with error severity are NOT blocked by the gate."""
    log = structlog.get_logger()
    gated = _GatedRecordingChannel()
    dispatcher = AlertDispatcher(channels=[gated], log=log)

    event = _resolved(labels={"severity": "error"})
    results = await dispatcher.dispatch(event)

    assert len(gated.received) == 1
    assert results[0].ok is True


@pytest.mark.asyncio
async def test_dispatch_resolved_warning_skipped_by_gated_channel() -> None:
    """Resolved events with warning severity are blocked by the gate."""
    log = structlog.get_logger()
    gated = _GatedRecordingChannel()
    dispatcher = AlertDispatcher(channels=[gated], log=log)

    event = _resolved(labels={"severity": "warning"})
    results = await dispatcher.dispatch(event)

    assert len(gated.received) == 0
    assert results == []


@pytest.mark.asyncio
async def test_dispatch_default_accept_channel_receives_every_severity() -> None:
    """Regression: default-accept channel is unaffected by severity routing."""
    log = structlog.get_logger()
    dashboard = _RecordingChannel()
    dispatcher = AlertDispatcher(channels=[dashboard], log=log)

    for sev_label in ["info", "warning", "critical", "error", ""]:
        event = _firing(labels={"severity": sev_label})
        await dispatcher.dispatch(event)

    EXPECTED_COUNT = 5
    assert len(dashboard.received) == EXPECTED_COUNT
