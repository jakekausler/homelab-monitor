"""Tests for :class:`LogStreamBudgetCollector`."""

from __future__ import annotations

import httpx
import pytest
import structlog
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    MemoryRetainingMetricsWriter,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.builtin.log_stream_budget import (
    LogStreamBudgetCollector,
    LogStreamState,
)

_VL_URL = "http://vl-test:9428"


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
        log=structlog.get_logger().bind(collector="log_stream_budget"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


@pytest.mark.asyncio
async def test_run_emits_metrics_and_updates_state(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Mock VL stats -> per-stream gauges + state map populated."""
    httpx_mock.add_response(
        url=f"{_VL_URL}/select/logsql/stats",
        method="GET",
        json={
            "streams": [
                {"host": "alice", "service": "nginx", "bytes_today": 100, "lines_per_sec": 5.0},
                {"host": "alice", "service": "ssh", "bytes_today": 50, "lines_per_sec": 1.0},
            ]
        },
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))

    assert result.ok
    assert result.metrics_emitted == 4  # 2 streams * 2 metrics  # noqa: PLR2004
    bytes_entries = {
        (e.labels["host"], e.labels["service"]): e.value
        for e in writer.snapshot()
        if e.name == "homelab_log_stream_bytes_today"
    }
    assert bytes_entries[("alice", "nginx")] == 100.0  # noqa: PLR2004
    assert bytes_entries[("alice", "ssh")] == 50.0  # noqa: PLR2004
    rate_entries = {
        (e.labels["host"], e.labels["service"]): e.value
        for e in writer.snapshot()
        if e.name == "homelab_log_stream_lines_per_sec"
    }
    assert rate_entries[("alice", "nginx")] == 5.0  # noqa: PLR2004
    assert ("alice", "nginx") in state
    assert state[("alice", "nginx")].bytes_today == 100  # noqa: PLR2004


@pytest.mark.asyncio
async def test_run_handles_transport_error(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """VL transport error -> ok=False with vl_transport: error."""
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok is False
    assert any(e.startswith("vl_transport:") for e in result.errors)
    assert state == {}


@pytest.mark.asyncio
async def test_run_handles_non_200(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """VL non-200 -> ok=False with vl_status."""
    httpx_mock.add_response(
        url=f"{_VL_URL}/select/logsql/stats",
        method="GET",
        status_code=503,
        text="busy",
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok is False
    assert any(e.startswith("vl_status:") for e in result.errors)


@pytest.mark.asyncio
async def test_run_handles_malformed_json(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """VL non-JSON body -> ok=False with vl_json."""
    httpx_mock.add_response(
        url=f"{_VL_URL}/select/logsql/stats",
        method="GET",
        text="not-json",
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok is False
    assert any(e.startswith("vl_json:") for e in result.errors)


@pytest.mark.asyncio
async def test_run_skips_items_with_missing_host_or_service(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Items missing host or service are silently skipped."""
    httpx_mock.add_response(
        url=f"{_VL_URL}/select/logsql/stats",
        method="GET",
        json={
            "streams": [
                {"host": "", "service": "x", "bytes_today": 1, "lines_per_sec": 1},
                {"host": "y", "service": "", "bytes_today": 1, "lines_per_sec": 1},
                {"host": "ok", "service": "ok", "bytes_today": 1, "lines_per_sec": 1},
            ]
        },
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok
    assert len(state) == 1
    assert ("ok", "ok") in state


@pytest.mark.asyncio
async def test_run_skips_unparseable_values(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Items with non-numeric bytes/lines are skipped."""
    httpx_mock.add_response(
        url=f"{_VL_URL}/select/logsql/stats",
        method="GET",
        json={
            "streams": [
                {
                    "host": "h",
                    "service": "s",
                    "bytes_today": "not-int",
                    "lines_per_sec": "not-float",
                },
                {"host": "h2", "service": "s2", "bytes_today": 1, "lines_per_sec": 1.0},
            ]
        },
    )
    state: LogStreamState = {}
    async with httpx.AsyncClient() as client:
        collector = LogStreamBudgetCollector(state=state, vl_url=_VL_URL, http_client=client)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="log_stream_budget")
        result = await collector.run(_ctx(writer, cfg, repo, client))
    assert result.ok
    assert len(state) == 1
    assert ("h2", "s2") in state
