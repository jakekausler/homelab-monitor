"""Tests for :class:`MemoryRetainingMetricsWriter`."""

from __future__ import annotations

from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter

EXPECTED_LATEST_VALUE_AFTER_OVERWRITE = 2.0


def test_write_gauge_records_in_both_storage() -> None:
    writer = MemoryRetainingMetricsWriter()
    writer.write_gauge("test_metric", 1.0, {"a": "1"})
    assert len(writer.recorded) == 1
    snap = writer.snapshot()
    assert len(snap) == 1
    assert snap[0].name == "test_metric"
    assert snap[0].value == 1.0
    assert snap[0].labels == {"a": "1"}
    assert snap[0].kind == "gauge"


def test_write_overwrites_latest_for_same_label_set() -> None:
    writer = MemoryRetainingMetricsWriter()
    writer.write_gauge("test_metric", 1.0, {"a": "1"})
    writer.write_gauge("test_metric", EXPECTED_LATEST_VALUE_AFTER_OVERWRITE, {"a": "1"})
    snap = writer.snapshot()
    assert len(snap) == 1
    assert snap[0].value == EXPECTED_LATEST_VALUE_AFTER_OVERWRITE
    assert len(writer.recorded) == 2  # noqa: PLR2004


def test_write_keeps_separate_latest_for_different_labels() -> None:
    writer = MemoryRetainingMetricsWriter()
    writer.write_gauge("test_metric", 1.0, {"cpu": "all"})
    writer.write_gauge("test_metric", EXPECTED_LATEST_VALUE_AFTER_OVERWRITE, {"cpu": "0"})
    snap = writer.snapshot()
    assert len(snap) == 2  # noqa: PLR2004
    values = {tuple(sorted(e.labels.items())): e.value for e in snap}
    assert values[(("cpu", "0"),)] == EXPECTED_LATEST_VALUE_AFTER_OVERWRITE
    assert values[(("cpu", "all"),)] == 1.0


def test_replace_family_wipes_prior_entries() -> None:
    writer = MemoryRetainingMetricsWriter()
    writer.write_gauge("X", 1.0, {"k": "1"})
    writer.write_gauge("X", 2.0, {"k": "2"})
    writer.replace_family("X", [(99.0, {"k": "3"})])
    snap = [e for e in writer.snapshot() if e.name == "X"]
    assert len(snap) == 1
    assert snap[0].labels == {"k": "3"}
    assert snap[0].value == 99.0  # noqa: PLR2004


def test_replace_family_preserves_other_families() -> None:
    writer = MemoryRetainingMetricsWriter()
    writer.write_gauge("X", 1.0, {"k": "1"})
    writer.write_gauge("Y", 2.0, {"k": "2"})
    writer.replace_family("X", [])
    snap = writer.snapshot()
    names = {e.name for e in snap}
    assert "Y" in names
    assert "X" not in names


def test_replace_family_appends_to_recorded() -> None:
    writer = MemoryRetainingMetricsWriter()
    writer.write_gauge("X", 1.0, {"k": "1"})
    before = len(writer.recorded)
    writer.replace_family("X", [(2.0, {"k": "a"}), (3.0, {"k": "b"})])
    assert len(writer.recorded) == before + 2


def test_snapshot_returns_latest_entries_only() -> None:
    writer = MemoryRetainingMetricsWriter()
    writer.write_gauge("g", 1.0, {})
    writer.write_gauge("g", 5.0, {})
    snap = writer.snapshot()
    assert len(snap) == 1
    assert snap[0].value == 5.0  # noqa: PLR2004


def test_ts_auto_stamped() -> None:
    writer = MemoryRetainingMetricsWriter()
    writer.write_gauge("g", 1.0, {})
    snap = writer.snapshot()
    assert snap[0].ts != ""
    # ISO-8601 UTC offset format from utc_now_iso
    assert snap[0].ts.endswith("+00:00")


def test_inherited_helpers_still_work() -> None:
    writer = MemoryRetainingMetricsWriter()
    writer.write_counter("homelab_collector_run_success_total", 1.0, {"name": "noop"})
    assert writer.last_tick_at_for("noop") is not None


def test_write_counter_and_summary_also_retain_latest() -> None:
    writer = MemoryRetainingMetricsWriter()
    writer.write_counter("c", 1.0, {"a": "1"})
    writer.write_summary("s", 2.0, {"a": "1"})
    snap = writer.snapshot()
    kinds = {e.name: e.kind for e in snap}
    assert kinds["c"] == "counter"
    assert kinds["s"] == "summary"


def test_stress_100_distinct_labels_grow_linearly() -> None:
    writer = MemoryRetainingMetricsWriter()
    for i in range(100):
        writer.write_gauge("g", float(i), {"i": str(i)})
    assert len(writer.snapshot()) == 100  # noqa: PLR2004
    assert len(writer.recorded) == 100  # noqa: PLR2004


def test_stress_100_replace_family_iterations_bounded() -> None:
    writer = MemoryRetainingMetricsWriter()
    for tick in range(100):
        entries: list[tuple[float, dict[str, str]]] = [
            (float(tick * 10 + j), {"pid": str(j)}) for j in range(10)
        ]
        writer.replace_family("topn", entries)
    snap_topn = [e for e in writer.snapshot() if e.name == "topn"]
    assert len(snap_topn) == 10  # noqa: PLR2004
    # last iteration's values: tick=99, so 990..999
    values = {int(e.value) for e in snap_topn}
    assert values == set(range(990, 1000))


def test_last_gauge_returns_none_when_no_match() -> None:
    """last_gauge returns None when no matching gauge was written."""
    writer = MemoryRetainingMetricsWriter()
    assert writer.last_gauge("nonexistent_metric") is None
