"""Tests for HaUpdateCollector — per-entity update-available gauges."""

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
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_update import (
    M_UPDATE_AVAILABLE,
    HaUpdateCollector,
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


def _update_state(entity_id: str, state: str, *, title: object = "Some Package") -> HaState:
    """Build an update-domain HaState with the given state and title attribute."""
    return HaState(
        entity_id=entity_id,
        state=state,
        attributes={"title": title},
        last_changed="",
        last_updated="",
    )


def _ctx(writer: InMemoryMetricsWriter, ha: object) -> SimpleNamespace:
    """Build a partial CollectorContext as a SimpleNamespace."""
    return SimpleNamespace(
        config=SimpleNamespace(),
        vm=writer,
        ha=ha,
        log=structlog.get_logger().bind(collector="ha_update"),
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all recorded gauges with the given metric name."""
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]


# --- shared-helper unit test (extract_domain used by this collector) ---


def test_extract_domain_update_entity() -> None:
    """extract_domain correctly parses update.* entity_ids."""
    assert extract_domain("update.home_assistant_core") == "update"
    assert extract_domain("update.addon_foo_bar") == "update"


# --- collector behavior tests ---


async def test_update_on_emits_1_off_emits_0() -> None:
    """update 'on' emits 1.0; 'off' emits 0.0; both carry entity_id and title labels."""
    states = [
        _update_state("update.core", "on", title="Home Assistant Core"),
        _update_state("update.addon_ssh", "off", title="SSH Add-on"),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaUpdateCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    gauges = _gauges(writer, M_UPDATE_AVAILABLE)
    assert len(gauges) == 2  # noqa: PLR2004

    on_gauge = next(g for g in gauges if g.labels["entity_id"] == "update.core")
    assert on_gauge.value == 1.0
    assert on_gauge.labels == {"entity_id": "update.core", "title": "Home Assistant Core"}

    off_gauge = next(g for g in gauges if g.labels["entity_id"] == "update.addon_ssh")
    assert off_gauge.value == 0.0
    assert off_gauge.labels == {"entity_id": "update.addon_ssh", "title": "SSH Add-on"}


async def test_non_update_entity_not_emitted() -> None:
    """Entities outside the update domain (e.g. sensor.*) are not emitted."""
    states = [
        HaState(
            entity_id="sensor.temperature",
            state="on",
            attributes={},
            last_changed="",
            last_updated="",
        ),
        _update_state("update.core", "on", title="Core"),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaUpdateCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    gauges = _gauges(writer, M_UPDATE_AVAILABLE)
    assert len(gauges) == 1
    assert gauges[0].labels["entity_id"] == "update.core"


async def test_unavailable_and_unknown_states_skipped() -> None:
    """update entities with state 'unavailable' or 'unknown' are skipped (no series emitted)."""
    states = [
        _update_state("update.a", "unavailable"),
        _update_state("update.b", "unknown"),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaUpdateCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _gauges(writer, M_UPDATE_AVAILABLE) == []


async def test_missing_title_defaults_to_empty_string() -> None:
    """An update entity with no title attribute emits title=''."""
    states = [
        HaState(
            entity_id="update.no_title",
            state="on",
            attributes={},  # no "title" key
            last_changed="",
            last_updated="",
        ),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaUpdateCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    gauges = _gauges(writer, M_UPDATE_AVAILABLE)
    assert len(gauges) == 1
    assert gauges[0].labels["title"] == ""


async def test_non_str_title_defaults_to_empty_string() -> None:
    """An update entity with a non-str title attribute (e.g. int 123) emits title=''."""
    states = [
        _update_state("update.bad_title", "on", title=123),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaUpdateCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    gauges = _gauges(writer, M_UPDATE_AVAILABLE)
    assert len(gauges) == 1
    assert gauges[0].labels["title"] == ""


async def test_ha_error_returns_failed_result() -> None:
    """HaError from get_states results in a failed run with the error message."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaError())

    result = await HaUpdateCollector().run(ctx)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["get_states failed: down"]
    assert writer.recorded == []


async def test_ha_none_returns_failed_result() -> None:
    """ctx.ha is None results in a failed run with 'ha client not configured'."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, None)

    result = await HaUpdateCollector().run(ctx)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["ha client not configured"]
    assert writer.recorded == []


async def test_over_cap_drops_series_and_emits_one_suggestion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Over-cap update entities are dropped to the cap; one warning suggestion emitted."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(f"cardinality_caps:\n  families:\n    {M_UPDATE_AVAILABLE}: 5\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))

    states = [_update_state(f"update.e{i:05d}", "on", title=f"Package {i}") for i in range(10)]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaUpdateCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    gauges = _gauges(writer, M_UPDATE_AVAILABLE)
    assert len(gauges) == 5  # noqa: PLR2004

    drop = [
        g for g in _gauges(writer, _DROP_METRIC) if g.labels.get("family") == M_UPDATE_AVAILABLE
    ]
    assert len(drop) == 1
    assert drop[0].value == 5.0  # noqa: PLR2004

    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


async def test_empty_states_list_ok_only_drop_gauge() -> None:
    """Empty states -> ok=True, only the drop gauge (value 0.0), no update series."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates([]))

    result = await HaUpdateCollector().run(ctx)  # type: ignore[arg-type]

    assert result.ok is True
    assert _gauges(writer, M_UPDATE_AVAILABLE) == []

    drop = _gauges(writer, _DROP_METRIC)
    assert len(drop) == 1
    assert drop[0].value == 0.0
    assert drop[0].labels == {"family": M_UPDATE_AVAILABLE}
