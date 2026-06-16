"""Tests for kernel/plugins/process_context.py."""

from __future__ import annotations

import pickle

import pytest

from homelab_monitor.kernel.plugins import (
    BufferingMetricsWriter,
    CollectorConfig,
    ProcessCollectorContext,
)
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver


def test_buffering_metrics_writer_starts_empty() -> None:
    w = BufferingMetricsWriter()
    assert w.drain() == []


_EXPECTED_GAUGE_VALUE = 22.5


def test_buffering_metrics_writer_records_gauge() -> None:
    w = BufferingMetricsWriter()
    w.write_gauge("temperature", _EXPECTED_GAUGE_VALUE, {"location": "room1"})
    drained = w.drain()
    assert len(drained) == 1
    assert drained[0].kind == "gauge"
    assert drained[0].name == "temperature"
    assert drained[0].value == _EXPECTED_GAUGE_VALUE
    assert drained[0].labels == {"location": "room1"}


def test_buffering_metrics_writer_records_counter() -> None:
    w = BufferingMetricsWriter()
    w.write_counter("requests_total", 1.0, {"method": "GET"})
    drained = w.drain()
    assert len(drained) == 1
    assert drained[0].kind == "counter"
    assert drained[0].name == "requests_total"
    assert drained[0].value == 1.0
    assert drained[0].labels == {"method": "GET"}


_EXPECTED_SUMMARY_VALUE = 0.123


def test_buffering_metrics_writer_records_summary() -> None:
    w = BufferingMetricsWriter()
    w.write_summary("response_time_seconds", _EXPECTED_SUMMARY_VALUE, {"endpoint": "/api"})
    drained = w.drain()
    assert len(drained) == 1
    assert drained[0].kind == "summary"
    assert drained[0].name == "response_time_seconds"
    assert drained[0].value == _EXPECTED_SUMMARY_VALUE
    assert drained[0].labels == {"endpoint": "/api"}


_EXPECTED_BUFFER_SIZE = 3


def test_buffering_metrics_writer_drain_clears_buffer() -> None:
    w = BufferingMetricsWriter()
    w.write_gauge("g", 1.0, {})
    w.write_counter("c", 1.0, {})
    w.write_summary("s", 1.0, {})
    assert len(w.drain()) == _EXPECTED_BUFFER_SIZE
    w.write_gauge("g2", 2.0, {})
    drained = w.drain()
    assert len(drained) == 1
    assert drained[0].name == "g2"


def test_buffering_write_counter_absolute_buffers_gauge_kind() -> None:
    """write_counter_absolute buffers a kind='gauge' entry; drain returns it."""
    w = BufferingMetricsWriter()
    w.write_counter_absolute("hl_abs", 77.0, {"d": "x"})
    drained = w.drain()
    assert len(drained) == 1
    assert drained[0].kind == "gauge"
    assert drained[0].name == "hl_abs"
    assert drained[0].value == 77.0  # noqa: PLR2004
    assert drained[0].labels == {"d": "x"}


@pytest.mark.parametrize(
    "method_name",
    ["write_gauge", "write_counter", "write_summary"],
)
def test_buffering_metrics_writer_labels_defensively_copied(method_name: str) -> None:
    """All three writers (gauge/counter/summary) defensively copy labels."""
    w = BufferingMetricsWriter()
    labels = {"k": "v"}
    method = getattr(w, method_name)
    method("metric_name", 1.0, labels)
    # Mutate original dict
    labels["k"] = "MUTATED"
    # Drained entry should have original labels
    drained = w.drain()
    assert drained[0].labels == {"k": "v"}


def test_process_context_constructible() -> None:
    cfg = CollectorConfig(name="test_collector")
    secrets = SyncSecretsResolver(_values={"api_key": "secret"})
    metrics = BufferingMetricsWriter()
    ctx = ProcessCollectorContext(config=cfg, secrets=secrets, metrics=metrics)
    assert ctx.config.name == "test_collector"
    assert ctx.secrets is not None
    assert ctx.metrics is not None


def test_process_context_is_picklable() -> None:
    cfg = CollectorConfig(name="example")
    secrets = SyncSecretsResolver(_values={"k": "v"})
    metrics = BufferingMetricsWriter()
    metrics.write_counter("foo_total", 1.0, {"a": "b"})
    ctx = ProcessCollectorContext(config=cfg, secrets=secrets, metrics=metrics)

    blob = pickle.dumps(ctx)
    restored = pickle.loads(blob)

    assert restored.config.name == "example"
    assert restored.secrets.get("k") == "v"
    drained = restored.metrics.drain()
    assert len(drained) == 1
    assert drained[0].name == "foo_total"
    assert drained[0].kind == "counter"
    assert drained[0].value == 1.0
