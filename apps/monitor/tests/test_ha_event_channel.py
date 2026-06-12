"""Tests for HAEventChannel (STAGE-005-020)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.alerts.events import AlertFiringEvent, AlertResolvedEvent
from homelab_monitor.kernel.alerts.types import Severity
from homelab_monitor.kernel.dispatch.channels.ha_event import HAEventChannel
from homelab_monitor.kernel.ha.errors import HaError

_TOKEN_SENTINEL = "super-secret-ha-token-xyz"
_HTTP_SERVER_ERROR = 500


class _FakeHaClient:
    """Records fire_event args and returns a preconfigured result."""

    def __init__(self, result: None | HaError) -> None:
        self._result = result
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def fire_event(self, event_type: str, data: dict[str, str]) -> None | HaError:
        self.calls.append((event_type, data))
        return self._result


def _firing(
    *,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    severity: Severity = Severity.WARNING,
    fingerprint: str = "fp-1",
    source_tool: str = "alertmanager",
) -> AlertFiringEvent:
    return AlertFiringEvent(
        alert_id="aid-1",
        fingerprint=fingerprint,
        source_tool=source_tool,
        severity=severity,
        opened_at="2026-05-07T00:00:00+00:00",
        last_seen_at="2026-05-07T00:00:00+00:00",
        labels={"alertname": "Foo"} if labels is None else labels,
        annotations={} if annotations is None else annotations,
        ts="2026-05-07T00:00:00+00:00",
    )


def _resolved(
    *,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    severity: Severity = Severity.WARNING,
    fingerprint: str = "fp-1",
    source_tool: str = "alertmanager",
) -> AlertResolvedEvent:
    return AlertResolvedEvent(
        alert_id="aid-1",
        fingerprint=fingerprint,
        source_tool=source_tool,
        severity=severity,
        resolved_at="2026-05-07T00:01:00+00:00",
        labels={"alertname": "Foo"} if labels is None else labels,
        annotations={} if annotations is None else annotations,
        ts="2026-05-07T00:01:00+00:00",
    )


# --- accepts -------------------------------------------------------------


def test_accepts_false_when_event_type_empty_even_with_label() -> None:
    client = _FakeHaClient(None)
    channel = HAEventChannel(
        client,  # pyright: ignore[reportArgumentType]
        event_type="",
        public_url_provider=lambda: None,
    )
    event = _firing(labels={"alertname": "Foo", "push_to_ha": "true"})
    assert channel.accepts(event) is False


def test_accepts_true_when_enabled_and_opted_in() -> None:
    client = _FakeHaClient(None)
    channel = HAEventChannel(
        client,  # pyright: ignore[reportArgumentType]
        event_type="homelab_monitor_alert",
        public_url_provider=lambda: None,
    )
    event = _firing(labels={"alertname": "Foo", "push_to_ha": "true"})
    assert channel.accepts(event) is True


def test_accepts_false_when_label_absent() -> None:
    client = _FakeHaClient(None)
    channel = HAEventChannel(
        client,  # pyright: ignore[reportArgumentType]
        event_type="homelab_monitor_alert",
        public_url_provider=lambda: None,
    )
    event = _firing(labels={"alertname": "Foo"})
    assert channel.accepts(event) is False


def test_accepts_false_when_label_false() -> None:
    client = _FakeHaClient(None)
    channel = HAEventChannel(
        client,  # pyright: ignore[reportArgumentType]
        event_type="homelab_monitor_alert",
        public_url_provider=lambda: None,
    )
    event = _firing(labels={"alertname": "Foo", "push_to_ha": "false"})
    assert channel.accepts(event) is False


# --- deliver -------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_firing_payload_with_url() -> None:
    client = _FakeHaClient(None)
    channel = HAEventChannel(
        client,  # pyright: ignore[reportArgumentType]
        event_type="homelab_monitor_alert",
        public_url_provider=lambda: "http://mon.local/",
    )
    event = _firing(
        labels={"alertname": "DiskFull", "push_to_ha": "true"},
        annotations={"summary": "Disk is full"},
        severity=Severity.CRITICAL,
        fingerprint="fp-disk",
        source_tool="alertmanager",
    )

    await channel.deliver(event)

    assert len(client.calls) == 1
    event_type, data = client.calls[0]
    assert event_type == "homelab_monitor_alert"
    assert data["status"] == "firing"
    assert data["fingerprint"] == "fp-disk"
    assert data["alertname"] == "DiskFull"
    assert data["severity"] == "critical"
    assert data["summary"] == "Disk is full"
    assert data["source_tool"] == "alertmanager"
    # rstrip("/") on public_url, then flat dashboard link.
    assert data["url"] == "http://mon.local/alerts/active"


@pytest.mark.asyncio
async def test_deliver_resolved_sets_status_resolved() -> None:
    client = _FakeHaClient(None)
    channel = HAEventChannel(
        client,  # pyright: ignore[reportArgumentType]
        event_type="homelab_monitor_alert",
        public_url_provider=lambda: "http://mon.local",
    )
    event = _resolved(
        labels={"alertname": "DiskFull", "push_to_ha": "true"},
        annotations={"summary": "Disk full"},
        fingerprint="fp-r",
    )

    await channel.deliver(event)

    _, data = client.calls[0]
    assert data["status"] == "resolved"
    assert data["fingerprint"] == "fp-r"
    assert data["url"] == "http://mon.local/alerts/active"


@pytest.mark.asyncio
async def test_deliver_omits_url_when_public_url_none() -> None:
    client = _FakeHaClient(None)
    channel = HAEventChannel(
        client,  # pyright: ignore[reportArgumentType]
        event_type="homelab_monitor_alert",
        public_url_provider=lambda: None,
    )
    event = _firing(
        labels={"alertname": "Foo", "push_to_ha": "true"},
        annotations={"summary": "S"},
    )

    await channel.deliver(event)

    _, data = client.calls[0]
    assert "url" not in data


@pytest.mark.asyncio
async def test_deliver_missing_annotations_and_alertname_use_empty_strings() -> None:
    client = _FakeHaClient(None)
    channel = HAEventChannel(
        client,  # pyright: ignore[reportArgumentType]
        event_type="homelab_monitor_alert",
        public_url_provider=lambda: None,
    )
    event = _firing(labels={}, annotations={})

    await channel.deliver(event)

    _, data = client.calls[0]
    assert data["alertname"] == ""
    assert data["summary"] == ""


@pytest.mark.asyncio
async def test_deliver_haerror_raises_token_safe_runtime_error() -> None:
    err = HaError(reason="http_error", message="POST /api/...", status=_HTTP_SERVER_ERROR)
    client = _FakeHaClient(err)
    channel = HAEventChannel(
        client,  # pyright: ignore[reportArgumentType]
        event_type="homelab_monitor_alert",
        public_url_provider=lambda: None,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await channel.deliver(_firing(labels={"push_to_ha": "true"}, annotations={"summary": "S"}))

    raised = str(exc_info.value)
    assert "ha_event delivery failed" in raised
    assert "http_error" in raised
    assert str(_HTTP_SERVER_ERROR) in raised
    # Message is built from reason+status only — never a token.
    assert _TOKEN_SENTINEL not in raised
