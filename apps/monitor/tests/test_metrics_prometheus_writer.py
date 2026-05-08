"""Tests for ``PrometheusRegistryWriter``."""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from homelab_monitor.kernel.metrics.prometheus_writer import PrometheusRegistryWriter


def _expose(reg: CollectorRegistry) -> str:
    return generate_latest(reg).decode()


def test_gauge_set_appears_in_exposition() -> None:
    """A gauge write is visible in the registry's text exposition."""
    reg = CollectorRegistry()
    w = PrometheusRegistryWriter(reg)
    w.write_gauge("hl_gauge", 42.0, {"host": "a"})
    text = _expose(reg)
    assert "hl_gauge" in text
    assert 'host="a"' in text
    assert "42.0" in text


def test_counter_increments_accumulate() -> None:
    """Two counter increments add together for the same labelset."""
    reg = CollectorRegistry()
    w = PrometheusRegistryWriter(reg)
    w.write_counter("hl_counter", 1.0, {"k": "v"})
    w.write_counter("hl_counter", 2.5, {"k": "v"})
    text = _expose(reg)
    # Counter exposition uses the _total suffix in prometheus_client.
    assert 'hl_counter_total{k="v"} 3.5' in text


def test_summary_observe_records_count_and_sum() -> None:
    """Summary observations populate _count and _sum series."""
    reg = CollectorRegistry()
    w = PrometheusRegistryWriter(reg)
    w.write_summary("hl_summary", 0.5, {})
    w.write_summary("hl_summary", 1.5, {})
    text = _expose(reg)
    assert "hl_summary_count 2.0" in text
    assert "hl_summary_sum 2.0" in text


def test_no_labels_path() -> None:
    """Writes with empty labels register a metric with no labelnames."""
    reg = CollectorRegistry()
    w = PrometheusRegistryWriter(reg)
    w.write_gauge("hl_no_labels", 7.0, {})
    text = _expose(reg)
    assert "hl_no_labels 7.0" in text


def test_labelname_mismatch_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """Re-registering the same metric with different label names is skipped + logged."""
    reg = CollectorRegistry()
    w = PrometheusRegistryWriter(reg)
    w.write_gauge("mismatch", 1.0, {"a": "1"})
    # Second call with a DIFFERENT label-name set — must NOT raise + must NOT update.
    w.write_gauge("mismatch", 99.0, {"b": "2"})
    text = _expose(reg)
    # Original labelset still present, value unchanged.
    assert 'mismatch{a="1"} 1.0' in text
    # New labelset NOT present.
    assert 'b="2"' not in text


def test_replace_family_clears_and_rewrites() -> None:
    """``replace_family`` wipes children of a metric and re-emits the new entries."""
    reg = CollectorRegistry()
    w = PrometheusRegistryWriter(reg)
    w.write_gauge("rf", 1.0, {"k": "old"})
    w.replace_family("rf", [(99.0, {"k": "new"})])
    text = _expose(reg)
    assert 'rf{k="new"} 99.0' in text
    assert 'k="old"' not in text


def test_replace_family_on_unregistered_metric_creates_it() -> None:
    """``replace_family`` on a never-seen metric registers it via the entries."""
    reg = CollectorRegistry()
    w = PrometheusRegistryWriter(reg)
    w.replace_family("brandnew", [(5.0, {"x": "y"})])
    text = _expose(reg)
    assert 'brandnew{x="y"} 5.0' in text


def test_replace_family_with_mismatching_entry_drops_that_entry() -> None:
    """If an entry's labelnames mismatch the existing metric, the entry is dropped."""
    reg = CollectorRegistry()
    w = PrometheusRegistryWriter(reg)
    w.write_gauge("mix", 1.0, {"a": "1"})
    # First entry has matching labelnames {"a"}, second does not.
    w.replace_family("mix", [(10.0, {"a": "2"}), (99.0, {"b": "wrong"})])
    text = _expose(reg)
    assert 'mix{a="2"} 10.0' in text
    assert 'b="wrong"' not in text


def test_counter_labelname_mismatch_is_skipped() -> None:
    """Counter write with mismatched label-name set is silently dropped (line 103)."""
    reg = CollectorRegistry()
    w = PrometheusRegistryWriter(reg)
    w.write_counter("cnt_mismatch", 1.0, {"a": "1"})
    # Second call with a DIFFERENT label-name set must NOT raise + must NOT update.
    w.write_counter("cnt_mismatch", 99.0, {"b": "2"})
    text = _expose(reg)
    # Original labelset still present.
    assert 'cnt_mismatch_total{a="1"} 1.0' in text
    # Mismatched labelset NOT present.
    assert 'b="2"' not in text


def test_counter_no_labels_path() -> None:
    """Counter write with empty labels uses the no-labels inc() branch (line 108)."""
    reg = CollectorRegistry()
    w = PrometheusRegistryWriter(reg)
    w.write_counter("cnt_no_labels", 3.0, {})
    text = _expose(reg)
    assert "cnt_no_labels_total 3.0" in text


def test_summary_labelname_mismatch_is_skipped() -> None:
    """Summary write with mismatched label-name set is silently dropped (line 114)."""
    reg = CollectorRegistry()
    w = PrometheusRegistryWriter(reg)
    w.write_summary("sum_mismatch", 1.0, {"a": "1"})
    # Second call with a DIFFERENT label-name set must NOT raise + must NOT update.
    w.write_summary("sum_mismatch", 99.0, {"b": "2"})
    text = _expose(reg)
    # Original observation count still 1.
    assert 'sum_mismatch_count{a="1"} 1.0' in text
    # Mismatched labelset NOT present.
    assert 'b="2"' not in text
