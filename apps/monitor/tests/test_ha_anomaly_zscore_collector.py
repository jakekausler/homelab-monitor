"""Tests for HaAnomalyZscoreCollector — stateful rolling per-entity z-score gauges."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import structlog

from homelab_monitor.kernel.ha.client import HaState
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter, MetricEntry
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_anomaly_zscore import (
    M_ZSCORE,
    HaAnomalyZscoreCollector,
)

_DROP_METRIC = "homelab_metric_family_dropped_series"

# Named test consts (avoid PLR2004 magic-number lint on assertion literals).
_MIN_SAMPLES = 12
_WINDOW_SAMPLES = 48
_STABLE_VALUE = 20.0
_NEAR_ZERO = 0.5  # |z| tolerance for a perfectly-warmed stable sensor (z must be ~0)
_SPIKE_VALUE = 1000.0
_HIGH_Z = 2.0  # a clear outlier produces |z| well above this


class _FakeHaMultiTick:
    """HA client double whose get_states() pops the NEXT states list each call.

    Construct with a list-of-lists (one inner list per planned tick). Each await of
    get_states() returns (and consumes) the next inner list. Lets ONE collector instance
    be driven across many ticks with distinct snapshots.
    """

    def __init__(self, ticks: list[list[HaState]]) -> None:
        self._ticks = list(ticks)

    async def get_states(self) -> list[HaState] | HaError:
        return self._ticks.pop(0)


class _FakeHaError:
    """HA client double whose get_states() returns an HaError."""

    async def get_states(self) -> list[HaState] | HaError:
        return HaError(reason="unreachable", message="get_states failed: down")


def _sensor(
    entity_id: str,
    value: str,
    *,
    device_class: str = "temperature",
    state_class: str = "measurement",
) -> HaState:
    """Build a measurement-class numeric sensor HaState."""
    return HaState(
        entity_id=entity_id,
        state=value,
        attributes={"device_class": device_class, "state_class": state_class},
        last_changed="",
        last_updated="",
    )


def _plain(entity_id: str, value: str) -> HaState:
    """Build a non-eligible HaState (no state_class / device_class attributes)."""
    return HaState(
        entity_id=entity_id,
        state=value,
        attributes={},
        last_changed="",
        last_updated="",
    )


def _ctx(writer: InMemoryMetricsWriter, ha: object) -> SimpleNamespace:
    """Partial CollectorContext as a SimpleNamespace (only fields run() reads)."""
    return SimpleNamespace(
        config=SimpleNamespace(),
        vm=writer,
        ha=ha,
        log=structlog.get_logger().bind(collector="ha_anomaly_zscore"),
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all recorded gauges with the given metric name."""
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]


def _zscores(writer: InMemoryMetricsWriter) -> list[MetricEntry]:
    """Return all recorded z-score gauges."""
    return _gauges(writer, M_ZSCORE)


# --- failure-path tests (ha None / HaError) ---


async def test_ha_none_returns_failed_result() -> None:
    """ctx.ha is None -> failed run, 'ha client not configured', no writes."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, None)
    result = await HaAnomalyZscoreCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["ha client not configured"]
    assert writer.recorded == []


async def test_ha_error_returns_failed_result() -> None:
    """HaError from get_states -> failed run with the error message, no writes."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaError())
    result = await HaAnomalyZscoreCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["get_states failed: down"]
    assert writer.recorded == []


# --- cold-start warmup (window < min_samples -> no z-score, only drop gauge) ---


async def test_cold_start_below_min_samples_emits_no_zscore() -> None:
    """First few ticks (< min_samples) emit only the drop gauge, no z-score series."""
    collector = HaAnomalyZscoreCollector()
    # Feed min_samples-1 ticks of a stable eligible sensor.
    for _ in range(_MIN_SAMPLES - 1):
        writer = InMemoryMetricsWriter()
        ctx = _ctx(writer, _FakeHaMultiTick([[_sensor("sensor.temp", str(_STABLE_VALUE))]]))
        result = await collector.run(ctx)  # type: ignore[arg-type]
        assert result.ok is True
        assert _zscores(writer) == []
        # Only the always-written drop gauge (value 0.0) is present.
        drop = _gauges(writer, _DROP_METRIC)
        assert len(drop) == 1
        assert drop[0].value == 0.0


