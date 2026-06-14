"""Tests for HaCadenceCollector — automation/script run-cadence + enabled gauges."""

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
    parse_iso_or_none,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_cadence import (
    M_AUTOMATION_ENABLED,
    M_AUTOMATION_LAST_TRIGGERED,
    M_PARSE_ERRORS,
    M_SCRIPT_LAST_TRIGGERED,
    HaCadenceCollector,
)

_DROP_METRIC = "homelab_metric_family_dropped_series"

# Sentinel meaning "omit the last_triggered attribute entirely".
_OMIT = object()


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


def _automation_state(
    entity_id: str,
    state: str,
    *,
    last_triggered: object = _OMIT,
) -> HaState:
    """Build an automation-domain HaState.

    last_triggered semantics:
      - _OMIT  -> attribute absent (attributes.get returns None) == never-triggered
      - None   -> attribute present but None (also treated as never-triggered)
      - <str>  -> attribute present with that value (parsed or counted as error)
    """
    attributes: dict[str, object] = {}
    if last_triggered is not _OMIT:
        attributes["last_triggered"] = last_triggered
    return HaState(
        entity_id=entity_id,
        state=state,
        attributes=attributes,
        last_changed="",
        last_updated="",
    )


def _script_state(
    entity_id: str,
    *,
    state: str = "off",
    last_triggered: object = _OMIT,
) -> HaState:
    """Build a script-domain HaState (state defaults to 'off'; scripts have no enabled metric)."""
    attributes: dict[str, object] = {}
    if last_triggered is not _OMIT:
        attributes["last_triggered"] = last_triggered
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
        log=structlog.get_logger().bind(collector="ha_cadence"),
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all recorded gauges with the given metric name."""
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]


async def test_automation_enabled_on_off_and_other() -> None:
    """automation 'on' -> 1.0, 'off' -> 0.0, other state -> enabled skipped."""
    states = [
        _automation_state("automation.on_one", "on"),
        _automation_state("automation.off_one", "off"),
        _automation_state("automation.weird", "unavailable"),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    enabled = {g.labels["entity_id"]: g.value for g in _gauges(writer, M_AUTOMATION_ENABLED)}
    assert enabled["automation.on_one"] == 1.0
    assert enabled["automation.off_one"] == 0.0
    assert "automation.weird" not in enabled


async def test_automation_last_triggered_recent() -> None:
    """A recent last_triggered yields seconds-since within tolerance."""
    triggered = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    states = [_automation_state("automation.a", "on", last_triggered=triggered)]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    lt = _gauges(writer, M_AUTOMATION_LAST_TRIGGERED)
    assert len(lt) == 1
    assert 110.0 <= lt[0].value <= 130.0  # noqa: PLR2004


async def test_automation_last_triggered_naive_assumed_utc() -> None:
    """A naive (tzinfo-less) last_triggered is assumed UTC."""
    naive = (datetime.now(UTC) - timedelta(seconds=60)).replace(tzinfo=None).isoformat()
    states = [_automation_state("automation.a", "on", last_triggered=naive)]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    lt = _gauges(writer, M_AUTOMATION_LAST_TRIGGERED)
    assert len(lt) == 1
    assert 50.0 <= lt[0].value <= 70.0  # noqa: PLR2004


async def test_automation_last_triggered_future_clamped_to_zero() -> None:
    """A future last_triggered (clock skew) clamps to 0.0."""
    future = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
    states = [_automation_state("automation.a", "on", last_triggered=future)]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    lt = _gauges(writer, M_AUTOMATION_LAST_TRIGGERED)
    assert len(lt) == 1
    assert lt[0].value == 0.0


async def test_automation_missing_last_triggered_skipped_no_error() -> None:
    """Absent last_triggered (never-triggered) -> no series AND no parse error."""
    states = [_automation_state("automation.never", "on")]  # _OMIT -> attribute absent
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    assert _gauges(writer, M_AUTOMATION_LAST_TRIGGERED) == []
    assert _gauges(writer, M_PARSE_ERRORS) == []
    # enabled is still emitted for the automation
    enabled = {g.labels["entity_id"] for g in _gauges(writer, M_AUTOMATION_ENABLED)}
    assert enabled == {"automation.never"}


async def test_automation_explicit_none_last_triggered_skipped_no_error() -> None:
    """last_triggered present but None -> treated as never-triggered, no error."""
    states = [_automation_state("automation.never", "on", last_triggered=None)]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    assert _gauges(writer, M_AUTOMATION_LAST_TRIGGERED) == []
    assert _gauges(writer, M_PARSE_ERRORS) == []


async def test_automation_unparseable_last_triggered_counts_error() -> None:
    """Present-but-unparseable last_triggered -> parse error, no series."""
    states = [
        _automation_state("automation.bad", "on", last_triggered="not-a-date"),
        _automation_state("automation.bad2", "on", last_triggered=""),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    assert _gauges(writer, M_AUTOMATION_LAST_TRIGGERED) == []
    errors = _gauges(writer, M_PARSE_ERRORS)
    assert len(errors) == 1
    assert errors[0].value == 2.0  # noqa: PLR2004
    assert errors[0].labels == {}


async def test_disabled_automation_with_last_triggered_emits_no_triggered_series() -> None:
    """DISABLED (state='off') automation with valid last_triggered -> no triggered series,
    but automation_enabled IS emitted at 0.0. Core behavior-change lock."""
    triggered = (datetime.now(UTC) - timedelta(seconds=90)).isoformat()
    states = [_automation_state("automation.disabled_one", "off", last_triggered=triggered)]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    # No triggered series for a disabled automation.
    assert _gauges(writer, M_AUTOMATION_LAST_TRIGGERED) == []
    # No parse error (the timestamp was valid).
    assert _gauges(writer, M_PARSE_ERRORS) == []
    # Enabled series IS still emitted at 0.0.
    enabled = {g.labels["entity_id"]: g.value for g in _gauges(writer, M_AUTOMATION_ENABLED)}
    assert enabled == {"automation.disabled_one": 0.0}


async def test_unavailable_automation_with_last_triggered_emits_no_triggered_series() -> None:
    """UNAVAILABLE automation with valid last_triggered -> no triggered series AND no
    enabled series (unavailable is skipped by the enabled gate too). Locks the
    '== on' (not '!= off') form of the gate."""
    triggered = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    states = [
        _automation_state("automation.unavailable_one", "unavailable", last_triggered=triggered)
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    # No triggered series.
    assert _gauges(writer, M_AUTOMATION_LAST_TRIGGERED) == []
    # No parse error.
    assert _gauges(writer, M_PARSE_ERRORS) == []
    # No enabled series (unavailable is skipped by the on/off gate).
    enabled_ids = {g.labels["entity_id"] for g in _gauges(writer, M_AUTOMATION_ENABLED)}
    assert "automation.unavailable_one" not in enabled_ids


async def test_disabled_automation_unparseable_last_triggered_still_counts_parse_error() -> None:
    """DISABLED (state='off') automation with unparseable last_triggered ->
    parse error IS counted even though the automation is disabled and emits no
    triggered series. Locks 'parse-error counting is OUTSIDE the state gate'."""
    states = [_automation_state("automation.disabled_bad", "off", last_triggered="not-a-date")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    # No triggered series (disabled + no valid timestamp).
    assert _gauges(writer, M_AUTOMATION_LAST_TRIGGERED) == []
    # Parse error IS incremented — the bad timestamp is counted regardless of enabled state.
    errors = _gauges(writer, M_PARSE_ERRORS)
    assert len(errors) == 1
    assert errors[0].value == 1.0
    assert errors[0].labels == {}
    # Enabled series is 0.0 (off automations still appear in enabled).
    enabled = {g.labels["entity_id"]: g.value for g in _gauges(writer, M_AUTOMATION_ENABLED)}
    assert enabled == {"automation.disabled_bad": 0.0}


async def test_script_last_triggered_emitted() -> None:
    """A script with last_triggered emits script_last_triggered_seconds, no enabled metric."""
    triggered = (datetime.now(UTC) - timedelta(seconds=45)).isoformat()
    states = [_script_state("script.backup", last_triggered=triggered)]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    st = _gauges(writer, M_SCRIPT_LAST_TRIGGERED)
    assert len(st) == 1
    assert st[0].labels == {"entity_id": "script.backup"}
    assert 35.0 <= st[0].value <= 55.0  # noqa: PLR2004
    # scripts get NO enabled metric
    script_enabled = [
        g for g in _gauges(writer, M_AUTOMATION_ENABLED) if g.labels["entity_id"] == "script.backup"
    ]
    assert script_enabled == []


async def test_script_missing_last_triggered_skipped() -> None:
    """A script with no last_triggered emits nothing (no series, no error)."""
    states = [_script_state("script.never")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    assert _gauges(writer, M_SCRIPT_LAST_TRIGGERED) == []
    assert _gauges(writer, M_PARSE_ERRORS) == []


async def test_script_unparseable_last_triggered_counts_error() -> None:
    """A script with present-but-unparseable last_triggered -> parse error, no series."""
    states = [_script_state("script.bad", last_triggered="garbage")]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    assert _gauges(writer, M_SCRIPT_LAST_TRIGGERED) == []
    errors = _gauges(writer, M_PARSE_ERRORS)
    assert len(errors) == 1
    assert errors[0].value == 1.0


async def test_other_domain_entity_ignored() -> None:
    """A sensor.* entity contributes nothing to any cadence family."""
    triggered = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    states = [
        HaState(
            entity_id="sensor.temperature",
            state="on",
            attributes={"last_triggered": triggered},
            last_changed="",
            last_updated="",
        ),
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    assert _gauges(writer, M_AUTOMATION_LAST_TRIGGERED) == []
    assert _gauges(writer, M_SCRIPT_LAST_TRIGGERED) == []
    assert _gauges(writer, M_AUTOMATION_ENABLED) == []
    assert _gauges(writer, M_PARSE_ERRORS) == []


async def test_ha_error_returns_failed_result() -> None:
    """HaError from get_states -> ok=False, metrics_emitted=0, no writes."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaError())

    result = await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["get_states failed: down"]
    assert writer.recorded == []


