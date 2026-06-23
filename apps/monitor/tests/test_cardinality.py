"""Tests for the per-metric-family cardinality cap (STAGE-005-004)."""

from __future__ import annotations

from homelab_monitor.kernel.config import CardinalityCapsConfig
from homelab_monitor.kernel.metrics.cardinality import (
    M_FAMILY_DROPPED_SERIES,
    CappedEmitter,
    CardinalityCapper,
)
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter, MetricEntry
from homelab_monitor.kernel.plugins.types import CollectorEvent, SuggestionEvent

# Cardinality test fixtures: distinct named counts so asserts are self-documenting.
_DEFAULT_CAP = 500
_UNDER = 10
_SEEN = 1847
_DROPPED = _SEEN - _DEFAULT_CAP  # 1347
_SMALL = 5
_TINY_CAP = 3
_MID = 50


def _obs(n: int) -> list[tuple[dict[str, str], float]]:
    """N observations with labels {"entity": "e0000".."eNNNN"} and value=index."""
    return [({"entity": f"e{i:05d}"}, float(i)) for i in range(n)]


def _gauges_named(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    """Return all gauges recorded with the given name."""
    return [e for e in writer.recorded if e.kind == "gauge" and e.name == name]


# CardinalityCapper tests


def test_capper_under_cap_all_survive() -> None:
    """All observations survive when under cap."""
    capper = CardinalityCapper(cap=_DEFAULT_CAP)
    result = capper.apply(_obs(_UNDER))
    assert len(result.survivors) == _UNDER
    assert result.dropped == 0
    assert result.seen == _UNDER


def test_capper_over_cap_keeps_exactly_cap() -> None:
    """Exactly cap observations survive when over cap."""
    capper = CardinalityCapper(cap=_DEFAULT_CAP)
    result = capper.apply(_obs(_SEEN))
    assert len(result.survivors) == _DEFAULT_CAP
    assert result.dropped == _DROPPED
    assert result.seen == _SEEN


def test_capper_at_cap_boundary() -> None:
    """Exactly at cap."""
    capper = CardinalityCapper(cap=_DEFAULT_CAP)
    result = capper.apply(_obs(_DEFAULT_CAP))
    assert len(result.survivors) == _DEFAULT_CAP
    assert result.dropped == 0
    assert result.seen == _DEFAULT_CAP


def test_capper_empty_input() -> None:
    """Empty input yields empty survivors."""
    capper = CardinalityCapper(cap=500)
    result = capper.apply([])
    assert result.survivors == []
    assert result.dropped == 0
    assert result.seen == 0


def test_capper_cap_zero_drops_all() -> None:
    """Cap of 0 drops all observations."""
    capper = CardinalityCapper(cap=0)
    result = capper.apply(_obs(_SMALL))
    assert result.survivors == []
    assert result.dropped == _SMALL
    assert result.seen == _SMALL


def test_capper_negative_cap_treated_as_zero() -> None:
    """Negative cap is treated as zero."""
    capper = CardinalityCapper(cap=-3)
    result = capper.apply(_obs(_SMALL))
    assert result.survivors == []
    assert result.dropped == _SMALL
    assert result.seen == _SMALL


def test_capper_deterministic_across_input_order() -> None:
    """Same candidates in different order produce same survivors."""
    obs = _obs(_MID)
    obs_reversed = list(reversed(obs))
    capper = CardinalityCapper(cap=_UNDER)
    r1 = capper.apply(obs)
    r2 = capper.apply(obs_reversed)
    assert r1.survivors == r2.survivors


def test_capper_survivors_are_lowest_sorted_keys() -> None:
    """Survivors are the first-N by sorted label order."""
    capper = CardinalityCapper(cap=_TINY_CAP)
    result = capper.apply(_obs(_UNDER))
    entity_labels = [lbl["entity"] for lbl, _ in result.survivors]
    assert entity_labels == ["e00000", "e00001", "e00002"]


# CappedEmitter tests


def test_emit_under_cap_no_suggestion() -> None:
    """Under cap: no suggestion appended."""
    writer = InMemoryMetricsWriter()
    events: list[CollectorEvent] = []
    emitter = CappedEmitter(writer=writer, events=events)
    ret = emitter.emit_family("fam", _DEFAULT_CAP, _obs(_UNDER))
    assert ret == _UNDER
    assert events == []
    assert len(_gauges_named(writer, "fam")) == _UNDER
    assert len(_gauges_named(writer, M_FAMILY_DROPPED_SERIES)) == 1


def test_emit_under_cap_emits_zero_drop_gauge() -> None:
    """Under cap: drop gauge is 0."""
    writer = InMemoryMetricsWriter()
    events: list[CollectorEvent] = []
    emitter = CappedEmitter(writer=writer, events=events)
    emitter.emit_family("fam", _DEFAULT_CAP, _obs(_UNDER))
    drop_gauges = _gauges_named(writer, M_FAMILY_DROPPED_SERIES)
    assert len(drop_gauges) == 1
    assert drop_gauges[0].value == 0.0
    assert drop_gauges[0].labels == {"family": "fam"}


def test_emit_over_cap_one_suggestion() -> None:
    """Over cap: exactly one suggestion appended."""
    writer = InMemoryMetricsWriter()
    events: list[CollectorEvent] = []
    emitter = CappedEmitter(writer=writer, events=events)
    emitter.emit_family("homelab_ha_entity_available", _DEFAULT_CAP, _obs(_SEEN))
    assert len(events) == 1
    assert isinstance(events[0], SuggestionEvent)
    assert events[0].severity == "warning"


def test_emit_over_cap_suggestion_body() -> None:
    """Over cap: suggestion has correct title and body."""
    writer = InMemoryMetricsWriter()
    events: list[CollectorEvent] = []
    emitter = CappedEmitter(writer=writer, events=events)
    emitter.emit_family("homelab_ha_entity_available", _DEFAULT_CAP, _obs(_SEEN))
    assert isinstance(events[0], SuggestionEvent)
    assert (
        events[0].title == "Metric family homelab_ha_entity_available exceeded its cardinality cap"
    )
    assert (
        events[0].body
        == "metric homelab_ha_entity_available exceeded its 500-series budget (1847 seen); raise the cap or narrow the entity filter."  # noqa: E501
    )


def test_emit_over_cap_drop_gauge_value() -> None:
    """Over cap: drop gauge reflects actual dropped count."""
    writer = InMemoryMetricsWriter()
    events: list[CollectorEvent] = []
    emitter = CappedEmitter(writer=writer, events=events)
    emitter.emit_family("homelab_ha_entity_available", _DEFAULT_CAP, _obs(_SEEN))
    drop_gauges = _gauges_named(writer, M_FAMILY_DROPPED_SERIES)
    assert len(drop_gauges) == 1
    assert drop_gauges[0].value == float(_DROPPED)
    assert drop_gauges[0].labels == {"family": "homelab_ha_entity_available"}


def test_emit_over_cap_only_cap_survivors_written() -> None:
    """Over cap: only cap survivors written as metric gauges."""
    writer = InMemoryMetricsWriter()
    events: list[CollectorEvent] = []
    emitter = CappedEmitter(writer=writer, events=events)
    ret = emitter.emit_family("fam", _DEFAULT_CAP, _obs(_SEEN))
    assert ret == _DEFAULT_CAP
    assert len(_gauges_named(writer, "fam")) == _DEFAULT_CAP
    survivors = _gauges_named(writer, "fam")
    # _obs sets value == index; survivors are the lowest-sorted keys e00000.. so
    # their values round-trip as 0.0..(cap-1). Verify the value path, not just count.
    survivor_values = sorted(g.value for g in survivors)
    assert survivor_values == [float(i) for i in range(_DEFAULT_CAP)]
    survivor_entities = {g.labels["entity"] for g in survivors}
    assert f"e{0:05d}" in survivor_entities
    assert f"e{_DEFAULT_CAP - 1:05d}" in survivor_entities
    assert f"e{_DEFAULT_CAP:05d}" not in survivor_entities


def test_emit_returns_survivor_count() -> None:
    """Return value is the count of survivors written."""
    writer = InMemoryMetricsWriter()
    events: list[CollectorEvent] = []
    emitter = CappedEmitter(writer=writer, events=events)
    ret = emitter.emit_family("fam", 0, _obs(_SMALL))
    assert ret == 0
    assert len(_gauges_named(writer, "fam")) == 0
    drop_gauges = _gauges_named(writer, M_FAMILY_DROPPED_SERIES)
    assert len(drop_gauges) == 1
    assert drop_gauges[0].value == float(_SMALL)
    assert len(events) == 1


# CardinalityCapsConfig.cap_for tests (STAGE-007-004 family additions)


def test_cap_for_unifi_client_stats() -> None:
    """The unifi_client_stats family resolves to its configured cap (200)."""
    assert CardinalityCapsConfig().cap_for("unifi_client_stats") == 200  # noqa: PLR2004


def test_cap_for_unifi_dpi() -> None:
    """The unifi_dpi family resolves to its configured cap (100)."""
    assert CardinalityCapsConfig().cap_for("unifi_dpi") == 100  # noqa: PLR2004


def test_cap_for_pihole_client_queries() -> None:
    """The pihole_client_queries family resolves to its configured cap (50)."""
    assert CardinalityCapsConfig().cap_for("pihole_client_queries") == 50  # noqa: PLR2004


def test_cap_for_pihole_top_domains() -> None:
    """The pihole_top_domains family resolves to its configured cap (50)."""
    assert CardinalityCapsConfig().cap_for("pihole_top_domains") == 50  # noqa: PLR2004


def test_cap_for_unknown_family_returns_default() -> None:
    """An unconfigured family falls back to the default cap (500)."""
    assert CardinalityCapsConfig().cap_for("no_such_family") == 500  # noqa: PLR2004
