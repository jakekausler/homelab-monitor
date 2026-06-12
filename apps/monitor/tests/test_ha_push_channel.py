"""Tests for kernel.dispatch.channels.ha_push.HAPushChannel (STAGE-005-017).

Covers the firing/resolved payload shapes, the deep-link branches (explorer /
default / no-public-url), the empty-annotation + missing-alertname fallbacks,
the empty-notify-service no-op, and the HaError -> token-safe RuntimeError path.
Tests assert public behavior (the call_service payload + raised message); no
private symbols are imported.
"""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.alerts.events import AlertFiringEvent, AlertResolvedEvent
from homelab_monitor.kernel.alerts.types import Severity
from homelab_monitor.kernel.dispatch.channels.ha_push import HAPushChannel
from homelab_monitor.kernel.ha.client import HaServiceResult
from homelab_monitor.kernel.ha.errors import HaError

_TOKEN_SENTINEL = "super-secret-ha-token-xyz"
_HTTP_SERVER_ERROR = 500


class _FakeHaClient:
    """Records call_service args and returns a preconfigured result."""

    def __init__(self, result: HaServiceResult | HaError) -> None:
        self._result = result
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> HaServiceResult | HaError:
        self.calls.append((domain, service, data if data is not None else {}))
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
) -> AlertResolvedEvent:
    return AlertResolvedEvent(
        alert_id="aid-1",
        fingerprint=fingerprint,
        source_tool="alertmanager",
        severity=severity,
        resolved_at="2026-05-07T00:01:00+00:00",
        labels={"alertname": "Foo"} if labels is None else labels,
        annotations={} if annotations is None else annotations,
        ts="2026-05-07T00:01:00+00:00",
    )


def _ok() -> HaServiceResult:
    return HaServiceResult(changed_states=[])


# ---- firing: full annotations + explorer + public_url ----


@pytest.mark.asyncio
async def test_firing_with_explorer_and_public_url() -> None:
    client = _FakeHaClient(_ok())
    channel = HAPushChannel(
        client,  # pyright: ignore[reportArgumentType]
        notify_service="mobile_app_pixel",
        public_url_provider=lambda: "http://mon.local/",
    )
    event = _firing(
        labels={"alertname": "DiskFull", "integration": "docker"},
        annotations={
            "summary": "Disk is full",
            "description": "root fs at 98%",
            "explorer": "/explore/disk",
        },
        severity=Severity.CRITICAL,
        fingerprint="fp-disk",
    )

    await channel.deliver(event)

    assert len(client.calls) == 1
    domain, service, payload = client.calls[0]
    assert domain == "notify"
    assert service == "mobile_app_pixel"
    assert payload["title"] == "[CRITICAL] Disk is full"
    assert payload["message"] == "root fs at 98%"
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["tag"] == "fp-disk"
    assert data["severity"] == "critical"
    assert data["group"] == "docker"
    # rstrip("/") on public_url, then + explorer (no double slash).
    assert data["url"] == "http://mon.local/explore/disk"


# ---- firing: no explorer, public_url set -> default deep link ----


@pytest.mark.asyncio
async def test_firing_no_explorer_uses_default_link() -> None:
    client = _FakeHaClient(_ok())
    channel = HAPushChannel(
        client,  # pyright: ignore[reportArgumentType]
        notify_service="svc",
        public_url_provider=lambda: "http://mon.local",
    )
    event = _firing(
        labels={"alertname": "Foo"},
        annotations={"summary": "S", "description": "D"},
    )

    await channel.deliver(event)

    _, _, payload = client.calls[0]
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["url"] == "http://mon.local/alerts/active"
    # group falls back to source_tool when no integration label.
    assert data["group"] == "alertmanager"


# ---- firing: public_url None -> no url key ----


@pytest.mark.asyncio
async def test_firing_no_public_url_omits_url() -> None:
    client = _FakeHaClient(_ok())
    channel = HAPushChannel(
        client,  # pyright: ignore[reportArgumentType]
        notify_service="svc",
        public_url_provider=lambda: None,
    )
    event = _firing(annotations={"summary": "S", "description": "D"})

    await channel.deliver(event)

    _, _, payload = client.calls[0]
    data = payload["data"]
    assert isinstance(data, dict)
    assert "url" not in data


# ---- firing: empty annotations -> alertname fallback for summary/desc ----