async def test_ha_none_returns_failed_result() -> None:
    """ctx.ha is None -> ok=False with 'ha client not configured'."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, None)

    result = await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["ha client not configured"]
    assert writer.recorded == []


async def test_over_cap_drops_series_and_emits_one_suggestion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Over-cap automation last_triggered entities drop to the cap; one warning suggestion."""
    cfg_file = tmp_path / "homelab-monitor.yaml"
    cfg_file.write_text(f"cardinality_caps:\n  families:\n    {M_AUTOMATION_LAST_TRIGGERED}: 5\n")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(cfg_file))

    triggered = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    states = [
        _automation_state(f"automation.e{i:05d}", "on", last_triggered=triggered) for i in range(10)
    ]
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates(states))

    result = await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    lt = _gauges(writer, M_AUTOMATION_LAST_TRIGGERED)
    assert len(lt) == 5  # noqa: PLR2004

    drop = [
        g
        for g in _gauges(writer, _DROP_METRIC)
        if g.labels.get("family") == M_AUTOMATION_LAST_TRIGGERED
    ]
    assert len(drop) == 1
    assert drop[0].value == 5.0  # noqa: PLR2004

    suggestions = [e for e in result.events if isinstance(e, SuggestionEvent)]
    assert len(suggestions) == 1
    assert suggestions[0].severity == "warning"