# --- warmup then emit (stable window -> z ~ 0 once min_samples reached) ---


async def test_stable_sensor_emits_near_zero_zscore_after_warmup() -> None:
    """After min_samples stable-but-jittered values, the z-score is ~0."""
    collector = HaAnomalyZscoreCollector()
    # Use a tiny jitter so pstdev > epsilon (a perfectly-flat window would be
    # zero-variance and SKIP — that's a different test). Alternate +/-0.1 around 20.
    values = [str(_STABLE_VALUE + (0.1 if i % 2 else -0.1)) for i in range(_MIN_SAMPLES)]
    for _i, v in enumerate(values):
        writer = InMemoryMetricsWriter()
        ctx = _ctx(writer, _FakeHaMultiTick([[_sensor("sensor.temp", v)]]))
        await collector.run(ctx)  # type: ignore[arg-type]
    # Feed one more tick at exactly the mean value so z is ~0 (not +/-1.0 from jitter).
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaMultiTick([[_sensor("sensor.temp", str(_STABLE_VALUE))]]))
    await collector.run(ctx)  # type: ignore[arg-type]
    z = _zscores(writer)
    assert len(z) == 1
    assert z[0].labels == {"entity_id": "sensor.temp"}
    assert abs(z[0].value) < _NEAR_ZERO


# --- spike (stable window then large outlier -> high |z|) ---


async def test_spike_emits_high_zscore() -> None:
    """A large outlier after a stable warmed window produces a high-magnitude z-score."""
    collector = HaAnomalyZscoreCollector()
    # Warm with min_samples jittered-stable values (pstdev small but > epsilon).
    for i in range(_MIN_SAMPLES):
        v = str(_STABLE_VALUE + (0.1 if i % 2 else -0.1))
        writer = InMemoryMetricsWriter()
        ctx = _ctx(writer, _FakeHaMultiTick([[_sensor("sensor.temp", v)]]))
        await collector.run(ctx)  # type: ignore[arg-type]
    # Now a spike.
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaMultiTick([[_sensor("sensor.temp", str(_SPIKE_VALUE))]]))
    await collector.run(ctx)  # type: ignore[arg-type]
    z = _zscores(writer)
    assert len(z) == 1
    assert abs(z[0].value) > _HIGH_Z


# --- zero-variance (identical values -> pstdev < epsilon -> NO series) ---


async def test_zero_variance_emits_no_series() -> None:
    """A window of identical values has pstdev 0 (< epsilon) -> no z-score emitted."""
    collector = HaAnomalyZscoreCollector()
    writer = InMemoryMetricsWriter()
    for _ in range(_MIN_SAMPLES):
        writer = InMemoryMetricsWriter()
        ctx = _ctx(writer, _FakeHaMultiTick([[_sensor("sensor.flat", str(_STABLE_VALUE))]]))
        result = await collector.run(ctx)  # type: ignore[arg-type]
        assert result.ok is True
    # Even fully warmed, an exactly-flat sensor emits NO z-score series.
    assert _zscores(writer) == []
    # Drop gauge still written (value 0.0; zero candidates -> zero dropped).
    drop = _gauges(writer, _DROP_METRIC)
    assert len(drop) == 1
    assert drop[0].value == 0.0


# --- non-eligible skip (wrong device_class / missing state_class) ---


async def test_non_eligible_states_never_enter_window() -> None:
    """States lacking state_class=measurement or with a non-allowed device_class are skipped."""
    collector = HaAnomalyZscoreCollector()
    writer = InMemoryMetricsWriter()
    # Feed many ticks of NON-eligible states; never warms, never emits.
    for _ in range(_MIN_SAMPLES + 2):
        writer = InMemoryMetricsWriter()
        states = [
            _plain("light.kitchen", "on"),  # no attributes at all
            _sensor("sensor.enum", "5", device_class="enum"),  # wrong device_class
            _sensor("sensor.nostate", "5", state_class="total"),  # wrong state_class
        ]
        ctx = _ctx(writer, _FakeHaMultiTick([states]))
        result = await collector.run(ctx)  # type: ignore[arg-type]
        assert result.ok is True
    assert _zscores(writer) == []


