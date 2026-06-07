"""Unit tests for NewSignatureCollector (STAGE-004-035)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy import text

from homelab_monitor.kernel.config import NewSignatureConfig
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.metrics.new_signature_collector import NewSignatureCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryMetricsWriter,
    LatestMetricEntry,
    MemoryRetainingMetricsWriter,
)


async def _seed_signature(  # noqa: PLR0913
    repo_inst: SqliteRepository,
    *,
    template_hash: str,
    service_key: str = "svcA",
    status: str = "active",
    first_seen_at: int,
    first_seen_severity: str | None,
    last_seen_at: int = 0,
    total_count: int = 1,
) -> None:
    """Seed a single signature row directly via SQL."""
    async with repo_inst.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO log_signatures "
                "  (template_hash, service_key, template_str, label, status, "
                "   first_seen_at, first_seen_severity, last_seen_at, total_count) "
                "VALUES (:h, :s, 'tmpl <*>', NULL, :status, :first, :fss, :last, :tc)"
            ),
            {
                "h": template_hash,
                "s": service_key,
                "status": status,
                "first": first_seen_at,
                "fss": first_seen_severity,
                "last": last_seen_at,
                "tc": total_count,
            },
        )


def _entries_for(writer: MemoryRetainingMetricsWriter, family: str) -> list[LatestMetricEntry]:
    """Extract all snapshot entries for a given metric family name."""
    return [e for e in writer.snapshot() if e.name == family]


def _ctx(
    db: SqliteRepository,
    writer: MemoryRetainingMetricsWriter | InMemoryMetricsWriter,
) -> CollectorContext:
    """Build a minimal CollectorContext for testing."""
    return CollectorContext(
        config=None,  # pyright: ignore[reportArgumentType]
        db=db,
        vm=writer,
        vl=None,  # pyright: ignore[reportArgumentType]
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="new_signature"),  # pyright: ignore[reportArgumentType]
        ha=None,  # pyright: ignore[reportArgumentType]
    )


class TestNewSignatureCollectorBasic:
    """Basic collector behavior tests."""

    @pytest.mark.asyncio
    async def test_new_error_signature_emits_series(self, repo: SqliteRepository) -> None:
        """Recent error signature -> series emitted."""
        writer = MemoryRetainingMetricsWriter()
        ctx = _ctx(repo, writer)
        now_ms = int(time.time() * 1000)
        await _seed_signature(
            repo,
            template_hash="h1",
            service_key="svcA",
            status="active",
            first_seen_at=now_ms,
            first_seen_severity="error",
        )
        config = NewSignatureConfig(window_seconds=300, severities=frozenset({"error"}))
        collector = NewSignatureCollector(config=config)
        result = await collector.run(ctx)
        assert result.ok is True
        entries = _entries_for(writer, "homelab_log_signature_new")
        assert len(entries) == 1
        assert entries[0].labels["service_key"] == "svcA"
        assert entries[0].labels["template_hash"] == "h1"
        assert entries[0].labels["severity"] == "error"
        assert entries[0].value == 1.0

    @pytest.mark.asyncio
    async def test_new_critical_and_warning_signatures_emit(self, repo: SqliteRepository) -> None:
        """Critical and warning signatures in default config both emit."""
        writer = MemoryRetainingMetricsWriter()
        ctx = _ctx(repo, writer)
        now_ms = int(time.time() * 1000)
        await _seed_signature(
            repo,
            template_hash="h2",
            service_key="svcB",
            status="active",
            first_seen_at=now_ms,
            first_seen_severity="critical",
        )
        await _seed_signature(
            repo,
            template_hash="h3",
            service_key="svcC",
            status="active",
            first_seen_at=now_ms,
            first_seen_severity="warning",
        )
        config = NewSignatureConfig(
            window_seconds=300,
            severities=frozenset({"error", "critical", "warning"}),
        )
        collector = NewSignatureCollector(config=config)
        result = await collector.run(ctx)
        assert result.ok is True
        entries = _entries_for(writer, "homelab_log_signature_new")
        assert len(entries) == 2  # noqa: PLR2004
        hashes = {e.labels["template_hash"] for e in entries}
        assert hashes == {"h2", "h3"}

    @pytest.mark.asyncio
    async def test_suppressed_signature_absent(self, repo: SqliteRepository) -> None:
        """Suppressed signature -> no series emitted."""
        writer = MemoryRetainingMetricsWriter()
        ctx = _ctx(repo, writer)
        now_ms = int(time.time() * 1000)
        await _seed_signature(
            repo,
            template_hash="h4",
            service_key="svcD",
            status="suppressed",
            first_seen_at=now_ms,
            first_seen_severity="error",
        )
        config = NewSignatureConfig(window_seconds=300, severities=frozenset({"error"}))
        collector = NewSignatureCollector(config=config)
        result = await collector.run(ctx)
        assert result.ok is True
        entries = _entries_for(writer, "homelab_log_signature_new")
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_out_of_scope_severity_absent(self, repo: SqliteRepository) -> None:
        """Info severity signature not in scope -> no series."""
        writer = MemoryRetainingMetricsWriter()
        ctx = _ctx(repo, writer)
        now_ms = int(time.time() * 1000)
        await _seed_signature(
            repo,
            template_hash="h5",
            service_key="svcE",
            status="active",
            first_seen_at=now_ms,
            first_seen_severity="info",
        )
        config = NewSignatureConfig(window_seconds=300, severities=frozenset({"error"}))
        collector = NewSignatureCollector(config=config)
        result = await collector.run(ctx)
        assert result.ok is True
        entries = _entries_for(writer, "homelab_log_signature_new")
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_old_signature_outside_window_absent(self, repo: SqliteRepository) -> None:
        """Signature older than window -> no series."""
        writer = MemoryRetainingMetricsWriter()
        ctx = _ctx(repo, writer)
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - 10 * 60 * 1000  # 10 minutes ago
        await _seed_signature(
            repo,
            template_hash="h6",
            service_key="svcF",
            status="active",
            first_seen_at=old_ms,
            first_seen_severity="error",
        )
        config = NewSignatureConfig(window_seconds=300, severities=frozenset({"error"}))
        collector = NewSignatureCollector(config=config)
        result = await collector.run(ctx)
        assert result.ok is True
        entries = _entries_for(writer, "homelab_log_signature_new")
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_null_first_seen_severity_skipped(self, repo: SqliteRepository) -> None:
        """Signature with NULL first_seen_severity -> skipped."""
        writer = MemoryRetainingMetricsWriter()
        ctx = _ctx(repo, writer)
        now_ms = int(time.time() * 1000)
        await _seed_signature(
            repo,
            template_hash="h7",
            service_key="svcG",
            status="active",
            first_seen_at=now_ms,
            first_seen_severity=None,
        )
        config = NewSignatureConfig(window_seconds=300, severities=frozenset({"error"}))
        collector = NewSignatureCollector(config=config)
        result = await collector.run(ctx)
        assert result.ok is True
        entries = _entries_for(writer, "homelab_log_signature_new")
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_window_boundary_inclusive(self, repo: SqliteRepository) -> None:
        """Signature just inside the window edge -> emitted.

        Seed 1s inside the window (not at the exact edge): the collector reads its
        own ``_now_ms()`` a few ms after this test captures ``now_ms``, so an
        exact-edge seed would drift just outside the window and flake. 1s of slack
        keeps the assertion deterministic while still exercising the near-boundary
        ``now_ms - first_seen_at <= window_ms`` path.
        """
        writer = MemoryRetainingMetricsWriter()
        ctx = _ctx(repo, writer)
        now_ms = int(time.time() * 1000)
        window_ms = 300 * 1000
        near_edge_ms = now_ms - window_ms + 1000  # 1s inside the window edge
        await _seed_signature(
            repo,
            template_hash="h8",
            service_key="svcH",
            status="active",
            first_seen_at=near_edge_ms,
            first_seen_severity="error",
        )
        config = NewSignatureConfig(window_seconds=300, severities=frozenset({"error"}))
        collector = NewSignatureCollector(config=config)
        result = await collector.run(ctx)
        assert result.ok is True
        entries = _entries_for(writer, "homelab_log_signature_new")
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_self_metric_emitted_on_success(self, repo: SqliteRepository) -> None:
        """Self-metric homelab_collector_run_new_signature emitted with result=ok."""
        writer = MemoryRetainingMetricsWriter()
        ctx = _ctx(repo, writer)
        config = NewSignatureConfig()
        collector = NewSignatureCollector(config=config)
        result = await collector.run(ctx)
        assert result.ok is True
        self_metrics = _entries_for(writer, "homelab_collector_run_new_signature")
        assert len(self_metrics) >= 1
        found = False
        for m in self_metrics:
            if m.labels.get("result") == "ok" and m.labels.get("phase") == "tick":
                found = True
                break
        assert found

    @pytest.mark.asyncio
    async def test_replace_family_self_resolution(self, repo: SqliteRepository) -> None:
        """Signature status change -> series disappears via replace_family."""
        writer = MemoryRetainingMetricsWriter()
        ctx = _ctx(repo, writer)
        now_ms = int(time.time() * 1000)
        await _seed_signature(
            repo,
            template_hash="h9",
            service_key="svcI",
            status="active",
            first_seen_at=now_ms,
            first_seen_severity="error",
        )
        config = NewSignatureConfig(window_seconds=300, severities=frozenset({"error"}))
        collector = NewSignatureCollector(config=config)

        # First run: signature active -> series present
        result1 = await collector.run(ctx)
        assert result1.ok is True
        entries1 = _entries_for(writer, "homelab_log_signature_new")
        assert len(entries1) == 1

        # Suppress the signature directly
        async with repo.transaction() as conn:
            await conn.execute(
                text(
                    "UPDATE log_signatures SET status = 'suppressed' "
                    "WHERE template_hash = :h AND service_key = :s"
                ),
                {"h": "h9", "s": "svcI"},
            )

        # Second run: signature suppressed -> series gone (replace_family cleared it)
        result2 = await collector.run(ctx)
        assert result2.ok is True
        entries2 = _entries_for(writer, "homelab_log_signature_new")
        assert len(entries2) == 0


class TestNewSignatureCollectorNoReplaceFamilyCapability:
    """Test collector behavior when replace_family is unavailable."""

    @pytest.mark.asyncio
    async def test_no_replace_family_still_ok(self, repo: SqliteRepository) -> None:
        """Base InMemoryMetricsWriter without replace_family -> run still ok."""
        writer = InMemoryMetricsWriter()
        ctx = _ctx(repo, writer)
        now_ms = int(time.time() * 1000)
        await _seed_signature(
            repo,
            template_hash="h10",
            service_key="svcJ",
            status="active",
            first_seen_at=now_ms,
            first_seen_severity="error",
        )
        config = NewSignatureConfig(window_seconds=300, severities=frozenset({"error"}))
        collector = NewSignatureCollector(config=config)
        result = await collector.run(ctx)
        assert result.ok is True
        # 1 in-scope family entry (the seeded in-window error signature) + the
        # self-metric. Asserting >= 2 (not >= 1) pins that the entry-building loop
        # actually counted the in-scope row, not just the self-metric.
        assert result.metrics_emitted >= 2  # noqa: PLR2004


class TestNewSignatureCollectorDependenciesUnwired:
    """Test collector when config is unwired."""

    @pytest.mark.asyncio
    async def test_config_unwired_returns_error(self, repo: SqliteRepository) -> None:
        """Config is None -> result error, self-metric emitted with result=dependencies_unwired."""
        writer = MemoryRetainingMetricsWriter()
        ctx = _ctx(repo, writer)
        collector = NewSignatureCollector(config=None)
        result = await collector.run(ctx)
        assert result.ok is False
        assert "dependencies_unwired" in result.errors
        self_metrics = _entries_for(writer, "homelab_collector_run_new_signature")
        assert any(m.labels.get("result") == "dependencies_unwired" for m in self_metrics)


class TestNewSignatureCollectorQueryFailure:
    """Test collector when database query fails."""

    @pytest.mark.asyncio
    async def test_query_failure_returns_error(self, repo: SqliteRepository) -> None:
        """DB query raises exception -> result error, self-metric emitted with result=error."""
        writer = MemoryRetainingMetricsWriter()

        # Mock a db that raises on fetch_all
        failing_db = AsyncMock()
        failing_db.fetch_all = AsyncMock(side_effect=RuntimeError("db connection lost"))
        ctx = _ctx(repo, writer)
        ctx.db = failing_db  # type: ignore[assignment]

        config = NewSignatureConfig(window_seconds=300, severities=frozenset({"error"}))
        collector = NewSignatureCollector(config=config)
        result = await collector.run(ctx)
        assert result.ok is False
        assert "query_failed" in result.errors[0]
        self_metrics = _entries_for(writer, "homelab_collector_run_new_signature")
        assert any(m.labels.get("result") == "error" for m in self_metrics)

    @pytest.mark.asyncio
    async def test_error_tick_preserves_prior_family(self, repo: SqliteRepository) -> None:
        """Transient DB error does NOT wipe a previously-emitted homelab_log_signature_new family.

        replace_family is only called on the success path. An error tick must leave
        the prior family untouched so live alerts are not auto-resolved by a transient
        DB failure.
        """
        writer = MemoryRetainingMetricsWriter()
        ctx = _ctx(repo, writer)
        now_ms = int(time.time() * 1000)
        await _seed_signature(
            repo,
            template_hash="h_failsafe",
            service_key="svc_failsafe",
            status="active",
            first_seen_at=now_ms,
            first_seen_severity="error",
        )
        config = NewSignatureConfig(window_seconds=300, severities=frozenset({"error"}))
        collector = NewSignatureCollector(config=config)

        # First tick: succeeds; family is emitted into the writer
        result1 = await collector.run(ctx)
        assert result1.ok is True
        entries_after_success: list[LatestMetricEntry] = _entries_for(
            writer, "homelab_log_signature_new"
        )
        assert len(entries_after_success) == 1

        # Swap db to a failing mock (reuse the same idiom as test_query_failure_returns_error)
        failing_db = AsyncMock()
        failing_db.fetch_all = AsyncMock(side_effect=RuntimeError("db connection lost"))
        ctx.db = failing_db  # type: ignore[assignment]

        # Second tick: DB error -> no replace_family called
        result2 = await collector.run(ctx)
        assert result2.ok is False

        # CRITICAL: the prior family must still be present; the error tick must not wipe it
        entries_after_error: list[LatestMetricEntry] = _entries_for(
            writer, "homelab_log_signature_new"
        )
        assert len(entries_after_error) == 1


__all__ = []
