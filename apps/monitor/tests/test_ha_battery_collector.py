"""Tests for HaBatteryCollector — per-entity battery-level gauges + shared helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

from homelab_monitor.kernel.ha.client import HaState
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter, MetricEntry
from homelab_monitor.kernel.plugins.types import SuggestionEvent
from homelab_monitor.plugins.collectors.integrations.homeassistant._shared import (
    extract_domain,
    parse_float_state,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_battery import (
    M_BATTERY_LEVEL,
    HaBatteryCollector,
)

_DROP_METRIC = "homelab_metric_family_dropped_series"


class _FakeHaStates:
    """HA client double whose get_states() returns a fixed list of HaState."""

    def __init__(self, states: list[HaState]) -> None:
        self._states = states

    async def get_states(self) -> list[HaState] | HaError:
        return self._states


class _FakeHaError:
    """HA client double whose get_states() returns an HaError."""

    async def get_states(self) -> list[HaState] | HaError:
        return HaError(reason="unreachable", message="get_states failed: down")


def _battery_state(entity_id: str, state: str, *, unit: str = "%") -> HaState:
    """Build a battery-classed HaState with the given numeric state and unit."""
    return HaState(
        entity_id=entity_id,
        state=state,
        attributes={"device_class": "battery", "unit_of_measurement": unit},
        last_changed="",
        last_updated="",
    )


def _plain_state(entity_id: str, state: str) -> HaState:
    """Build a non-battery HaState (no device_class/unit attributes)."""
    return HaState(
        entity_id=entity_id,
        state=state,
        attributes={},
        last_changed="",
        last_updated="",
    )


def _ctx(writer: InMemoryMetricsWriter, ha: object) -> SimpleNamespace:
    """Build a partial CollectorContext as a SimpleNamespace.

    Only the fields run() reads are populated: config, vm, ha, log. Passed to
    .run() with `# type: ignore[arg-type]` (SimpleNamespace is not a real
    CollectorContext).
    """
    return SimpleNamespace(
        config=SimpleNamespace(),
        vm=writer,
        ha=ha,
        log=structlog.get_logger().bind(collector="ha_battery"),
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all recorded gauges with the given metric name."""
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]


# --- shared-helper unit tests (cover _shared.py branches directly) ---


def test_extract_domain_simple_and_extra_dots() -> None:
    """extract_domain returns the text before the first dot (or the whole string)."""
    assert extract_domain("sensor.phone_battery") == "sensor"
    assert extract_domain("binary_sensor.x.y") == "binary_sensor"
    assert extract_domain("noseparator") == "noseparator"


def test_parse_float_state_numeric_sentinel_and_unparseable() -> None:
    """parse_float_state: numeric -> float; sentinels/unparseable -> None."""
    assert parse_float_state("42") == 42.0  # noqa: PLR2004
    assert parse_float_state("12.5") == 12.5  # noqa: PLR2004
    assert parse_float_state("unavailable") is None
    assert parse_float_state("unknown") is None
    assert parse_float_state("") is None
    assert parse_float_state("not-a-number") is None
    # Non-finite floats (nan, inf, -inf, Python spellings) -> None.
    assert parse_float_state("nan") is None
    assert parse_float_state("inf") is None
    assert parse_float_state("-inf") is None
    assert parse_float_state("Infinity") is None
    assert parse_float_state("-Infinity") is None


# --- collector behavior tests ---


async def test_only_battery_percent_entities_emit() -> None:
    """Only entities with device_class=battery AND unit=% emit; value + labels correct."""
    states = [
        _battery_state("sensor.phone", "87"),
        _plain_state("sensor.temperature", "21.5"),
        _plain_state("light.kitchen", "on"),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaBatteryCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    battery = _gauges(writer, M_BATTERY_LEVEL)
    assert len(battery) == 1
    assert battery[0].value == 87.0  # noqa: PLR2004
    assert battery[0].labels == {"entity_id": "sensor.phone", "domain": "sensor"}


async def test_non_numeric_battery_state_skipped() -> None:
    """unavailable/unknown/non-numeric battery states are skipped (no series, not 0)."""
    states = [
        _battery_state("sensor.a", "unavailable"),
        _battery_state("sensor.b", "unknown"),
        _battery_state("sensor.c", "bogus"),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaBatteryCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_BATTERY_LEVEL) == []


async def test_battery_class_wrong_unit_skipped() -> None:
    """A battery-classed entity with a non-% unit (e.g. voltage V) is skipped."""
    states = [_battery_state("sensor.cell_voltage", "3", unit="V")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaBatteryCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_BATTERY_LEVEL) == []


async def test_ha_error_returns_failed_result() -> None:
    """HaError from get_states results in a failed run with the error message."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaError())

    result = await HaBatteryCollector().run(ctx)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["get_states failed: down"]
    assert writer.recorded == []


async def test_ha_none_returns_failed_result() -> None:
    """ctx.ha is None results in a failed run with 'ha client not configured'."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, None)

    result = await HaBatteryCollector().run(ctx)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["ha client not configured"]
    assert writer.recorded == []


async def test_over_cap_drops_series_and_emits_one_suggestion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Over-cap battery entities are dropped to the cap; one warning suggestion emitted."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(f"cardinality_caps:\n  families:\n    {M_BATTERY_LEVEL}: 5\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))

    states = [_battery_state(f"sensor.e{i:05d}", "50") for i in range(10)]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaBatteryCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    battery = _gauges(writer, M_BATTERY_LEVEL)
    assert len(battery) == 5  # noqa: PLR2004

    drop = [g for g in _gauges(writer, _DROP_METRIC) if g.labels.get("family") == M_BATTERY_LEVEL]
    assert len(drop) == 1
    assert drop[0].value == 5.0  # noqa: PLR2004

    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


async def test_empty_states_list_ok_only_drop_gauge() -> None:
    """Empty states -> ok=True, only the drop gauge (value 0.0), no battery series."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates([]))

    result = await HaBatteryCollector().run(ctx)  # type: ignore[arg-type]

    assert result.ok is True
    assert _gauges(writer, M_BATTERY_LEVEL) == []

    drop = _gauges(writer, _DROP_METRIC)
    assert len(drop) == 1
    assert drop[0].value == 0.0
    assert drop[0].labels == {"family": M_BATTERY_LEVEL}
