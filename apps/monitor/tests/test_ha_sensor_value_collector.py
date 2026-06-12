"""Tests for HaSensorValueCollector — temp/humidity raw-value gauges."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

from homelab_monitor.kernel.ha.client import HaState
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter, MetricEntry
from homelab_monitor.kernel.plugins.types import SuggestionEvent
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_sensor_value import (
    M_SENSOR_VALUE,
    HaSensorValueCollector,
)

_DROP_METRIC = "homelab_metric_family_dropped_series"


class _FakeHaStates:
    def __init__(self, states: list[HaState]) -> None:
        self._states = states

    async def get_states(self) -> list[HaState] | HaError:
        return self._states


class _FakeHaError:
    async def get_states(self) -> list[HaState] | HaError:
        return HaError(reason="unreachable", message="get_states failed: down")


def _sensor_state(
    entity_id: str, state: str, *, device_class: str | None = "temperature"
) -> HaState:
    """Build a sensor HaState with the given numeric state and device_class."""
    attributes: dict[str, object] = {}
    if device_class is not None:
        attributes["device_class"] = device_class
    return HaState(
        entity_id=entity_id,
        state=state,
        attributes=attributes,
        last_changed="",
        last_updated="",
    )


def _ctx(writer: InMemoryMetricsWriter, ha: object) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(),
        vm=writer,
        ha=ha,
        log=structlog.get_logger().bind(collector="ha_sensor_value"),
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]


async def test_valid_temperature_emits_value() -> None:
    """A temperature sensor emits its raw float with {entity_id, device_class}."""
    states = [_sensor_state("sensor.freezer_temp", "-18.5", device_class="temperature")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSensorValueCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    series = _gauges(writer, M_SENSOR_VALUE)
    assert len(series) == 1
    assert series[0].value == -18.5  # noqa: PLR2004
    assert series[0].labels == {
        "entity_id": "sensor.freezer_temp",
        "device_class": "temperature",
    }


async def test_valid_humidity_emits_value() -> None:
    """A humidity sensor emits its raw float."""
    states = [_sensor_state("sensor.indoor_humidity", "55", device_class="humidity")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSensorValueCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    series = _gauges(writer, M_SENSOR_VALUE)
    assert len(series) == 1
    assert series[0].value == 55.0  # noqa: PLR2004
    assert series[0].labels["device_class"] == "humidity"


async def test_non_finite_skipped() -> None:
    """Non-finite / non-numeric / unavailable states are skipped (parse_float_state)."""
    states = [
        _sensor_state("sensor.a", "nan", device_class="temperature"),
        _sensor_state("sensor.b", "inf", device_class="temperature"),
        _sensor_state("sensor.c", "unavailable", device_class="humidity"),
        _sensor_state("sensor.d", "bogus", device_class="temperature"),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSensorValueCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_SENSOR_VALUE) == []


async def test_non_allowed_device_class_skipped() -> None:
    """A sensor whose device_class is not in the allow-set is skipped."""
    states = [_sensor_state("sensor.power_meter", "120.5", device_class="power")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSensorValueCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_SENSOR_VALUE) == []


async def test_missing_device_class_skipped() -> None:
    """A sensor with no device_class attribute is skipped."""
    states = [_sensor_state("sensor.bare", "21.0", device_class=None)]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSensorValueCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_SENSOR_VALUE) == []


async def test_device_class_allow_set_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env override changes which device_class is emitted."""
    monkeypatch.setenv("HOMELAB_MONITOR_HA_SENSOR_VALUE_DEVICE_CLASSES", "pressure")
    states = [
        _sensor_state("sensor.barometer", "1013", device_class="pressure"),
        _sensor_state("sensor.indoor_temp", "21", device_class="temperature"),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSensorValueCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    series = _gauges(writer, M_SENSOR_VALUE)
    assert len(series) == 1
    assert series[0].labels["entity_id"] == "sensor.barometer"


async def test_empty_override_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty override env var falls back to the default temp/humidity set."""
    monkeypatch.setenv("HOMELAB_MONITOR_HA_SENSOR_VALUE_DEVICE_CLASSES", " , ,")
    states = [_sensor_state("sensor.indoor_temp", "21", device_class="temperature")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSensorValueCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert len(_gauges(writer, M_SENSOR_VALUE)) == 1


async def test_ha_error_returns_failed_result() -> None:
    """HaError from get_states -> failed run, no writes."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaError())

    result = await HaSensorValueCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["get_states failed: down"]
    assert writer.recorded == []


async def test_ha_none_returns_failed_result() -> None:
    """ctx.ha is None -> failed run, no writes."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, None)

    result = await HaSensorValueCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["ha client not configured"]
    assert writer.recorded == []


async def test_over_cap_drops_series_and_emits_one_suggestion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Over-cap series are dropped to the cap; one warning suggestion emitted."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(f"cardinality_caps:\n  families:\n    {M_SENSOR_VALUE}: 4\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))

    states = [
        _sensor_state(f"sensor.t{i:05d}", "20.0", device_class="temperature") for i in range(9)
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSensorValueCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    series = _gauges(writer, M_SENSOR_VALUE)
    assert len(series) == 4  # noqa: PLR2004

    drop = [g for g in _gauges(writer, _DROP_METRIC) if g.labels.get("family") == M_SENSOR_VALUE]
    assert len(drop) == 1
    assert drop[0].value == 5.0  # noqa: PLR2004

    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


async def test_empty_states_ok_only_drop_gauge() -> None:
    """Empty states -> ok=True, only the drop gauge (value 0.0), no series."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates([]))

    result = await HaSensorValueCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_SENSOR_VALUE) == []
    drop = _gauges(writer, _DROP_METRIC)
    assert len(drop) == 1
    assert drop[0].value == 0.0
    assert drop[0].labels == {"family": M_SENSOR_VALUE}