@pytest.mark.asyncio
async def test_firing_empty_annotations_falls_back_to_alertname() -> None:
    client = _FakeHaClient(_ok())
    channel = HAPushChannel(
        client,  # pyright: ignore[reportArgumentType]
        notify_service="svc",
        public_url_provider=lambda: None,
    )
    event = _firing(labels={"alertname": "MyAlert"}, annotations={})

    await channel.deliver(event)

    _, _, payload = client.calls[0]
    assert payload["title"] == "[WARNING] MyAlert"
    assert payload["message"] == "MyAlert"


# ---- firing: no alertname label -> "alert" fallback ----


@pytest.mark.asyncio
async def test_firing_no_alertname_uses_alert_fallback() -> None:
    client = _FakeHaClient(_ok())
    channel = HAPushChannel(
        client,  # pyright: ignore[reportArgumentType]
        notify_service="svc",
        public_url_provider=lambda: None,
    )
    event = _firing(labels={}, annotations={})

    await channel.deliver(event)

    _, _, payload = client.calls[0]
    assert payload["title"] == "[WARNING] alert"
    assert payload["message"] == "alert"


# ---- resolved event ----


@pytest.mark.asyncio
async def test_resolved_event_payload() -> None:
    client = _FakeHaClient(_ok())
    channel = HAPushChannel(
        client,  # pyright: ignore[reportArgumentType]
        notify_service="svc",
        public_url_provider=lambda: "http://mon.local",
    )
    event = _resolved(
        labels={"alertname": "Foo", "integration": "docker"},
        annotations={"summary": "Disk full", "explorer": "/x"},
        fingerprint="fp-r",
    )

    await channel.deliver(event)

    _, _, payload = client.calls[0]
    assert payload["title"] == "[RESOLVED] Disk full"
    assert payload["message"] == "Disk full resolved"
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["tag"] == "fp-r"
    assert data["severity"] == "warning"
    # Resolved data is tag+severity ONLY (no group, no url).
    assert "group" not in data
    assert "url" not in data


@pytest.mark.asyncio
async def test_resolved_empty_annotations_falls_back_to_alertname() -> None:
    client = _FakeHaClient(_ok())
    channel = HAPushChannel(
        client,  # pyright: ignore[reportArgumentType]
        notify_service="svc",
        public_url_provider=lambda: None,
    )
    event = _resolved(labels={"alertname": "MyAlert"}, annotations={})

    await channel.deliver(event)

    _, _, payload = client.calls[0]
    assert payload["title"] == "[RESOLVED] MyAlert"
    assert payload["message"] == "MyAlert resolved"


# ---- empty notify_service -> no-op ----


@pytest.mark.asyncio
async def test_empty_notify_service_is_noop() -> None:
    client = _FakeHaClient(_ok())
    channel = HAPushChannel(
        client,  # pyright: ignore[reportArgumentType]
        notify_service="",
        public_url_provider=lambda: "http://mon.local",
    )

    await channel.deliver(_firing())

    assert client.calls == []  # no HA call made


# ---- HaError -> token-safe RuntimeError ----


@pytest.mark.asyncio
async def test_haerror_raises_token_safe_runtime_error() -> None:
    err = HaError(reason="http_error", message="POST /api/...", status=_HTTP_SERVER_ERROR)
    client = _FakeHaClient(err)
    channel = HAPushChannel(
        client,  # pyright: ignore[reportArgumentType]
        notify_service="svc",
        public_url_provider=lambda: None,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await channel.deliver(_firing(annotations={"summary": "S", "description": "D"}))

    raised = str(exc_info.value)
    assert "ha_push delivery failed" in raised
    assert "http_error" in raised
    assert str(_HTTP_SERVER_ERROR) in raised
    # The raised message is built from reason+status only — never a token.
    assert _TOKEN_SENTINEL not in raised


# ---- HaServiceResult -> success (returns None) ----


@pytest.mark.asyncio
async def test_haserviceresult_returns_none() -> None:
    client = _FakeHaClient(_ok())
    channel = HAPushChannel(
        client,  # pyright: ignore[reportArgumentType]
        notify_service="svc",
        public_url_provider=lambda: None,
    )

    result = await channel.deliver(_firing(annotations={"summary": "S", "description": "D"}))

    assert result is None
    assert len(client.calls) == 1
