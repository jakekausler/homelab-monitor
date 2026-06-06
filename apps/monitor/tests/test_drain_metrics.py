"""Tests for DrainConsumer metrics emission (STAGE-004-027).

Tests for _emit_cycle_metrics, cardinality warn, rising-edge log, and
per-path emission (ok, failed, early-return).
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.drain_consumer import (
    WATERMARK_KEY,
    _now_ms,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.logs.histogram import ms_to_iso
from homelab_monitor.kernel.logs.victorialogs_client import VlLogLine
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter, MetricEntry
from tests.test_drain_consumer import (
    _consumer,  # pyright: ignore[reportPrivateUsage]
    _FakeVlClient,  # pyright: ignore[reportPrivateUsage]
    _vl,  # pyright: ignore[reportPrivateUsage]
)


def _entries(mw: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    return [e for e in mw.recorded if e.name == name]


def _vl_with_severity(msg: str, ts: str, *, service: str = "pihole", severity: str) -> VlLogLine:
    return VlLogLine(
        timestamp=ts,
        message=msg,
        stream="stdout",
        fields={"service": service, "severity": severity},
    )


@pytest.mark.asyncio
async def test_ok_cycle_emits_counters_and_gauges(repo: SqliteRepository) -> None:
    """Normal ok cycle emits all five metric families."""
    lines = [
        _vl("connection accepted from 1.2.3.4", ms_to_iso(1000)),
        _vl("connection accepted from 1.2.3.4", ms_to_iso(1010)),
        _vl("query done", ms_to_iso(1020), service="unbound"),
    ]
    consumer, settings, _p, mw = _consumer(
        repo, _FakeVlClient(lines), batch_max_lines=50, ingest_lag_grace_seconds=0
    )
    await settings.set(WATERMARK_KEY, "1")
    result = await consumer.run_once()
    assert result.cycle_status == "ok"

    # Cycle counters
    lines_total = _entries(mw, "homelab_drain_cycle_lines_total")
    assert len(lines_total) == 1
    assert lines_total[0].value == 3.0  # noqa: PLR2004

    new_tpl = _entries(mw, "homelab_drain_cycle_new_templates_total")
    assert len(new_tpl) == 1
    assert new_tpl[0].value >= 1.0

    dur = _entries(mw, "homelab_drain_cycle_duration_seconds")
    assert len(dur) == 1
    assert dur[0].value >= 0.0

    # Per-sig count: pihole gets 2, unbound gets 1
    sig_counts = _entries(mw, "homelab_log_signature_count")
    assert len(sig_counts) >= 1
    pihole_counts = [e for e in sig_counts if e.labels.get("service_key") == "pihole"]
    assert pihole_counts
    assert pihole_counts[0].value == 2.0  # noqa: PLR2004
    # severity label should be "unknown" since _vl() does not set severity
    assert pihole_counts[0].labels["severity"] == "unknown"

    # Cardinality warn = 0
    card = _entries(mw, "homelab_log_signature_cardinality_warn")
    assert len(card) == 1
    assert card[0].value == 0.0


@pytest.mark.asyncio
async def test_ok_cycle_severity_label_from_field(repo: SqliteRepository) -> None:
    """A line with severity='error' in fields yields severity label 'error'."""
    lines = [_vl_with_severity("disk full", ms_to_iso(2000), service="storage", severity="error")]
    consumer, settings, _p, mw = _consumer(
        repo, _FakeVlClient(lines), batch_max_lines=50, ingest_lag_grace_seconds=0
    )
    await settings.set(WATERMARK_KEY, "1")
    result = await consumer.run_once()
    assert result.cycle_status == "ok"

    sig_counts = _entries(mw, "homelab_log_signature_count")
    storage_counts = [e for e in sig_counts if e.labels.get("service_key") == "storage"]
    assert storage_counts
    assert storage_counts[0].labels["severity"] == "error"


@pytest.mark.asyncio
async def test_first_seen_ns_conversion(repo: SqliteRepository) -> None:
    """homelab_log_signature_first_seen_ts is first_seen_ts_ms * 1_000_000."""
    lines = [_vl("hello", ms_to_iso(5000))]
    consumer, settings, _p, mw = _consumer(
        repo, _FakeVlClient(lines), batch_max_lines=50, ingest_lag_grace_seconds=0
    )
    await settings.set(WATERMARK_KEY, "1")
    await consumer.run_once()

    first_seen_entries = _entries(mw, "homelab_log_signature_first_seen_ts")
    assert first_seen_entries
    # first_seen_ts is stored as ms; metric must be ns (ms * 1_000_000)
    entry = first_seen_entries[0]
    # We can only check the conversion ratio: value must equal some_ms * 1e6
    # i.e. value % 1_000_000 == 0 and value > 0
    assert entry.value > 0.0
    assert entry.value % 1_000_000 == 0.0


@pytest.mark.asyncio
async def test_cardinality_warn_fires_above_threshold(repo: SqliteRepository) -> None:
    """homelab_log_signature_cardinality_warn == 1.0 when distinct sigs > threshold."""
    lines = [
        _vl("message alpha", ms_to_iso(1000), service="svc1"),
        _vl("message beta", ms_to_iso(1010), service="svc2"),
    ]
    consumer, settings, _p, mw = _consumer(
        repo,
        _FakeVlClient(lines),
        batch_max_lines=50,
        ingest_lag_grace_seconds=0,
        signature_cardinality_warn_threshold=1,  # threshold=1, 2 distinct sigs → over
    )
    await settings.set(WATERMARK_KEY, "1")
    result = await consumer.run_once()
    assert result.cycle_status == "ok"

    card = _entries(mw, "homelab_log_signature_cardinality_warn")
    assert card
    assert card[-1].value == 1.0


@pytest.mark.asyncio
async def test_cardinality_warn_logs_once_rising_edge(repo: SqliteRepository) -> None:
    """Rising-edge warning is logged exactly once when transitioning over threshold."""
    # _FakeVlClient yields the same lines regardless of window, so both cycles see the same sigs
    fake = _FakeVlClient(
        [
            _vl("msg alpha", ms_to_iso(1000), service="svc1"),
            _vl("msg beta", ms_to_iso(1010), service="svc2"),
        ]
    )
    consumer, settings, _p, _mw = _consumer(
        repo,
        fake,
        batch_max_lines=50,
        ingest_lag_grace_seconds=0,
        signature_cardinality_warn_threshold=1,
    )
    await settings.set(WATERMARK_KEY, "1")

    # First cycle: over threshold → warning fires
    with capture_logs() as logs1:
        await consumer.run_once()
    warn_logs1 = [e for e in logs1 if e.get("event") == "drain_consumer.signature_cardinality_high"]
    assert len(warn_logs1) == 1

    # Second cycle with same consumer: still over, but already warned → no second warning
    with capture_logs() as logs2:
        await consumer.run_once()
    warn_logs2 = [e for e in logs2 if e.get("event") == "drain_consumer.signature_cardinality_high"]
    assert len(warn_logs2) == 0  # already warned; rising-edge suppressed


@pytest.mark.asyncio
async def test_failed_cycle_emits_partial_metrics(repo: SqliteRepository) -> None:
    """VL-failure mid-stream still emits counters + duration + partial per-sig gauges."""
    lines = [
        _vl("line a", ms_to_iso(1000)),
        _vl("line b", ms_to_iso(1010)),
        _vl("line c", ms_to_iso(1020)),
    ]
    consumer, settings, _p, mw = _consumer(
        repo,
        _FakeVlClient(lines, raise_after=2),
        batch_max_lines=50,
        ingest_lag_grace_seconds=0,
    )
    await settings.set(WATERMARK_KEY, "1")
    result = await consumer.run_once()
    assert result.cycle_status == "failed"
    assert result.lines_processed == 2  # noqa: PLR2004

    lines_total = _entries(mw, "homelab_drain_cycle_lines_total")
    assert lines_total
    assert lines_total[0].value == 2.0  # noqa: PLR2004

    dur = _entries(mw, "homelab_drain_cycle_duration_seconds")
    assert dur

    # Per-sig gauges should exist for lines processed before failure
    sig_counts = _entries(mw, "homelab_log_signature_count")
    assert sig_counts  # at least one partial signature was recorded


@pytest.mark.asyncio
async def test_empty_early_return_emits_only_counters_and_duration(
    repo: SqliteRepository,
) -> None:
    """Early-return path emits only 2 counters (0) + duration; no per-sig or cardinality."""
    consumer, settings, _p, mw = _consumer(
        repo,
        _FakeVlClient([_vl("x", "2999-01-01T00:00:00Z")]),
        ingest_lag_grace_seconds=0,
    )
    # Seed watermark in the far future so query_end_ms <= watermark_ms
    future_watermark = _now_ms() + 600_000
    await settings.set(WATERMARK_KEY, str(future_watermark))

    result = await consumer.run_once()
    assert result.cycle_status == "ok"
    assert result.lines_processed == 0

    # Both counters emitted with 0
    lines_total = _entries(mw, "homelab_drain_cycle_lines_total")
    assert lines_total
    assert lines_total[0].value == 0.0

    new_tpl = _entries(mw, "homelab_drain_cycle_new_templates_total")
    assert new_tpl
    assert new_tpl[0].value == 0.0

    # Duration emitted
    dur = _entries(mw, "homelab_drain_cycle_duration_seconds")
    assert dur

    # NO per-sig gauges, NO cardinality
    assert not _entries(mw, "homelab_log_signature_count")
    assert not _entries(mw, "homelab_log_signature_total")
    assert not _entries(mw, "homelab_log_signature_first_seen_ts")
    assert not _entries(mw, "homelab_log_signature_cardinality_warn")
