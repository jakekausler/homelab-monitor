"""Tests for SilenceDetectionCollector (STAGE-004-038)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sqlalchemy import text
from structlog.testing import CapturingLogger

from homelab_monitor.kernel.config import SilenceDetectionConfig
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.metrics.silence_detection_collector import (
    SilenceDetectionCollector,
)
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryMetricsWriter,
    MemoryRetainingMetricsWriter,
)


def _ctx(
    repo: SqliteRepository,
    writer: MemoryRetainingMetricsWriter | InMemoryMetricsWriter | None = None,
) -> CollectorContext:
    """Bare CollectorContext for testing."""
    return CollectorContext(
        config=None,  # pyright: ignore[reportArgumentType]
        db=repo,
        vm=writer if writer is not None else MemoryRetainingMetricsWriter(),
        vl=None,  # pyright: ignore[reportArgumentType]
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=CapturingLogger(),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


def _entries_for(
    writer: MemoryRetainingMetricsWriter, family: str
) -> list[tuple[float, dict[str, str]]]:
    """Extract (value, labels) tuples for a metric family from a writer."""
    entries: list[tuple[float, dict[str, str]]] = []
    for entry in writer.snapshot():
        if entry.name == family:
            entries.append((entry.value, entry.labels))
    return entries


async def _seed_signature(
    repo_inst: SqliteRepository,
    *,
    template_hash: str,
    service_key: str = "svcA",
    status: str = "active",
    last_seen_at: int,
) -> None:
    async with repo_inst.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO log_signatures "
                "  (template_hash, service_key, template_str, label, status, "
                "   first_seen_at, last_seen_at, total_count) "
                "VALUES (:h, :s, 'tmpl <*>', NULL, :status, 0, :last, 1)"
            ),
            {"h": template_hash, "s": service_key, "status": status, "last": last_seen_at},
        )


async def _seed_allow(  # noqa: PLR0913
    repo_inst: SqliteRepository,
    *,
    template_hash: str | None,
    service_key: str,
    schedule_kind: str,
    schedule_value: str = "",
    expires_at: str | None = None,
) -> None:
    async with repo_inst.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO log_signature_silence_allowlist "
                "  (template_hash, service_key, schedule_kind, schedule_value, "
                "   reason, created_at, expires_at) "
                "VALUES (:h, :s, :kind, :val, 'r', '2026-01-01T00:00:00+00:00', :exp)"
            ),
            {
                "h": template_hash,
                "s": service_key,
                "kind": schedule_kind,
                "val": schedule_value,
                "exp": expires_at,
            },
        )


async def test_silent_signature_emits(repo: SqliteRepository) -> None:
    """30-min-old active sig -> 1 entry emitted."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 1
    assert entries[0] == (1.0, {"service_key": "svcA", "template_hash": "h1"})


async def test_too_recent_absent(repo: SqliteRepository) -> None:
    """5-min-old -> 0 entries."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 5 * 60 * 1000)

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 0


async def test_too_old_absent(repo: SqliteRepository) -> None:
    """90-min-old -> 0 entries."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 90 * 60 * 1000)

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 0


async def test_silent_min_boundary_inclusive(repo: SqliteRepository) -> None:
    """now_ms - 900*1000 - 1000 (just past 15-min min edge, inside window) -> 1 entry."""
    now_ms = int(time.time() * 1000)
    # 1s past the 900s min boundary, safely inside the [900s, 3600s] silent window.
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 900 * 1000 - 1000)

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 1


async def test_suppressed_absent(repo: SqliteRepository) -> None:
    """30-min-old suppressed sig -> 0 entries."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(
        repo, template_hash="h1", status="suppressed", last_seen_at=now_ms - 30 * 60 * 1000
    )

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 0


async def test_allowlist_always_covers(repo: SqliteRepository) -> None:
    """Silent sig + always entry -> 0 entries."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)
    await _seed_allow(repo, template_hash="h1", service_key="svcA", schedule_kind="always")

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 0


