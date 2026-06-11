"""Tests for HaEntityAvailableCollector — per-entity availability + staleness gauges."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_entity_available import (
    M_ENTITY_AVAILABLE,
    M_ENTITY_LAST_CHANGED_SECONDS,
    M_ENTITY_PARSE_ERRORS,
    HaEntityAvailableCollector,
)


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


def _state(entity_id: str, state: str, last_changed: str) -> HaState:
    """Build an HaState with the three fields the collector reads (others empty/defaulted)."""
    return HaState(
        entity_id=entity_id,
        state=state,
        attributes={},
        last_changed=last_changed,
        last_updated=last_changed,
    )


def _ctx(
    writer: InMemoryMetricsWriter,
    ha: object,
    *,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
) -> SimpleNamespace:
    """Build a partial CollectorContext as a SimpleNamespace.

    Only the fields run() reads are populated: config (with optional ha_domains_*), vm, ha, log.
    Passed to .run() with `# type: ignore[arg-type]` (SimpleNamespace is not a real
    CollectorContext).
    """
    config_kwargs: dict[str, object] = {}
    if allow is not None:
        config_kwargs["ha_domains_allow"] = allow
    if deny is not None:
        config_kwargs["ha_domains_deny"] = deny
    return SimpleNamespace(
        config=SimpleNamespace(**config_kwargs),
        vm=writer,
        ha=ha,
        log=structlog.get_logger().bind(collector="ha_entity_available"),
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all recorded gauges with the given metric name."""
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]


def test_extract_domain_simple_and_extra_dots() -> None:
    """Test extract_domain for simple cases and entity_ids with extra dots."""
    assert extract_domain("sensor.x") == "sensor"
    assert extract_domain("binary_sensor.y") == "binary_sensor"
    assert extract_domain("sensor.a.b") == "sensor"
    assert extract_domain("noseparator") == "noseparator"


async def test_mixed_states_availability() -> None:
    """Test availability computation for real, unavailable, unknown, and empty states."""
    recent_iso = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    states = [
        _state("sensor.real", "42", recent_iso),
        _state("sensor.un", "unavailable", recent_iso),
        _state("sensor.unk", "unknown", recent_iso),
        _state("sensor.empty", "", recent_iso),
    ]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    result = await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    avail = _gauges(writer, M_ENTITY_AVAILABLE)
    by_entity = {g.labels["entity_id"]: g.value for g in avail}
    assert by_entity["sensor.real"] == 1.0
    assert by_entity["sensor.un"] == 0.0
    assert by_entity["sensor.unk"] == 0.0
    assert by_entity["sensor.empty"] == 0.0
    assert all(g.labels["domain"] == "sensor" for g in avail)


async def test_availability_case_sensitive() -> None:
    """Test that availability check is case-sensitive (Unavailable != unavailable)."""
    recent_iso = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    states = [_state("sensor.x", "Unavailable", recent_iso)]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    by_entity = {g.labels["entity_id"]: g.value for g in _gauges(writer, M_ENTITY_AVAILABLE)}
    assert by_entity["sensor.x"] == 1.0


async def test_domain_extraction_labels() -> None:
    """Test that domain labels are correctly extracted for entities."""
    recent_iso = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    states = [
        _state("sensor.a", "1", recent_iso),
        _state("binary_sensor.b", "on", recent_iso),
        _state("sensor.a.b", "2", recent_iso),
    ]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    avail = _gauges(writer, M_ENTITY_AVAILABLE)
    by_entity = {g.labels["entity_id"]: g.labels["domain"] for g in avail}
    assert by_entity["sensor.a"] == "sensor"
    assert by_entity["binary_sensor.b"] == "binary_sensor"
    assert by_entity["sensor.a.b"] == "sensor"


async def test_denied_domain_produces_no_series() -> None:
    """Test that denied domains are skipped (not in both availability and staleness)."""
    recent_iso = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    states = [_state("light.kitchen", "on", recent_iso)]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha, deny=["light"])

    await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    assert _gauges(writer, M_ENTITY_AVAILABLE) == []
    assert _gauges(writer, M_ENTITY_LAST_CHANGED_SECONDS) == []


async def test_non_allowed_domain_skipped() -> None:
    """Test that domains not in the allow list are skipped."""
    recent_iso = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    states = [_state("automation.foo", "1", recent_iso)]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    assert _gauges(writer, M_ENTITY_AVAILABLE) == []


async def test_custom_allow_and_deny_honored() -> None:
    """Test that custom allow and deny lists override defaults."""
    recent_iso = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    states = [
        _state("sensor.a", "1", recent_iso),
        _state("switch.b", "on", recent_iso),
        _state("light.c", "on", recent_iso),
    ]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha, allow=["sensor", "switch", "light"], deny=["switch"])

    await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    emitted = {g.labels["entity_id"] for g in _gauges(writer, M_ENTITY_AVAILABLE)}
    assert emitted == {"sensor.a", "light.c"}


