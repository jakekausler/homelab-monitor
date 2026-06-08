"""Tests for :class:`VlHealthCollector`."""

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
from homelab_monitor.plugins.collectors.builtin.vl_health import (
    VlHealthCollector,
)

_VL_URL = "http://vl-test:9428"
_HEALTH_URL = f"{_VL_URL}/health"


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
        log=structlog.get_logger().bind(collector="vl_health"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


@pytest.mark.asyncio
async def test_healthy_200_emits_up_and_latency(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """GET /health -> 200 -> homelab_vl_up=1.0, latency>0, ok=True."""
    httpx_mock.add_response(url=_HEALTH_URL, method="GET", status_code=200)
    async with httpx.AsyncClient() as client:
        collector = VlHealthCollector(vl_url=_VL_URL, http_client=client, timeout_s=5.0)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="vl_health")
        result = await collector.run(_ctx(writer, cfg, repo, client))

    assert result.ok is True
    assert result.errors == []
    snapshot = {e.name: e for e in writer.snapshot()}
    assert snapshot["homelab_vl_up"].value == 1.0
    assert snapshot["homelab_vl_response_time_seconds"].value >= 0.0
    assert snapshot["homelab_vl_up"].labels == {}
    assert snapshot["homelab_vl_response_time_seconds"].labels == {}


@pytest.mark.asyncio
async def test_non_200_emits_down_and_latency(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """GET /health -> 503 -> homelab_vl_up=0.0 + latency emitted, ok=True."""
    httpx_mock.add_response(url=_HEALTH_URL, method="GET", status_code=503)
    async with httpx.AsyncClient() as client:
        collector = VlHealthCollector(vl_url=_VL_URL, http_client=client, timeout_s=5.0)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="vl_health")
        result = await collector.run(_ctx(writer, cfg, repo, client))

    assert result.ok is True
    snapshot = {e.name: e for e in writer.snapshot()}
    assert snapshot["homelab_vl_up"].value == 0.0
    assert "homelab_vl_response_time_seconds" in snapshot


@pytest.mark.asyncio
async def test_timeout_emits_down_and_latency(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Timeout exception -> homelab_vl_up=0.0 + latency emitted, ok=True."""
    httpx_mock.add_exception(httpx.TimeoutException("timed out"))
    async with httpx.AsyncClient() as client:
        collector = VlHealthCollector(vl_url=_VL_URL, http_client=client, timeout_s=5.0)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="vl_health")
        result = await collector.run(_ctx(writer, cfg, repo, client))

    assert result.ok is True
    snapshot = {e.name: e for e in writer.snapshot()}
    assert snapshot["homelab_vl_up"].value == 0.0
    assert "homelab_vl_response_time_seconds" in snapshot


@pytest.mark.asyncio
async def test_connect_error_emits_down_and_latency(
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
) -> None:
    """Transport error (ConnectError) -> homelab_vl_up=0.0 + latency, ok=True."""
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    async with httpx.AsyncClient() as client:
        collector = VlHealthCollector(vl_url=_VL_URL, http_client=client, timeout_s=5.0)
        writer = MemoryRetainingMetricsWriter()
        cfg = CollectorConfig(name="vl_health")
        result = await collector.run(_ctx(writer, cfg, repo, client))

    assert result.ok is True
    snapshot = {e.name: e for e in writer.snapshot()}
    assert snapshot["homelab_vl_up"].value == 0.0
    assert "homelab_vl_response_time_seconds" in snapshot


@pytest.mark.asyncio
async def test_no_http_client_returns_error(
    repo: SqliteRepository,
) -> None:
    """No http_client and ctx.http=None -> ok=False, http_client_unavailable."""
    collector = VlHealthCollector(vl_url=_VL_URL, http_client=None, timeout_s=5.0)
    writer = MemoryRetainingMetricsWriter()
    cfg = CollectorConfig(name="vl_health")
    ctx = CollectorContext(
        config=cfg,
        db=repo,
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="vl_health"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )
    result = await collector.run(ctx)
    assert result.ok is False
    assert "http_client_unavailable" in result.errors
    assert writer.snapshot() == []