async def test_allowlist_window_in_covers(repo: SqliteRepository) -> None:
    """Silent sig + window entry whose range includes now -> 0 entries."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)
    now_dt = datetime.now(UTC)
    start_iso = (now_dt - __import__("datetime").timedelta(hours=1)).isoformat()
    end_iso = (now_dt + __import__("datetime").timedelta(hours=1)).isoformat()
    window_val = f"{start_iso}/{end_iso}"
    await _seed_allow(
        repo,
        template_hash="h1",
        service_key="svcA",
        schedule_kind="window",
        schedule_value=window_val,
    )

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 0


async def test_allowlist_window_out_does_not_cover(repo: SqliteRepository) -> None:
    """Silent sig + window entry in the past -> 1 entry."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)
    await _seed_allow(
        repo,
        template_hash="h1",
        service_key="svcA",
        schedule_kind="window",
        schedule_value="2020-01-01T00:00:00+00:00/2020-01-02T00:00:00+00:00",
    )

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 1


async def test_allowlist_cron_in_grace_does_not_cover(repo: SqliteRepository) -> None:
    """Cron '* * * * *' with grace 100000: always within grace -> NOT covered -> 1 entry."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)
    await _seed_allow(
        repo,
        template_hash="h1",
        service_key="svcA",
        schedule_kind="cron",
        schedule_value="* * * * *",
    )

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig(cron_grace_seconds=100000))
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 1


async def test_allowlist_cron_out_of_grace_covers(repo: SqliteRepository) -> None:
    """Cron '0 0 1 1 *' (yearly) with now mid-year -> allowed -> 0 entries."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)
    await _seed_allow(
        repo,
        template_hash="h1",
        service_key="svcA",
        schedule_kind="cron",
        schedule_value="0 0 1 1 *",
    )

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig(cron_grace_seconds=1))
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 0


async def test_per_service_null_hash_covers(repo: SqliteRepository) -> None:
    """Silent sig + service-wide entry -> 0 entries."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)
    await _seed_allow(repo, template_hash=None, service_key="svcA", schedule_kind="always")

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 0


async def test_per_hash_precedence(repo: SqliteRepository) -> None:
    """Hash-specific entry wins over service-wide -> 0 entries."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)
    # Service-wide window (past)
    await _seed_allow(
        repo,
        template_hash=None,
        service_key="svcA",
        schedule_kind="window",
        schedule_value="2020-01-01T00:00:00+00:00/2020-01-02T00:00:00+00:00",
    )
    # Hash-specific always
    await _seed_allow(repo, template_hash="h1", service_key="svcA", schedule_kind="always")

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 0


async def test_expired_entry_ignored(repo: SqliteRepository) -> None:
    """Silent sig + expired entry -> 1 entry."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)
    await _seed_allow(
        repo,
        template_hash="h1",
        service_key="svcA",
        schedule_kind="always",
        expires_at="2000-01-01T00:00:00+00:00",
    )

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 1


async def test_non_expired_entry_with_future_expiry_covers(repo: SqliteRepository) -> None:
    """Silent sig + always with future expiry -> 0 entries."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)
    await _seed_allow(
        repo,
        template_hash="h1",
        service_key="svcA",
        schedule_kind="always",
        expires_at="2099-12-31T23:59:59+00:00",
    )

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 0


async def test_malformed_schedule_value_does_not_crash_and_does_not_cover(
    repo: SqliteRepository,
) -> None:
    """Malformed window value -> ValueError swallowed -> 1 entry + result.ok is True."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)
    await _seed_allow(
        repo,
        template_hash="h1",
        service_key="svcA",
        schedule_kind="window",
        schedule_value="garbage",
    )

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 1


async def test_malformed_expires_at_not_treated_as_expired(repo: SqliteRepository) -> None:
    """Malformed expires_at -> not treated as expired -> always entry covers -> 0 entries."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)
    await _seed_allow(
        repo,
        template_hash="h1",
        service_key="svcA",
        schedule_kind="always",
        expires_at="not-a-date",
    )

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    entries = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries) == 0


