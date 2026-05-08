"""Tests for ``MultiplexMetricsWriter``."""

from __future__ import annotations

from homelab_monitor.kernel.metrics.multiplex import MultiplexMetricsWriter
from homelab_monitor.kernel.plugins.io import (
    InMemoryMetricsWriter,
    MemoryRetainingMetricsWriter,
)

_EXPECTED_COUNTER_VALUE = 2.0
_REPLACE_FAMILY_VALUE = 99.0


def test_multiplex_fans_out_gauge() -> None:
    """Every gauge write goes to every inner writer."""
    a = InMemoryMetricsWriter()
    b = InMemoryMetricsWriter()
    mux = MultiplexMetricsWriter([a, b])
    mux.write_gauge("g", 1.0, {"k": "v"})
    assert len(a.recorded) == 1
    assert len(b.recorded) == 1
    assert a.recorded[0].kind == "gauge"
    assert b.recorded[0].kind == "gauge"
    assert a.recorded[0].name == "g"
    assert a.recorded[0].labels == {"k": "v"}


def test_multiplex_fans_out_counter() -> None:
    """Every counter write goes to every inner writer."""
    a = InMemoryMetricsWriter()
    b = InMemoryMetricsWriter()
    mux = MultiplexMetricsWriter([a, b])
    mux.write_counter("c", _EXPECTED_COUNTER_VALUE, {})
    assert a.recorded[0].kind == "counter"
    assert b.recorded[0].kind == "counter"
    assert a.recorded[0].value == _EXPECTED_COUNTER_VALUE


def test_multiplex_fans_out_summary() -> None:
    """Every summary write goes to every inner writer."""
    a = InMemoryMetricsWriter()
    b = InMemoryMetricsWriter()
    mux = MultiplexMetricsWriter([a, b])
    mux.write_summary("s", 3.0, {})
    assert a.recorded[0].kind == "summary"
    assert b.recorded[0].kind == "summary"


def test_multiplex_preserves_registration_order() -> None:
    """Writers are visited in registration order on every fan-out."""
    seen: list[str] = []

    class _Recorder:
        def __init__(self, tag: str) -> None:
            self._tag = tag

        def write_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
            del name, value, labels
            seen.append(self._tag)

        def write_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
            del name, value, labels
            seen.append(self._tag)

        def write_summary(self, name: str, value: float, labels: dict[str, str]) -> None:
            del name, value, labels
            seen.append(self._tag)

    mux = MultiplexMetricsWriter([_Recorder("first"), _Recorder("second")])
    mux.write_gauge("x", 0, {})
    mux.write_counter("x", 0, {})
    mux.write_summary("x", 0, {})
    assert seen == ["first", "second", "first", "second", "first", "second"]


def test_multiplex_replace_family_forwards_to_supporting_writers() -> None:
    """``replace_family`` is delivered to writers that implement it."""
    retain = MemoryRetainingMetricsWriter()
    plain = InMemoryMetricsWriter()
    mux = MultiplexMetricsWriter([retain, plain])
    retain.write_gauge("rf", 1.0, {"k": "old"})
    mux.replace_family("rf", [(_REPLACE_FAMILY_VALUE, {"k": "new"})])
    snap = retain.snapshot()
    rf_entries = [e for e in snap if e.name == "rf"]
    assert len(rf_entries) == 1
    assert rf_entries[0].labels == {"k": "new"}
    assert rf_entries[0].value == _REPLACE_FAMILY_VALUE


def test_multiplex_replace_family_skips_writers_without_method() -> None:
    """Writers without ``replace_family`` are silently skipped — no error."""
    plain_a = InMemoryMetricsWriter()
    plain_b = InMemoryMetricsWriter()
    mux = MultiplexMetricsWriter([plain_a, plain_b])
    # No exception even though neither writer implements replace_family.
    mux.replace_family("rf", [(1.0, {})])
    # Neither writer was called for replace_family (no entries appended via that path).
    # Both plain writers' .recorded lists remain empty.
    assert plain_a.recorded == []
    assert plain_b.recorded == []
