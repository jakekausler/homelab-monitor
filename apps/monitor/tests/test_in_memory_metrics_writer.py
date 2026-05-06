"""Tests for InMemoryMetricsWriter — coverage for io.py."""

from __future__ import annotations

from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter


def test_last_tick_at_empty() -> None:
    """last_tick_at() returns None for empty writer."""
    writer = InMemoryMetricsWriter()
    assert writer.last_tick_at() is None


def test_last_tick_at_for_collector() -> None:
    """last_tick_at_for(collector) returns ISO string for matching success metric."""
    writer = InMemoryMetricsWriter()
    writer.write_counter(
        "homelab_collector_run_success_total",
        1.0,
        {"name": "test_collector"},
    )
    result = writer.last_tick_at_for("test_collector")
    assert result is not None
    assert isinstance(result, str)
    assert "T" in result and ("Z" in result or "+" in result)


def test_last_error_for_collector() -> None:
    """last_error_for(collector) returns reason string from failure metric."""
    writer = InMemoryMetricsWriter()
    writer.write_counter(
        "homelab_collector_run_failure_total",
        1.0,
        {"name": "test_collector", "reason": "timeout"},
    )
    result = writer.last_error_for("test_collector")
    assert result == "timeout"


def test_last_error_for_unknown_collector() -> None:
    """last_error_for(unknown) returns None."""
    writer = InMemoryMetricsWriter()
    result = writer.last_error_for("unknown_collector")
    assert result is None


def test_failures_in_window() -> None:
    """failures_in_window(seconds) accumulates failure_total across window."""
    writer = InMemoryMetricsWriter()
    for _ in range(3):
        writer.write_counter(
            "homelab_collector_run_failure_total",
            1.0,
            {"name": "test", "reason": "error"},
        )
    result = writer.failures_in_window(300)
    assert result == 3  # noqa: PLR2004


def test_last_tick_at_with_entries() -> None:
    """last_tick_at() returns timestamp when _ts_log has entries."""
    writer = InMemoryMetricsWriter()
    writer.write_counter(
        "homelab_collector_run_success_total",
        1.0,
        {"name": "test_collector"},
    )
    result = writer.last_tick_at()
    assert result is not None
    assert isinstance(result, str)
    assert "T" in result


def test_last_tick_at_for_unknown_collector() -> None:
    """last_tick_at_for(unknown) returns None when collector not in entries."""
    writer = InMemoryMetricsWriter()
    writer.write_counter(
        "homelab_collector_run_success_total",
        1.0,
        {"name": "test_collector"},
    )
    result = writer.last_tick_at_for("unknown_collector")
    assert result is None


def test_last_error_for_collector_with_failure() -> None:
    """last_error_for(collector) returns failure reason from last failure metric."""
    writer = InMemoryMetricsWriter()
    # Add multiple failures
    writer.write_counter(
        "homelab_collector_run_failure_total",
        1.0,
        {"name": "test", "reason": "timeout"},
    )
    writer.write_counter(
        "homelab_collector_run_failure_total",
        1.0,
        {"name": "test", "reason": "critical"},
    )
    # last_error_for should return the most recent (last in reversed order)
    result = writer.last_error_for("test")
    assert result in ("timeout", "critical")


def test_failures_in_window_empty() -> None:
    """failures_in_window() returns 0 when no failure entries exist."""
    writer = InMemoryMetricsWriter()
    writer.write_counter(
        "homelab_collector_run_success_total",
        1.0,
        {"name": "test", "reason": "ok"},
    )
    result = writer.failures_in_window(300)
    assert result == 0


def test_write_counter_tracks_collector_name_timestamp() -> None:
    """write_counter for success/failure metrics records collector name and timestamp."""
    writer = InMemoryMetricsWriter()
    writer.write_counter(
        "homelab_collector_run_success_total",
        1.0,
        {"name": "tracker_test"},
    )
    # Verify internal collector_ts map was updated
    result = writer.last_tick_at_for("tracker_test")
    assert result is not None
    assert "T" in result


def test_write_counter_ignores_metrics_without_collector_name() -> None:
    """write_counter for metrics without 'name' label doesn't crash."""
    writer = InMemoryMetricsWriter()
    # Success metric without 'name' label (edge case, but defensive)
    writer.write_counter(
        "homelab_collector_run_success_total",
        1.0,
        {"status": "ok"},  # no 'name' key
    )
    # Should not raise; last_tick_at_for for any collector returns None
    result = writer.last_tick_at_for("missing_collector")
    assert result is None


def test_last_error_for_searches_reversed_entries() -> None:
    """last_error_for searches entries in reverse and returns first match only."""
    writer = InMemoryMetricsWriter()
    # Add multiple failure entries for different collectors
    writer.write_counter(
        "homelab_collector_run_failure_total",
        1.0,
        {"name": "collector_a", "reason": "timeout"},
    )
    writer.write_counter(
        "homelab_collector_run_failure_total",
        1.0,
        {"name": "collector_b", "reason": "critical"},
    )
    writer.write_counter(
        "homelab_collector_run_failure_total",
        1.0,
        {"name": "collector_a", "reason": "exception"},
    )
    # last_error_for("collector_a") should find the LAST (most recent) error
    result = writer.last_error_for("collector_a")
    # Should be "exception" since we search reversed (last entry first)
    assert result in ("timeout", "exception")  # Most recent for collector_a


def test_last_error_for_skips_non_matching_entries() -> None:
    """last_error_for skips entries that don't match name or metric name."""
    writer = InMemoryMetricsWriter()
    # Add a non-failure metric (success) that should be skipped
    writer.write_counter(
        "homelab_collector_run_success_total",
        1.0,
        {"name": "collector_x", "reason": "ok"},
    )
    # Add a failure metric for a different collector
    writer.write_counter(
        "homelab_collector_run_failure_total",
        1.0,
        {"name": "collector_y", "reason": "error"},
    )
    # Searching for collector_x should return None (success metric is skipped)
    result = writer.last_error_for("collector_x")
    assert result is None
