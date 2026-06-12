"""Tests for HaSafetySensorsCollector — safety binary_sensor on/off gauges."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

from homelab_monitor.kernel.ha.client import HaState
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter, MetricEntry
from homelab_monitor.kernel.plugins.types import SuggestionEvent
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_safety_sensors import (
    M_BINARY_SENSOR_ON,
    HaSafetySensorsCollector,
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


def _binary_state(entity_id: str, state: str, *, device_class: str | None = "smoke") -> HaState:
    """Build a binary_sensor HaState with the given state and device_class."""
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
    """Build a partial CollectorContext as a SimpleNamespace."""
    return SimpleNamespace(
        config=SimpleNamespace(),
        vm=writer,
        ha=ha,
        log=structlog.get_logger().bind(collector="ha_safety_sensors"),
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all recorded gauges with the given metric name."""
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]


async def test_on_emits_one() -> None:
    """A safety binary_sensor in state 'on' emits value 1.0 with full labels."""
    states = [_binary_state("binary_sensor.kitchen_smoke", "on", device_class="smoke")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    series = _gauges(writer, M_BINARY_SENSOR_ON)
    assert len(series) == 1
    assert series[0].value == 1.0
    assert series[0].labels == {
        "entity_id": "binary_sensor.kitchen_smoke",
        "domain": "binary_sensor",
        "device_class": "smoke",
    }


async def test_off_emits_zero() -> None:
    """A safety binary_sensor in state 'off' emits value 0.0."""
    states = [_binary_state("binary_sensor.front_door", "off", device_class="door")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    series = _gauges(writer, M_BINARY_SENSOR_ON)
    assert len(series) == 1
    assert series[0].value == 0.0
    assert series[0].labels["device_class"] == "door"


async def test_unavailable_and_unknown_skipped() -> None:
    """unavailable / unknown / empty / other states are skipped (no series)."""
    states = [
        _binary_state("binary_sensor.a", "unavailable", device_class="smoke"),
        _binary_state("binary_sensor.b", "unknown", device_class="gas"),
        _binary_state("binary_sensor.c", "", device_class="moisture"),
        _binary_state("binary_sensor.d", "weird", device_class="window"),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_BINARY_SENSOR_ON) == []


async def test_non_safety_device_class_skipped() -> None:
    """A binary_sensor whose device_class is not in the safety set is skipped."""
    states = [_binary_state("binary_sensor.motion_hall", "on", device_class="motion")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_BINARY_SENSOR_ON) == []


async def test_missing_device_class_skipped() -> None:
    """A binary_sensor with no device_class attribute is skipped."""
    states = [_binary_state("binary_sensor.bare", "on", device_class=None)]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_BINARY_SENSOR_ON) == []


async def test_non_binary_sensor_domain_skipped() -> None:
    """A safety-classed entity in a non-binary_sensor domain is skipped."""
    states = [
        HaState(
            entity_id="sensor.smoke_level",
            state="on",
            attributes={"device_class": "smoke"},
            last_changed="",
            last_updated="",
        )
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_BINARY_SENSOR_ON) == []


async def test_device_class_allow_set_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env override changes which device_class is emitted."""
    monkeypatch.setenv("HOMELAB_MONITOR_HA_SAFETY_DEVICE_CLASSES", "leak")
    states = [
        _binary_state("binary_sensor.leak_basement", "on", device_class="leak"),
        _binary_state("binary_sensor.kitchen_smoke", "on", device_class="smoke"),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    series = _gauges(writer, M_BINARY_SENSOR_ON)
    assert len(series) == 1
    assert series[0].labels["entity_id"] == "binary_sensor.leak_basement"


async def test_empty_override_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty override env var falls back to the default safety set."""
    monkeypatch.setenv("HOMELAB_MONITOR_HA_SAFETY_DEVICE_CLASSES", "  , ,")
    states = [_binary_state("binary_sensor.kitchen_smoke", "on", device_class="smoke")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert len(_gauges(writer, M_BINARY_SENSOR_ON)) == 1


async def test_ha_error_returns_failed_result() -> None:
    """HaError from get_states -> failed run with the error message, no writes."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaError())

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["get_states failed: down"]
    assert writer.recorded == []


async def test_ha_none_returns_failed_result() -> None:
    """ctx.ha is None -> failed run with 'ha client not configured', no writes."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, None)

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
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
    cfg_file.write_text(f"cardinality_caps:\n  families:\n    {M_BINARY_SENSOR_ON}: 3\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))

    states = [
        _binary_state(f"binary_sensor.s{i:05d}", "on", device_class="smoke") for i in range(8)
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    series = _gauges(writer, M_BINARY_SENSOR_ON)
    assert len(series) == 3  # noqa: PLR2004

    drop = [
        g for g in _gauges(writer, _DROP_METRIC) if g.labels.get("family") == M_BINARY_SENSOR_ON
    ]
    assert len(drop) == 1
    assert drop[0].value == 5.0  # noqa: PLR2004

    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


async def test_empty_states_ok_only_drop_gauge() -> None:
    """Empty states -> ok=True, only the drop gauge (value 0.0), no series."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates([]))

    result = await HaSafetySensorsCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_BINARY_SENSOR_ON) == []
    drop = _gauges(writer, _DROP_METRIC)
    assert len(drop) == 1
    assert drop[0].value == 0.0
    assert drop[0].labels == {"family": M_BINARY_SENSOR_ON}
