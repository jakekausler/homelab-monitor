"""Tests for HaConfigEntryCollector — per-config-entry loaded/setup_error gauges."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter, MetricEntry
from homelab_monitor.kernel.plugins.types import SuggestionEvent
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_config_entry import (
    M_CONFIG_ENTRY_LOADED,
    M_CONFIG_ENTRY_SETUP_ERROR,
    HaConfigEntryCollector,
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


def _entry(
    domain: object,
    title: object,
    state: object,
    reason: object = None,
) -> dict[str, object]:
    """Build a config-entry dict as HA's config_entries/get returns it."""
    entry: dict[str, object] = {"domain": domain, "title": title, "state": state}
    if reason is not None:
        entry["reason"] = reason
    return entry


def _ctx(writer: InMemoryMetricsWriter) -> SimpleNamespace:
    """Build a partial CollectorContext (only vm + log are read by run())."""
    return SimpleNamespace(
        config=SimpleNamespace(),
        vm=writer,
        log=structlog.get_logger().bind(collector="ha_config_entry"),
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all recorded gauges with the given metric name."""
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]


def _collector_with_ws(ws: _FakeWs) -> HaConfigEntryCollector:
    """Construct the collector and inject a fake WS (lifespan precedent)."""
    collector = HaConfigEntryCollector()
    collector._ws = ws  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    return collector


# --- state mapping: loaded / setup_error gauge values ---


async def test_loaded_state_emits_loaded_1_error_0() -> None:
    """state 'loaded' -> loaded gauge 1.0, setup_error gauge 0.0; {domain,title} labels."""
    ws = _FakeWs(
        connected=True,
        result=[_entry("hue", "Philips Hue", "loaded")],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True

    loaded = _gauges(writer, M_CONFIG_ENTRY_LOADED)
    error = _gauges(writer, M_CONFIG_ENTRY_SETUP_ERROR)
    assert len(loaded) == 1
    assert len(error) == 1
    assert loaded[0].value == 1.0
    assert loaded[0].labels == {"domain": "hue", "title": "Philips Hue"}
    assert error[0].value == 0.0
    assert error[0].labels == {"domain": "hue", "title": "Philips Hue"}


@pytest.mark.parametrize(
    "state",
    ["setup_error", "setup_retry", "migration_error", "failed_unload"],
)
async def test_error_states_emit_loaded_0_error_1(state: str) -> None:
    """Each error state -> loaded gauge 0.0, setup_error gauge 1.0."""
    ws = _FakeWs(connected=True, result=[_entry("zwave", "Z-Wave", state)])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True

    loaded = _gauges(writer, M_CONFIG_ENTRY_LOADED)
    error = _gauges(writer, M_CONFIG_ENTRY_SETUP_ERROR)
    assert loaded[0].value == 0.0
    assert error[0].value == 1.0


@pytest.mark.parametrize(
    "state",
    ["not_loaded", "setup_in_progress", "totally_unknown_state"],
)
async def test_neutral_states_emit_both_zero(state: str) -> None:
    """not_loaded / setup_in_progress / unknown -> both gauges 0.0 (still emitted)."""
    ws = _FakeWs(connected=True, result=[_entry("mqtt", "MQTT", state)])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True

    loaded = _gauges(writer, M_CONFIG_ENTRY_LOADED)
    error = _gauges(writer, M_CONFIG_ENTRY_SETUP_ERROR)
    assert loaded[0].value == 0.0
    assert error[0].value == 0.0


# --- label defaulting / skipping ---


async def test_missing_title_defaults_to_empty_string() -> None:
    """An entry with no 'title' key emits title=''."""
    ws = _FakeWs(connected=True, result=[{"domain": "hue", "state": "loaded"}])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    loaded = _gauges(writer, M_CONFIG_ENTRY_LOADED)
    assert loaded[0].labels == {"domain": "hue", "title": ""}


async def test_non_str_title_defaults_to_empty_string() -> None:
    """An entry with a non-str title (int) emits title=''."""
    ws = _FakeWs(connected=True, result=[_entry("hue", 123, "loaded")])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    loaded = _gauges(writer, M_CONFIG_ENTRY_LOADED)
    assert loaded[0].labels["title"] == ""


async def test_missing_domain_skips_entry() -> None:
    """An entry with no 'domain' is SKIPPED (no series emitted for it)."""
    ws = _FakeWs(connected=True, result=[{"title": "Orphan", "state": "loaded"}])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    # No per-entry series; only the two always-written drop gauges.
    assert _gauges(writer, M_CONFIG_ENTRY_LOADED) == []
    assert _gauges(writer, M_CONFIG_ENTRY_SETUP_ERROR) == []


async def test_empty_domain_skips_entry() -> None:
    """An entry with an empty-string 'domain' is SKIPPED."""
    ws = _FakeWs(connected=True, result=[_entry("", "Empty", "loaded")])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_CONFIG_ENTRY_LOADED) == []


async def test_non_dict_entry_skipped() -> None:
    """A non-dict element in the entries list is skipped gracefully (no crash)."""
    ws = _FakeWs(connected=True, result=["not a dict", _entry("hue", "Hue", "loaded")])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    loaded = _gauges(writer, M_CONFIG_ENTRY_LOADED)
    assert len(loaded) == 1
    assert loaded[0].labels["domain"] == "hue"


# --- WS-state guards ---


async def test_ws_none_returns_failed_not_configured() -> None:
    """self._ws is None -> ok=False, errors=['ha websocket not configured']."""
    writer = InMemoryMetricsWriter()
    collector = HaConfigEntryCollector()  # no WS injected
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
        result=HaError(reason="timeout", message="command 'config_entries/get' timed out"),
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["command 'config_entries/get' timed out"]
    assert writer.recorded == []


# --- defensive parse: list / dict-wrapped / malformed ---


async def test_parse_bare_list_result() -> None:
    """Result as a bare LIST (the real HA shape) parses correctly."""
    ws = _FakeWs(connected=True, result=[_entry("hue", "Hue", "loaded")])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert len(_gauges(writer, M_CONFIG_ENTRY_LOADED)) == 1


async def test_parse_dict_wrapped_list_under_entries_key() -> None:
    """Result as a dict wrapping the list under 'entries' parses correctly."""
    ws = _FakeWs(
        connected=True,
        result={"entries": [_entry("hue", "Hue", "loaded")]},
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert len(_gauges(writer, M_CONFIG_ENTRY_LOADED)) == 1


async def test_parse_dict_wrapped_list_under_config_entries_key() -> None:
    """Result as a dict wrapping the list under 'config_entries' parses correctly."""
    ws = _FakeWs(
        connected=True,
        result={"config_entries": [_entry("hue", "Hue", "loaded")]},
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert len(_gauges(writer, M_CONFIG_ENTRY_LOADED)) == 1


async def test_parse_malformed_dict_emits_only_drop_gauges() -> None:
    """Dict with no list (the {} degenerate case) -> ok=True, no series, drop gauges 0."""
    ws = _FakeWs(connected=True, result={})
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_CONFIG_ENTRY_LOADED) == []
    assert _gauges(writer, M_CONFIG_ENTRY_SETUP_ERROR) == []
    drops = _gauges(writer, _DROP_METRIC)
    assert len(drops) == 2  # noqa: PLR2004
    assert all(d.value == 0.0 for d in drops)


async def test_parse_dict_with_non_list_entries_value() -> None:
    """Result dict whose 'entries' value is not a list -> treated as empty (no crash)."""
    ws = _FakeWs(connected=True, result={"entries": "oops"})
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_CONFIG_ENTRY_LOADED) == []


# --- cardinality cap ---


async def test_over_cap_drops_series_and_emits_one_suggestion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Over-cap loaded family is dropped to the cap; exactly one warning suggestion."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(f"cardinality_caps:\n  families:\n    {M_CONFIG_ENTRY_LOADED}: 3\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))

    entries = [_entry(f"domain_{i:05d}", f"Entry {i}", "loaded") for i in range(10)]
    ws = _FakeWs(connected=True, result=entries)
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True

    loaded = _gauges(writer, M_CONFIG_ENTRY_LOADED)
    assert len(loaded) == 3  # noqa: PLR2004

    drop = [
        g for g in _gauges(writer, _DROP_METRIC) if g.labels.get("family") == M_CONFIG_ENTRY_LOADED
    ]
    assert len(drop) == 1
    assert drop[0].value == 7.0  # noqa: PLR2004

    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


# --- empty list ---


async def test_empty_list_ok_only_drop_gauges() -> None:
    """Empty entries list -> ok=True, two drop gauges at 0.0, no per-entry series."""
    ws = _FakeWs(connected=True, result=[])
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_CONFIG_ENTRY_LOADED) == []
    assert _gauges(writer, M_CONFIG_ENTRY_SETUP_ERROR) == []
    drops = _gauges(writer, _DROP_METRIC)
    assert len(drops) == 2  # noqa: PLR2004
    assert {d.labels["family"] for d in drops} == {
        M_CONFIG_ENTRY_LOADED,
        M_CONFIG_ENTRY_SETUP_ERROR,
    }
    assert all(d.value == 0.0 for d in drops)


# --- reason is panel-only, never a label ---


async def test_reason_never_emitted_as_label() -> None:
    """Even when an entry carries a 'reason', no gauge has a 'reason' label."""
    ws = _FakeWs(
        connected=True,
        result=[_entry("zwave", "Z-Wave", "setup_error", reason="usb dongle missing")],
    )
    writer = InMemoryMetricsWriter()
    result = await _collector_with_ws(ws).run(_ctx(writer))  # type: ignore[arg-type]
    assert result.ok is True
    for entry in writer.recorded:
        assert "reason" not in entry.labels
