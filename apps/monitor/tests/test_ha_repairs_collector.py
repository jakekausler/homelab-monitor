"""Tests for HaRepairsCollector — active repair-issue gauges."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter, MetricEntry
from homelab_monitor.kernel.plugins.types import SuggestionEvent
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_repairs import (
    M_REPAIR_ISSUE,
    HaRepairsCollector,
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


def _issue(
    domain: object,
    issue_id: object,
    *,
    active: object = True,
    ignored: object = False,
    severity: object = "warning",
) -> dict[str, object]:
    """Build a repair-issue dict as HA's repairs/list_issues returns it."""
    return {
        "domain": domain,
        "issue_id": issue_id,
        "active": active,
        "ignored": ignored,
        "severity": severity,
    }


def _ctx(writer: InMemoryMetricsWriter) -> SimpleNamespace:
    """Build a partial CollectorContext (only vm + log are read by run())."""
    return SimpleNamespace(
        config=SimpleNamespace(),
        vm=writer,
        log=structlog.get_logger().bind(collector="ha_repairs"),
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all recorded gauges with the given metric name."""
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]


def _collector_with_ws(ws: _FakeWs) -> HaRepairsCollector:
    """Construct the collector and inject a fake WS (lifespan precedent)."""
    collector = HaRepairsCollector()
    collector._ws = ws  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    return collector


# --- WS-state guards ---


async def test_ws_none_returns_failed_not_configured() -> None:
    """self._ws is None -> ok=False, errors=['ha websocket not configured']."""
    writer = InMemoryMetricsWriter()
    collector = HaRepairsCollector()  # no WS injected
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
        result=HaError(reason="timeout", message="command 'repairs/list_issues' timed out"),
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["command 'repairs/list_issues' timed out"]
    assert writer.recorded == []


# --- emission: active non-ignored issues ---


async def test_active_non_ignored_issue_emits_1_with_labels() -> None:
    """Active, non-ignored issue -> 1.0 gauge with {domain,issue_id,severity} labels."""
    ws = _FakeWs(
        connected=True,
        result=[_issue("homeassistant", "check_usb_100")],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True

    gauges = _gauges(writer, M_REPAIR_ISSUE)
    assert len(gauges) == 1
    assert gauges[0].value == 1.0
    assert gauges[0].labels == {
        "domain": "homeassistant",
        "issue_id": "check_usb_100",
        "severity": "warning",
    }


async def test_multiple_active_issues_each_emit_1() -> None:
    """Two active issues -> two 1.0 gauges."""
    ws = _FakeWs(
        connected=True,
        result=[
            _issue("homeassistant", "issue_a", severity="error"),
            _issue("zwave_js", "issue_b", severity="critical"),
        ],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    gauges = _gauges(writer, M_REPAIR_ISSUE)
    assert len(gauges) == 2  # noqa: PLR2004
    assert all(g.value == 1.0 for g in gauges)


# --- parse shapes: bare list vs dict-wrapped ---


async def test_parse_dict_wrapped_issues() -> None:
    """Result as {'issues': [...]} parses the inner list correctly."""
    ws = _FakeWs(
        connected=True,
        result={"issues": [_issue("hue", "hue_issue_1")]},
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert len(_gauges(writer, M_REPAIR_ISSUE)) == 1


async def test_parse_empty_dict_emits_only_drop_gauge() -> None:
    """Dict with no 'issues' key -> ok=True, no series, one drop gauge at 0.0."""
    ws = _FakeWs(connected=True, result={})
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_REPAIR_ISSUE) == []
    drops = _gauges(writer, _DROP_METRIC)
    assert len(drops) == 1
    assert drops[0].value == 0.0


async def test_parse_dict_with_non_list_issues_value() -> None:
    """Result dict whose 'issues' value is not a list -> treated as empty."""
    ws = _FakeWs(connected=True, result={"issues": "oops"})
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_REPAIR_ISSUE) == []


# --- empty list ---


async def test_empty_list_ok_only_drop_gauge() -> None:
    """Empty issues list -> ok=True, one drop gauge at 0.0, no per-issue series."""
    ws = _FakeWs(connected=True, result=[])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_REPAIR_ISSUE) == []
    drops = _gauges(writer, _DROP_METRIC)
    assert len(drops) == 1
    assert drops[0].labels["family"] == M_REPAIR_ISSUE
    assert drops[0].value == 0.0


# --- filtering: ignored / inactive ---


async def test_ignored_true_issue_excluded() -> None:
    """ignored=True -> issue NOT emitted."""
    ws = _FakeWs(
        connected=True,
        result=[_issue("homeassistant", "ignored_issue", ignored=True)],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_REPAIR_ISSUE) == []


async def test_active_false_issue_excluded() -> None:
    """active=False -> issue NOT emitted."""
    ws = _FakeWs(
        connected=True,
        result=[_issue("homeassistant", "inactive_issue", active=False)],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_REPAIR_ISSUE) == []


async def test_missing_active_field_treated_as_active() -> None:
    """Missing 'active' key -> treated as active -> emitted."""
    ws = _FakeWs(
        connected=True,
        result=[{"domain": "mqtt", "issue_id": "mqtt_check", "severity": "warning"}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert len(_gauges(writer, M_REPAIR_ISSUE)) == 1


async def test_missing_ignored_field_treated_as_not_ignored() -> None:
    """Missing 'ignored' key -> treated as not-ignored -> emitted."""
    ws = _FakeWs(
        connected=True,
        result=[{"domain": "mqtt", "issue_id": "mqtt_check2", "active": True, "severity": "error"}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert len(_gauges(writer, M_REPAIR_ISSUE)) == 1


# --- label skipping: missing/empty/non-str domain or issue_id ---


async def test_missing_domain_skips_issue() -> None:
    """Issue with no 'domain' key is SKIPPED."""
    ws = _FakeWs(
        connected=True,
        result=[{"issue_id": "orphan", "active": True, "severity": "warning"}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_REPAIR_ISSUE) == []


async def test_empty_domain_skips_issue() -> None:
    """Issue with domain='' is SKIPPED."""
    ws = _FakeWs(
        connected=True,
        result=[{"domain": "", "issue_id": "test_id", "active": True, "severity": "warning"}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_REPAIR_ISSUE) == []


async def test_non_str_domain_skips_issue() -> None:
    """Issue with a non-str domain (int) is SKIPPED."""
    ws = _FakeWs(
        connected=True,
        result=[{"domain": 42, "issue_id": "test_id", "active": True, "severity": "warning"}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_REPAIR_ISSUE) == []


async def test_missing_issue_id_skips_issue() -> None:
    """Issue with no 'issue_id' key is SKIPPED."""
    ws = _FakeWs(
        connected=True,
        result=[{"domain": "homeassistant", "active": True, "severity": "warning"}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_REPAIR_ISSUE) == []


async def test_empty_issue_id_skips_issue() -> None:
    """Issue with issue_id='' is SKIPPED."""
    ws = _FakeWs(
        connected=True,
        result=[{"domain": "homeassistant", "issue_id": "", "active": True, "severity": "warning"}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_REPAIR_ISSUE) == []


async def test_non_str_issue_id_skips_issue() -> None:
    """Issue with a non-str issue_id (int) is SKIPPED."""
    ws = _FakeWs(
        connected=True,
        result=[{"domain": "homeassistant", "issue_id": 99, "active": True, "severity": "warning"}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_REPAIR_ISSUE) == []


# --- severity defaulting ---


async def test_missing_severity_defaults_to_unknown() -> None:
    """Missing 'severity' key -> severity label is 'unknown'."""
    ws = _FakeWs(
        connected=True,
        result=[{"domain": "homeassistant", "issue_id": "check_x", "active": True}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    gauges = _gauges(writer, M_REPAIR_ISSUE)
    assert len(gauges) == 1
    assert gauges[0].labels["severity"] == "unknown"


async def test_non_str_severity_defaults_to_unknown() -> None:
    """Non-str severity (int) -> severity label is 'unknown'."""
    ws = _FakeWs(
        connected=True,
        result=[{"domain": "homeassistant", "issue_id": "check_y", "active": True, "severity": 3}],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    gauges = _gauges(writer, M_REPAIR_ISSUE)
    assert len(gauges) == 1
    assert gauges[0].labels["severity"] == "unknown"


# --- free-text fields are never labels ---


async def test_free_text_fields_never_emitted_as_labels() -> None:
    """translation_key, description, learn_more_url, is_fixable are NOT label keys."""
    issue_dict: dict[str, object] = {
        "domain": "homeassistant",
        "issue_id": "check_z",
        "active": True,
        "ignored": False,
        "severity": "warning",
        "translation_key": "some_translation",
        "translation_placeholders": {"name": "foo"},
        "description": "Fix this thing",
        "learn_more_url": "https://example.com",
        "breaks_in_ha_version": "2026.1.0",
        "is_fixable": True,
    }
    ws = _FakeWs(connected=True, result=[issue_dict])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    for entry in writer.recorded:
        for forbidden in (
            "translation_key",
            "translation_placeholders",
            "description",
            "learn_more_url",
            "breaks_in_ha_version",
            "is_fixable",
        ):
            assert forbidden not in entry.labels


# --- non-dict issues ---


async def test_non_dict_issue_skipped() -> None:
    """A non-dict element in the issues list is skipped gracefully."""
    ws = _FakeWs(
        connected=True,
        result=["not a dict", _issue("homeassistant", "real_issue")],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    gauges = _gauges(writer, M_REPAIR_ISSUE)
    assert len(gauges) == 1
    assert gauges[0].labels["domain"] == "homeassistant"


# --- cardinality cap ---


async def test_over_cap_drops_series_and_emits_one_suggestion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Over-cap issue family is dropped to the cap; exactly one warning suggestion."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(f"cardinality_caps:\n  families:\n    {M_REPAIR_ISSUE}: 3\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))

    issues = [_issue("homeassistant", f"issue_{i:05d}") for i in range(10)]
    ws = _FakeWs(connected=True, result=issues)
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True

    issue_gauges = _gauges(writer, M_REPAIR_ISSUE)
    assert len(issue_gauges) == 3  # noqa: PLR2004

    drop = [g for g in _gauges(writer, _DROP_METRIC) if g.labels.get("family") == M_REPAIR_ISSUE]
    assert len(drop) == 1
    assert drop[0].value == 7.0  # noqa: PLR2004

    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


# --- metrics_emitted accounting ---


async def test_metrics_emitted_counts_survivors_plus_drop_gauge() -> None:
    """metrics_emitted == len(survivors) + 1 (the single drop gauge)."""
    ws = _FakeWs(
        connected=True,
        result=[
            _issue("homeassistant", "issue_a"),
            _issue("zwave_js", "issue_b"),
        ],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    # 2 survivors + 1 drop gauge = 3
    assert result.metrics_emitted == 3  # noqa: PLR2004