async def test_self_metric_emitted_on_success(repo: SqliteRepository) -> None:
    """Self-metric homelab_collector_run_silence_detection emitted with result=ok."""
    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result = await collector.run(ctx)
    assert result.ok is True
    self_entries = _entries_for(writer, "homelab_collector_run_silence_detection")
    assert any(e[1] == {"phase": "tick", "result": "ok"} for e in self_entries)


async def test_replace_family_self_resolution(repo: SqliteRepository) -> None:
    """Run1 emits 1; UPDATE sig's last_seen_at; run2 -> 0 entries (self-resolves)."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(
        repo, template_hash="h1", service_key="svcA", last_seen_at=now_ms - 30 * 60 * 1000
    )

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result1 = await collector.run(ctx)
    assert result1.ok is True
    entries1 = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries1) == 1

    # UPDATE sig's last_seen_at to now (recovered)
    async with repo.engine.begin() as conn:
        await conn.execute(
            text("UPDATE log_signatures SET last_seen_at = :now WHERE template_hash = 'h1'"),
            {"now": now_ms},
        )

    # Re-run -> 0 entries
    result2 = await collector.run(ctx)
    assert result2.ok is True
    entries2 = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries2) == 0


async def test_no_replace_family_still_ok(repo: SqliteRepository) -> None:
    """InMemoryMetricsWriter (no replace_family) -> result.ok and metrics_emitted >= 2."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    in_memory_writer = InMemoryMetricsWriter()
    ctx = _ctx(repo, in_memory_writer)

    result = await collector.run(ctx)
    assert result.ok is True
    assert result.metrics_emitted >= 2  # noqa: PLR2004


async def test_config_unwired_returns_error() -> None:
    """SilenceDetectionCollector(config=None) -> ok is False, dependencies_unwired error."""
    collector = SilenceDetectionCollector(config=None)
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(AsyncMock(), writer)  # pyright: ignore[reportArgumentType]

    result = await collector.run(ctx)
    assert result.ok is False
    assert "dependencies_unwired" in result.errors
    self_entries = _entries_for(writer, "homelab_collector_run_silence_detection")
    assert any(e[1].get("result") == "dependencies_unwired" for e in self_entries)


async def test_query_failure_returns_error() -> None:
    """Query exception -> ok is False, error in errors list."""
    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(AsyncMock(), writer)  # pyright: ignore[reportArgumentType]
    mock_db = AsyncMock()  # pyright: ignore[reportArgumentType]
    mock_db.fetch_all = AsyncMock(side_effect=Exception("db error"))  # pyright: ignore[reportArgumentType]
    ctx.db = mock_db  # pyright: ignore[reportAttributeAccessIssue]

    result = await collector.run(ctx)
    assert result.ok is False
    assert any("query_failed" in str(e) for e in result.errors)
    self_entries = _entries_for(writer, "homelab_collector_run_silence_detection")
    assert any(e[1].get("result") == "error" for e in self_entries)


async def test_error_tick_preserves_prior_family(repo: SqliteRepository) -> None:
    """Run1 succeeds (1 entry present); run2 fails -> prior family still present."""
    now_ms = int(time.time() * 1000)
    await _seed_signature(repo, template_hash="h1", last_seen_at=now_ms - 30 * 60 * 1000)

    collector = SilenceDetectionCollector(config=SilenceDetectionConfig())
    writer = MemoryRetainingMetricsWriter()
    ctx = _ctx(repo, writer)

    result1 = await collector.run(ctx)
    assert result1.ok is True
    entries1 = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries1) == 1

    # Replace db with failing mock
    mock_db = AsyncMock()  # pyright: ignore[reportArgumentType]
    mock_db.fetch_all = AsyncMock(side_effect=Exception("db error"))  # pyright: ignore[reportArgumentType]
    ctx.db = mock_db  # pyright: ignore[reportAttributeAccessIssue]

    # Run again -> fails but prior family still in writer
    result2 = await collector.run(ctx)
    assert result2.ok is False
    entries2 = _entries_for(writer, "homelab_log_signature_silent")
    assert len(entries2) == 1  # Prior entry still there


__all__: list[str] = []