async def test_last_changed_seconds_from_fixed_timestamp() -> None:
    """Test staleness computation from a known fixed timestamp."""
    changed = datetime.now(UTC) - timedelta(seconds=120)
    states = [_state("sensor.x", "1", changed.isoformat())]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    lc = _gauges(writer, M_ENTITY_LAST_CHANGED_SECONDS)
    assert len(lc) == 1
    assert 110.0 <= lc[0].value <= 130.0  # noqa: PLR2004


async def test_last_changed_naive_timestamp_assumed_utc() -> None:
    """Test that naive (tzinfo-less) timestamps are assumed to be UTC."""
    naive = (datetime.now(UTC) - timedelta(seconds=60)).replace(tzinfo=None).isoformat()
    states = [_state("sensor.x", "1", naive)]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    lc = _gauges(writer, M_ENTITY_LAST_CHANGED_SECONDS)
    assert len(lc) == 1
    assert 50.0 <= lc[0].value <= 70.0  # noqa: PLR2004


async def test_negative_skew_clamped_to_zero() -> None:
    """Test that negative staleness (future timestamp) is clamped to 0.0."""
    future = datetime.now(UTC) + timedelta(seconds=300)
    states = [_state("sensor.x", "1", future.isoformat())]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    lc = _gauges(writer, M_ENTITY_LAST_CHANGED_SECONDS)
    assert len(lc) == 1
    assert lc[0].value == 0.0


async def test_staleness_emitted_for_unavailable_entity() -> None:
    """Test that staleness is emitted even for unavailable entities."""
    changed = datetime.now(UTC) - timedelta(seconds=30)
    states = [_state("sensor.x", "unavailable", changed.isoformat())]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    avail = _gauges(writer, M_ENTITY_AVAILABLE)
    assert avail[0].value == 0.0

    lc = _gauges(writer, M_ENTITY_LAST_CHANGED_SECONDS)
    assert len(lc) == 1


async def test_unparseable_last_changed_skips_staleness_counts_error() -> None:
    """Test that parse errors skip staleness but keep availability."""
    recent_iso = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    states = [
        _state("sensor.good", "1", recent_iso),
        _state("sensor.bad", "1", ""),
        _state("sensor.bad2", "1", "not-a-date"),
    ]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    avail = _gauges(writer, M_ENTITY_AVAILABLE)
    assert len(avail) == 3  # noqa: PLR2004

    lc = _gauges(writer, M_ENTITY_LAST_CHANGED_SECONDS)
    lc_entities = {g.labels["entity_id"] for g in lc}
    assert lc_entities == {"sensor.good"}

    errors = _gauges(writer, M_ENTITY_PARSE_ERRORS)
    assert len(errors) == 1
    assert errors[0].value == 2.0  # noqa: PLR2004
    assert errors[0].labels == {}


async def test_parse_errors_gauge_absent_when_zero() -> None:
    """Test that parse-error gauge is absent when count is 0."""
    recent_iso = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    states = [_state("sensor.x", "1", recent_iso)]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    assert _gauges(writer, M_ENTITY_PARSE_ERRORS) == []


async def test_over_cap_drops_series_and_emits_one_suggestion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that over-cap entities are dropped and suggestion events are emitted."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(
        "cardinality_caps:\n"
        "  families:\n"
        f"    {M_ENTITY_AVAILABLE}: 5\n"
        f"    {M_ENTITY_LAST_CHANGED_SECONDS}: 5\n"
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))

    recent_iso = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    states = [_state(f"sensor.e{i:05d}", "1", recent_iso) for i in range(10)]
    ha = _FakeHaStates(states)
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    result = await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    avail = _gauges(writer, M_ENTITY_AVAILABLE)
    assert len(avail) == 5  # noqa: PLR2004

    drop = _gauges(writer, "homelab_metric_family_dropped_series")
    available_drop = [g for g in drop if g.labels.get("family") == M_ENTITY_AVAILABLE]
    assert len(available_drop) == 1
    assert available_drop[0].value == 5.0  # noqa: PLR2004

    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 2  # noqa: PLR2004
    assert all(e.severity == "warning" for e in suggestions)


async def test_ha_error_returns_failed_result() -> None:
    """Test that HaError from get_states results in a failed run."""
    ha = _FakeHaError()
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    result = await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["get_states failed: down"]
    assert writer.recorded == []


async def test_ha_none_returns_failed_result() -> None:
    """Test that ctx.ha is None results in a failed run."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, None)

    result = await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["ha client not configured"]
    assert writer.recorded == []


async def test_empty_states_list_ok_no_per_entity_series() -> None:
    """Test that empty states list is ok and produces only drop gauges."""
    ha = _FakeHaStates([])
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, ha)

    result = await HaEntityAvailableCollector().run(ctx)  # type: ignore[arg-type]

    assert result.ok is True
    assert _gauges(writer, M_ENTITY_AVAILABLE) == []
    assert _gauges(writer, M_ENTITY_LAST_CHANGED_SECONDS) == []

    drop = _gauges(writer, "homelab_metric_family_dropped_series")
    assert len(drop) == 2  # noqa: PLR2004
    assert all(g.value == 0.0 for g in drop)

    assert _gauges(writer, M_ENTITY_PARSE_ERRORS) == []
