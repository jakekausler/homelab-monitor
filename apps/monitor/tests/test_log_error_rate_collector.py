"""Tests for LogErrorRateCollector (STAGE-004-037)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import httpx
import pytest
import structlog

from homelab_monitor.kernel.config import DEFAULT_ERROR_PATTERNS, ErrorPattern
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VictoriaLogsClientError,
    VlLogLine,
    VlQueryResult,
)
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    MemoryRetainingMetricsWriter,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.builtin.log_error_rate import (
    LogErrorRateCollector,
    _build_error_rate_query,  # pyright: ignore[reportPrivateUsage]
)


class _FakeVlClient:
    """Minimal stand-in for VictoriaLogsClient.query (duck-typed)."""

    def __init__(
        self,
        *,
        result: VlQueryResult | None = None,
        raise_error: bool = False,
    ) -> None:
        self._result = result if result is not None else VlQueryResult(lines=[], truncated=False)
        self._raise = raise_error
        self.calls: list[dict[str, str]] = []

    async def query(self, *, expr: str, start: str, end: str) -> VlQueryResult:
        self.calls.append({"expr": expr, "start": start, "end": end})
        if self._raise:
            raise VictoriaLogsClientError("boom")
        return self._result


def _line(fields: dict[str, str]) -> VlLogLine:
    return VlLogLine(timestamp="", message="", stream="", fields=fields)


def _ctx(
    writer: MemoryRetainingMetricsWriter,
    cfg: CollectorConfig,
    repo: SqliteRepository,
    http_client: httpx.AsyncClient,
) -> CollectorContext:
    return CollectorContext(
        config=cfg,
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=http_client,
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="log_error_rate"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


@pytest.mark.asyncio
async def test_emits_gauge_per_service(repo: SqliteRepository) -> None:
    """Emit one homelab_container_error_rate gauge per service with correct labels."""
    fake_client = _FakeVlClient(
        result=VlQueryResult(
            lines=[
                _line({"service": "svcA", "count": "5"}),
                _line({"service": "svcB", "count": "12"}),
            ],
            truncated=False,
        )
    )
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="log_error_rate")
    async with httpx.AsyncClient() as http_client:
        collector = LogErrorRateCollector(client=cast("VictoriaLogsClient", fake_client))
        result = await collector.run(_ctx(writer, cfg, repo, http_client))

    assert result.ok is True
    assert result.metrics_emitted == 2  # noqa: PLR2004
    snapshot = writer.snapshot()
    gauges = [(e.name, e.value, e.labels) for e in snapshot]
    assert ("homelab_container_error_rate", 5.0, {"name": "svcA"}) in gauges
    assert ("homelab_container_error_rate", 12.0, {"name": "svcB"}) in gauges


@pytest.mark.asyncio
async def test_skips_missing_service(repo: SqliteRepository) -> None:
    """Line with no service key is skipped; metrics_emitted remains 0."""
    fake_client = _FakeVlClient(
        result=VlQueryResult(
            lines=[_line({"count": "5"})],  # Missing service
            truncated=False,
        )
    )
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="log_error_rate")
    async with httpx.AsyncClient() as http_client:
        collector = LogErrorRateCollector(client=cast("VictoriaLogsClient", fake_client))
        result = await collector.run(_ctx(writer, cfg, repo, http_client))

    assert result.ok is True
    assert result.metrics_emitted == 0
    assert writer.snapshot() == []


@pytest.mark.asyncio
async def test_skips_none_count(repo: SqliteRepository) -> None:
    """Line with no count key is skipped."""
    fake_client = _FakeVlClient(
        result=VlQueryResult(
            lines=[_line({"service": "svcA"})],  # Missing count
            truncated=False,
        )
    )
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="log_error_rate")
    async with httpx.AsyncClient() as http_client:
        collector = LogErrorRateCollector(client=cast("VictoriaLogsClient", fake_client))
        result = await collector.run(_ctx(writer, cfg, repo, http_client))

    assert result.ok is True
    assert result.metrics_emitted == 0


@pytest.mark.asyncio
async def test_skips_non_int_count(repo: SqliteRepository) -> None:
    """Line with non-integer count is skipped."""
    fake_client = _FakeVlClient(
        result=VlQueryResult(
            lines=[_line({"service": "svcA", "count": "NaN"})],
            truncated=False,
        )
    )
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="log_error_rate")
    async with httpx.AsyncClient() as http_client:
        collector = LogErrorRateCollector(client=cast("VictoriaLogsClient", fake_client))
        result = await collector.run(_ctx(writer, cfg, repo, http_client))

    assert result.ok is True
    assert result.metrics_emitted == 0


@pytest.mark.asyncio
async def test_skips_empty_string_count(repo: SqliteRepository) -> None:
    """Line with empty-string count is skipped (int('') raises ValueError)."""
    fake_client = _FakeVlClient(
        result=VlQueryResult(
            lines=[_line({"service": "svcEmpty", "count": ""})],
            truncated=False,
        )
    )
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="log_error_rate")
    async with httpx.AsyncClient() as http_client:
        collector = LogErrorRateCollector(client=cast("VictoriaLogsClient", fake_client))
        result = await collector.run(_ctx(writer, cfg, repo, http_client))

    assert result.ok is True
    assert result.metrics_emitted == 0
    assert writer.snapshot() == []


@pytest.mark.asyncio
async def test_empty_result(repo: SqliteRepository) -> None:
    """Empty VL result (no lines) returns ok=True, metrics_emitted=0."""
    fake_client = _FakeVlClient(result=VlQueryResult(lines=[], truncated=False))
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="log_error_rate")
    async with httpx.AsyncClient() as http_client:
        collector = LogErrorRateCollector(client=cast("VictoriaLogsClient", fake_client))
        result = await collector.run(_ctx(writer, cfg, repo, http_client))

    assert result.ok is True
    assert result.metrics_emitted == 0


@pytest.mark.asyncio
async def test_vl_error_returns_not_ok(repo: SqliteRepository) -> None:
    """VL query error -> ok=False with vl_query: error in errors list."""
    fake_client = _FakeVlClient(raise_error=True)
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="log_error_rate")
    async with httpx.AsyncClient() as http_client:
        collector = LogErrorRateCollector(client=cast("VictoriaLogsClient", fake_client))
        result = await collector.run(_ctx(writer, cfg, repo, http_client))

    assert result.ok is False
    assert result.metrics_emitted == 0
    assert any(e.startswith("vl_query:") for e in result.errors)


@pytest.mark.asyncio
async def test_http_unavailable_returns_not_ok(repo: SqliteRepository) -> None:
    """No http_client and no injected client -> ok=False, http_client_unavailable error."""
    writer = MemoryRetainingMetricsWriter()
    # Inject no client; pass a ctx whose http is None (no AsyncClient needed).
    collector = LogErrorRateCollector(client=None)
    ctx = SimpleNamespace(vm=writer, http=None, log=None)
    result = await collector.run(ctx)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.metrics_emitted == 0
    assert "http_client_unavailable" in result.errors


@pytest.mark.asyncio
async def test_resolve_client_builds_from_ctx_http(repo: SqliteRepository) -> None:
    """_resolve_client builds a VictoriaLogsClient from ctx.http when no client injected."""
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="log_error_rate")
    async with httpx.AsyncClient() as http_client:
        collector = LogErrorRateCollector(client=None)
        client = collector._resolve_client(_ctx(writer, cfg, repo, http_client))  # pyright: ignore[reportPrivateUsage]
        assert client is not None


@pytest.mark.asyncio
async def test_resolve_client_uses_injected_http_client(repo: SqliteRepository) -> None:
    """_resolve_client uses explicitly injected http_client."""
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="log_error_rate")
    async with httpx.AsyncClient() as http_client:
        collector = LogErrorRateCollector(client=None, http_client=http_client)
        client = collector._resolve_client(_ctx(writer, cfg, repo, http_client))  # pyright: ignore[reportPrivateUsage]
        assert client is not None


def test_query_default_patterns() -> None:
    """_build_error_rate_query with DEFAULT_ERROR_PATTERNS includes all patterns."""
    query = _build_error_rate_query(DEFAULT_ERROR_PATTERNS)
    assert "severity:error OR severity:critical OR severity:fatal" in query
    assert "_msg:~" in query
    assert "panic" in query
    assert "[Tt]raceback" in query
    assert "[Ee]xception" in query
    assert "| stats by (service) count() as count" in query


def test_query_empty_patterns() -> None:
    """_build_error_rate_query with empty patterns omits _msg:~ clause."""
    query = _build_error_rate_query(())
    assert "severity:error OR severity:critical OR severity:fatal" in query
    assert "_msg:~" not in query
    assert "| stats by (service) count() as count" in query


def test_query_escapes_quote_and_backslash() -> None:
    """_build_error_rate_query escapes quotes and backslashes in regex."""
    patterns = (ErrorPattern(kind="test", regex='a"b\\c'),)
    query = _build_error_rate_query(patterns)
    # The regex fragment should appear as a\"b\\c inside _msg:~"..."
    assert 'a\\"b\\\\c' in query
