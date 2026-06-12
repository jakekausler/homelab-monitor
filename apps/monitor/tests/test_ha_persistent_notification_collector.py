"""Tests for HaPersistentNotificationCollector — persistent-notification gauges.

Privacy tests are the defining constraint: notification body (title/message) must
NEVER appear in metric labels, result events, or log output.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter, MetricEntry
from homelab_monitor.kernel.plugins.types import SuggestionEvent
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_persistent_notification import (  # noqa: E501
    M_PERSISTENT_NOTIFICATION,
    HaPersistentNotificationCollector,
)

_DROP_METRIC = "homelab_metric_family_dropped_series"


class _FakeWs:
    """HA WS client double: configurable connected flag + send_command result."""

    def __init__(self, *, connected: bool, result: object) -> None:
        self.connected = connected
        self._result = result

    async def send_command(self, type_: str, **fields: object) -> object:
        del type_, fields
        return self._result


def _notification(
    notification_id: object,
    *,
    title: object = "Test Title",
    message: object = "Test message body",
) -> dict[str, object]:
    """Build a notification dict as HA's persistent_notification/get returns it."""
    return {
        "notification_id": notification_id,
        "title": title,
        "message": message,
    }


def _ctx(writer: InMemoryMetricsWriter) -> SimpleNamespace:
    """Build a partial CollectorContext (only vm + log are read by run())."""
    return SimpleNamespace(
        config=SimpleNamespace(),
        vm=writer,
        log=structlog.get_logger().bind(collector="ha_persistent_notification"),
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all recorded gauges with the given metric name."""
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]


def _collector_with_ws(ws: _FakeWs) -> HaPersistentNotificationCollector:
    """Construct the collector and inject a fake WS (lifespan precedent)."""
    collector = HaPersistentNotificationCollector()
    collector._ws = ws  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    return collector


# ---------------------------------------------------------------------------
# Log-capture helper (mirrors test_secrets_no_leak._LogCapture pattern)
# ---------------------------------------------------------------------------


class _LogCapture(logging.Handler):
    """Capture every log record's formatted message string."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


def _attach_root_capture() -> _LogCapture:
    """Attach a DEBUG-level capture handler to the root logger and return it."""
    handler = _LogCapture()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    return handler


def _detach_root_capture(handler: _LogCapture) -> None:
    logging.getLogger().removeHandler(handler)


# ---------------------------------------------------------------------------
# WS-state guards
# ---------------------------------------------------------------------------


async def test_ws_none_returns_failed_not_configured() -> None:
    """self._ws is None -> ok=False, errors=['ha websocket not configured']."""
    writer = InMemoryMetricsWriter()
    collector = HaPersistentNotificationCollector()  # no WS injected
    result = await collector.run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["ha websocket not configured"]
    assert writer.recorded == []


async def test_ws_not_connected_returns_failed_not_connected() -> None:
    """self._ws.connected is False -> ok=False, errors=['ha websocket not connected']."""
    ws = _FakeWs(connected=False, result=[])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["ha websocket not connected"]
    assert writer.recorded == []


async def test_send_command_haerror_returns_failed() -> None:
    """send_command returns HaError -> ok=False, errors=[message], nothing emitted."""
    ws = _FakeWs(
        connected=True,
        result=HaError(
            reason="timeout",
            message="command 'persistent_notification/get' timed out",
        ),
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["command 'persistent_notification/get' timed out"]
    assert writer.recorded == []


# ---------------------------------------------------------------------------
# Emission: active notifications (bare list)
# ---------------------------------------------------------------------------


async def test_notification_bare_list_emits_1_with_notification_id_label() -> None:
    """Bare list result -> 1.0 gauge with {notification_id} label."""
    ws = _FakeWs(
        connected=True,
        result=[_notification("persistent_notif_xyz")],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True

    gauges = _gauges(writer, M_PERSISTENT_NOTIFICATION)
    assert len(gauges) == 1
    assert gauges[0].value == 1.0
    assert gauges[0].labels == {"notification_id": "persistent_notif_xyz"}


async def test_multiple_notifications_each_emit_1() -> None:
    """Two notifications -> two 1.0 gauges."""
    ws = _FakeWs(
        connected=True,
        result=[
            _notification("notif_a"),
            _notification("notif_b"),
        ],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    gauges = _gauges(writer, M_PERSISTENT_NOTIFICATION)
    assert len(gauges) == 2  # noqa: PLR2004
    assert all(g.value == 1.0 for g in gauges)


# ---------------------------------------------------------------------------
# Parse shapes: bare list vs dict-wrapped
# ---------------------------------------------------------------------------


async def test_parse_dict_wrapped_notifications() -> None:
    """Result as {'notifications': [...]} parses the inner list correctly."""
    ws = _FakeWs(
        connected=True,
        result={"notifications": [_notification("dict_wrapped_id")]},
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert len(_gauges(writer, M_PERSISTENT_NOTIFICATION)) == 1


async def test_parse_empty_dict_emits_only_drop_gauge() -> None:
    """Dict with no 'notifications' key -> ok=True, no series, one drop gauge at 0.0."""
    ws = _FakeWs(connected=True, result={})
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_PERSISTENT_NOTIFICATION) == []
    drops = _gauges(writer, _DROP_METRIC)
    assert len(drops) == 1
    assert drops[0].value == 0.0


async def test_parse_dict_with_non_list_notifications_value() -> None:
    """Result dict whose 'notifications' value is not a list -> treated as empty."""
    ws = _FakeWs(connected=True, result={"notifications": "oops"})
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_PERSISTENT_NOTIFICATION) == []


# ---------------------------------------------------------------------------
# Empty list
# ---------------------------------------------------------------------------


async def test_empty_list_ok_only_drop_gauge() -> None:
    """Empty notifications list -> ok=True, one drop gauge at 0.0, no per-notification series."""
    ws = _FakeWs(connected=True, result=[])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_PERSISTENT_NOTIFICATION) == []
    drops = _gauges(writer, _DROP_METRIC)
    assert len(drops) == 1
    assert drops[0].labels["family"] == M_PERSISTENT_NOTIFICATION
    assert drops[0].value == 0.0


# ---------------------------------------------------------------------------
# Label skipping: missing/empty/non-str notification_id
# ---------------------------------------------------------------------------


async def test_missing_notification_id_skips_notification() -> None:
    """Notification with no 'notification_id' key is SKIPPED."""
    ws = _FakeWs(
        connected=True,
        result=[{"title": "orphan", "message": "body"}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_PERSISTENT_NOTIFICATION) == []


async def test_empty_notification_id_skips_notification() -> None:
    """Notification with notification_id='' is SKIPPED."""
    ws = _FakeWs(
        connected=True,
        result=[{"notification_id": "", "title": "t", "message": "m"}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_PERSISTENT_NOTIFICATION) == []


async def test_non_str_notification_id_skips_notification() -> None:
    """Notification with a non-str notification_id (int) is SKIPPED."""
    ws = _FakeWs(
        connected=True,
        result=[{"notification_id": 42, "title": "t", "message": "m"}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_PERSISTENT_NOTIFICATION) == []


# ---------------------------------------------------------------------------
# Non-dict notification elements
# ---------------------------------------------------------------------------


async def test_non_dict_notification_skipped() -> None:
    """A non-dict element in the notifications list is skipped gracefully."""
    ws = _FakeWs(
        connected=True,
        result=["not a dict", _notification("real_notif_id")],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    gauges = _gauges(writer, M_PERSISTENT_NOTIFICATION)
    assert len(gauges) == 1
    assert gauges[0].labels["notification_id"] == "real_notif_id"


# ---------------------------------------------------------------------------
# Privacy tests (THE defining constraint)
# ---------------------------------------------------------------------------

_BODY_SENTINEL = "SECRET-BODY-XYZ-7c3f8b9a"
_TITLE_SENTINEL = "SECRET-TITLE-4d2e1f8b"


def _private_notification() -> dict[str, object]:
    """A notification whose body fields must never appear in output."""
    return {
        "notification_id": "safe_id_12345",
        "title": _TITLE_SENTINEL,
        "message": _BODY_SENTINEL,
        "created_at": "2026-06-12T07:00:00+00:00",
    }


async def test_privacy_label_content_never_contains_body_or_title() -> None:
    """No emitted metric label VALUE or KEY may contain the body sentinel or title sentinel."""
    ws = _FakeWs(connected=True, result=[_private_notification()])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True

    for entry in writer.recorded:
        # No label KEY should be title/message/created_at
        for forbidden_key in ("title", "message", "created_at"):
            assert forbidden_key not in entry.labels, (
                f"Label key '{forbidden_key}' must never appear"
            )
        # No label VALUE should contain the sentinels
        for label_val in entry.labels.values():
            assert _BODY_SENTINEL not in label_val, (
                f"Body sentinel found in label value: {label_val!r}"
            )
            assert _TITLE_SENTINEL not in label_val, (
                f"Title sentinel found in label value: {label_val!r}"
            )

    # Positive assertion: the only label key on the notification gauge is notification_id
    notif_gauges = _gauges(writer, M_PERSISTENT_NOTIFICATION)
    assert len(notif_gauges) == 1
    assert set(notif_gauges[0].labels.keys()) == {"notification_id"}
    assert notif_gauges[0].labels["notification_id"] == "safe_id_12345"


async def test_privacy_no_event_body_text() -> None:
    """No CollectorEvent in result.events may contain body or title sentinel text."""
    ws = _FakeWs(connected=True, result=[_private_notification()])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True

    for event in result.events:
        # Serialize the event to a string representation for scanning
        event_text = repr(event)
        assert _BODY_SENTINEL not in event_text, (
            f"Body sentinel found in event repr: {event_text!r}"
        )
        assert _TITLE_SENTINEL not in event_text, (
            f"Title sentinel found in event repr: {event_text!r}"
        )


async def test_privacy_no_log_output_contains_body_or_title() -> None:
    """run() emits NO log records containing the body or title sentinel.

    Uses the _LogCapture / root-logger attachment pattern from
    test_secrets_no_leak.py to capture all stdlib + structlog output.
    """
    ws = _FakeWs(connected=True, result=[_private_notification()])
    writer = InMemoryMetricsWriter()

    capture = _attach_root_capture()
    try:
        await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    finally:
        _detach_root_capture(capture)

    all_log_text = "\n".join(capture.messages)
    assert _BODY_SENTINEL not in all_log_text, (
        "Body sentinel leaked into logs: found in captured log output"
    )
    assert _TITLE_SENTINEL not in all_log_text, (
        "Title sentinel leaked into logs: found in captured log output"
    )


# ---------------------------------------------------------------------------
# Cardinality cap
# ---------------------------------------------------------------------------


async def test_over_cap_drops_series_and_emits_one_suggestion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Over-cap notification family is dropped to the cap; exactly one warning suggestion."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(f"cardinality_caps:\n  families:\n    {M_PERSISTENT_NOTIFICATION}: 3\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))

    notifications = [_notification(f"notif_{i:05d}") for i in range(10)]
    ws = _FakeWs(connected=True, result=notifications)
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True

    notif_gauges = _gauges(writer, M_PERSISTENT_NOTIFICATION)
    assert len(notif_gauges) == 3  # noqa: PLR2004

    drop = [
        g
        for g in _gauges(writer, _DROP_METRIC)
        if g.labels.get("family") == M_PERSISTENT_NOTIFICATION
    ]
    assert len(drop) == 1
    assert drop[0].value == 7.0  # noqa: PLR2004

    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


# ---------------------------------------------------------------------------
# metrics_emitted accounting
# ---------------------------------------------------------------------------


async def test_metrics_emitted_counts_survivors_plus_drop_gauge() -> None:
    """metrics_emitted == len(survivors) + 1 (the single drop gauge)."""
    ws = _FakeWs(
        connected=True,
        result=[
            _notification("notif_a"),
            _notification("notif_b"),
        ],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    # 2 survivors + 1 drop gauge = 3
    assert result.metrics_emitted == 3  # noqa: PLR2004