async def test_empty_states_ok_three_drop_gauges_no_parse_error() -> None:
    """Empty states -> ok=True, three drop gauges (0.0), no parse-error gauge."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer, _FakeHaStates([]))

    result = await HaCadenceCollector().run(ctx)  # type: ignore[arg-type]
    assert result.ok is True

    assert _gauges(writer, M_AUTOMATION_LAST_TRIGGERED) == []
    assert _gauges(writer, M_SCRIPT_LAST_TRIGGERED) == []
    assert _gauges(writer, M_AUTOMATION_ENABLED) == []

    drop = _gauges(writer, _DROP_METRIC)
    assert len(drop) == 3  # noqa: PLR2004
    assert all(g.value == 0.0 for g in drop)
    drop_families = {g.labels["family"] for g in drop}
    assert drop_families == {
        M_AUTOMATION_LAST_TRIGGERED,
        M_SCRIPT_LAST_TRIGGERED,
        M_AUTOMATION_ENABLED,
    }

    assert _gauges(writer, M_PARSE_ERRORS) == []


def test_parse_iso_or_none_non_str_returns_none() -> None:
    """parse_iso_or_none returns None for non-string input."""
    assert parse_iso_or_none(123) is None
    assert parse_iso_or_none(None) is None
    assert parse_iso_or_none("") is None
    assert parse_iso_or_none("not-a-date") is None