# --- parse-None skip (unavailable/unknown value not appended) ---


async def test_unparseable_value_not_appended() -> None:
    """An eligible sensor reporting 'unavailable'/'unknown' contributes no window value."""
    collector = HaAnomalyZscoreCollector()
    writer = InMemoryMetricsWriter()
    # Eligible device_class but non-numeric state every tick -> never warms.
    for _ in range(_MIN_SAMPLES + 2):
        writer = InMemoryMetricsWriter()
        ctx = _ctx(writer, _FakeHaMultiTick([[_sensor("sensor.temp", "unavailable")]]))
        result = await collector.run(ctx)  # type: ignore[arg-type]
        assert result.ok is True
    assert _zscores(writer) == []


# --- config overrides: excluded + extra entity_ids ---


async def test_excluded_and_extra_entity_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """excluded_entity_ids drops an otherwise-eligible sensor; extra_entity_ids includes a
    sensor whose device_class would normally be rejected."""
    monkeypatch.setenv("HOMELAB_MONITOR_HA_ZSCORE_EXCLUDED_ENTITY_IDS", "sensor.excluded")
    monkeypatch.setenv("HOMELAB_MONITOR_HA_ZSCORE_EXTRA_ENTITY_IDS", "sensor.weird")
    collector = HaAnomalyZscoreCollector()
    writer = InMemoryMetricsWriter()
    # Warm both: sensor.excluded (eligible heuristic but excluded) and sensor.weird
    # (device_class 'enum' rejected by heuristic but force-included). Jitter so weird
    # has variance > epsilon.
    for i in range(_MIN_SAMPLES):
        v_weird = str(_STABLE_VALUE + (0.1 if i % 2 else -0.1))
        writer = InMemoryMetricsWriter()
        states = [
            _sensor("sensor.excluded", str(_STABLE_VALUE + (0.1 if i % 2 else -0.1))),
            _sensor("sensor.weird", v_weird, device_class="enum"),
        ]
        ctx = _ctx(writer, _FakeHaMultiTick([states]))
        await collector.run(ctx)  # type: ignore[arg-type]
    z = _zscores(writer)
    labels = {e.labels["entity_id"] for e in z}
    assert "sensor.excluded" not in labels  # hard-excluded
    assert "sensor.weird" in labels  # force-included despite enum device_class


# --- window eviction (deque maxlen bounds the baseline) ---


