"""Tests for the shared Unifi parsing helpers -- as_float / as_bool / emit_numeric."""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.unifi._parsing import (
    as_bool,
    as_float,
    emit_numeric,
)


def _ctx(writer: InMemoryMetricsWriter) -> CollectorContext:
    """Minimal CollectorContext -- only vm is used by emit_numeric."""
    return CollectorContext(
        config=CollectorConfig(name="unifi_parsing", interval_seconds=30, timeout_seconds=15),
        db=None,  # pyright: ignore[reportArgumentType]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="unifi_parsing"),  # pyright: ignore[reportArgumentType]
        unifi=None,
    )


# ---------------------------------------------------------------------------
# as_float branch coverage
# ---------------------------------------------------------------------------


def test_as_float_bool_excluded() -> None:
    """bool inputs return None (bool must be excluded before int check)."""
    assert as_float(True) is None
    assert as_float(False) is None


def test_as_float_numeric_types() -> None:
    """int and float inputs convert correctly."""
    assert as_float(42) == 42.0  # noqa: PLR2004
    assert as_float(3.14) == 3.14  # noqa: PLR2004


def test_as_float_string_numeric() -> None:
    """String numeric inputs parse correctly."""
    assert as_float("21.9") == 21.9  # noqa: PLR2004
    assert as_float("  0.5 ") == 0.5  # noqa: PLR2004


def test_as_float_unparseable_string() -> None:
    """Non-numeric string returns None."""
    assert as_float("not-a-number") is None
    assert as_float("") is None


def test_as_float_non_numeric_type() -> None:
    """None and other non-numeric types return None."""
    assert as_float(None) is None
    assert as_float([1, 2]) is None
    assert as_float({"a": 1}) is None


# ---------------------------------------------------------------------------
# as_bool branch coverage
# ---------------------------------------------------------------------------


def test_as_bool_true_false() -> None:
    """Bool inputs are returned as-is."""
    assert as_bool(True) is True
    assert as_bool(False) is False


def test_as_bool_non_bool_returns_false() -> None:
    """Non-bool inputs return False."""
    assert as_bool(1) is False
    assert as_bool("True") is False
    assert as_bool(None) is False


# ---------------------------------------------------------------------------
# emit_numeric branch coverage
# ---------------------------------------------------------------------------


def test_emit_numeric_writes_and_increments() -> None:
    """A parseable value is written and the counter is incremented."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer)
    emitted = [0]
    labels: dict[str, str] = {"k": "v"}
    emit_numeric(ctx, "homelab_test_metric", 5, labels, emitted)
    assert emitted[0] == 1
    assert len(writer.recorded) == 1
    assert writer.recorded[0].name == "homelab_test_metric"
    assert writer.recorded[0].value == 5.0  # noqa: PLR2004
    assert writer.recorded[0].labels == {"k": "v"}


def test_emit_numeric_skips_none() -> None:
    """An unparseable value writes nothing and does not increment."""
    writer = InMemoryMetricsWriter()
    ctx = _ctx(writer)
    emitted = [0]
    labels: dict[str, str] = {}
    emit_numeric(ctx, "homelab_test_metric", "not-a-number", labels, emitted)
    assert emitted[0] == 0
    assert writer.recorded == []