async def test_window_bounded_by_maxlen(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a small window, old values are evicted; the z-score reflects only recent values.

    Set window_samples == min_samples == 4. Feed 4 stable values (warms), then feed enough
    NEW stable-at-a-different-level values to fully evict the original baseline; the sensor
    keeps emitting sanely (a finite z-score) rather than drifting on stale data.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_HA_ZSCORE_WINDOW_SAMPLES", "4")
    monkeypatch.setenv("HOMELAB_MONITOR_HA_ZSCORE_MIN_SAMPLES", "4")
    collector = HaAnomalyZscoreCollector()
    writer = InMemoryMetricsWriter()
    # Phase 1: warm around 20 (jittered).
    for i in range(4):
        v = str(20.0 + (0.1 if i % 2 else -0.1))
        writer = InMemoryMetricsWriter()
        ctx = _ctx(writer, _FakeHaMultiTick([[_sensor("sensor.t", v)]]))
        await collector.run(ctx)  # type: ignore[arg-type]
    # Phase 2: shift baseline to ~100 (jittered); after 4 ticks the deque holds only ~100s.
    for i in range(5):
        v = str(100.0 + (0.1 if i % 2 else -0.1))
        writer = InMemoryMetricsWriter()
        ctx = _ctx(writer, _FakeHaMultiTick([[_sensor("sensor.t", v)]]))
        await collector.run(ctx)  # type: ignore[arg-type]
    z = _zscores(writer)
    assert len(z) == 1
    # The baseline is now ~100, the current is ~100, so |z| is small — the old ~20 values
    # were evicted (had they remained, current 100 vs a mixed mean would give a large |z|).
    assert abs(z[0].value) < _HIGH_Z


# --- metrics_emitted accounting (survivors + 1) ---


async def test_metrics_emitted_equals_survivors_plus_drop_gauge() -> None:
    """metrics_emitted == number of z-score survivors + 1 (the drop gauge)."""
    collector = HaAnomalyZscoreCollector()
    writer = InMemoryMetricsWriter()
    # Warm two distinct eligible sensors with jitter so both emit.
    result = None
    for i in range(_MIN_SAMPLES):
        a = str(20.0 + (0.1 if i % 2 else -0.1))
        b = str(30.0 + (0.1 if i % 2 else -0.1))
        writer = InMemoryMetricsWriter()
        states = [_sensor("sensor.a", a), _sensor("sensor.b", b)]
        ctx = _ctx(writer, _FakeHaMultiTick([states]))
        result = await collector.run(ctx)  # type: ignore[arg-type]
    assert result is not None
    z = _zscores(writer)
    assert len(z) == 2  # noqa: PLR2004
    assert result.metrics_emitted == len(z) + 1


# --- empty states (reachable HA, nothing eligible) ---


async def test_empty_states_ok_only_drop_gauge() -> None:
    """Reachable HA with an empty states list -> ok=True, only the drop gauge (0.0)."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaMultiTick([[]]))
    result = await HaAnomalyZscoreCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    assert _zscores(writer) == []
    drop = _gauges(writer, _DROP_METRIC)
    assert len(drop) == 1
    assert drop[0].value == 0.0
    assert drop[0].labels == {"family": M_ZSCORE}


async def test_non_finite_value_never_emitted_and_no_raise() -> None:
    """An eligible sensor reporting 'inf' (or 'nan') never enters the window.

    parse_float_state rejects non-finite floats -> None -> value not appended ->
    window never reaches min_samples -> no z-score series emitted, even after
    _MIN_SAMPLES + 2 ticks. run() must still return ok=True (no exception raised).
    """
    collector = HaAnomalyZscoreCollector()
    writer = InMemoryMetricsWriter()
    result = None
    for _ in range(_MIN_SAMPLES + 2):
        writer = InMemoryMetricsWriter()
        ctx = _ctx(
            writer,
            _FakeHaMultiTick([[_sensor("sensor.inf_temp", "inf", device_class="temperature")]]),
        )
        result = await collector.run(ctx)  # type: ignore[arg-type]
        assert result.ok is True
    assert result is not None
    assert _zscores(writer) == []


async def test_window_resize_on_config_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing window_samples live rebuilds the deque, keeping the most-recent values."""
    monkeypatch.setenv("HOMELAB_MONITOR_HA_ZSCORE_WINDOW_SAMPLES", "4")
    monkeypatch.setenv("HOMELAB_MONITOR_HA_ZSCORE_MIN_SAMPLES", "2")
    collector = HaAnomalyZscoreCollector()
    for i in range(3):
        v = str(20.0 + (0.1 if i % 2 else -0.1))
        writer = InMemoryMetricsWriter()
        ctx = _ctx(writer, _FakeHaMultiTick([[_sensor("sensor.t", v)]]))
        await collector.run(ctx)  # type: ignore[arg-type]
    # Now shrink the window; the deque is rebuilt with maxlen=2 (resize branch).
    monkeypatch.setenv("HOMELAB_MONITOR_HA_ZSCORE_WINDOW_SAMPLES", "2")
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaMultiTick([[_sensor("sensor.t", "20.2")]]))
    result = await collector.run(ctx)  # type: ignore[arg-type]
    assert result.ok is True
    # Still emits (>= min_samples=2 after resize keeps the last 2 values).
    assert len(_zscores(writer)) == 1
