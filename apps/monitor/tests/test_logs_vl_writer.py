"""Tests for :class:`VictoriaLogsWriter`."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.logs.vl_writer import VictoriaLogsWriter

_VL_URL = "http://vl-test:9428"


@pytest.mark.asyncio
async def test_ingest_enqueues_event() -> None:
    """ingest() places an event on the internal queue."""
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            loop=asyncio.get_running_loop(),
            queue_size=10,
        )
        writer.ingest("svc.host", "hello")
        assert writer._queue.qsize() == 1  # pyright: ignore[reportPrivateUsage]
        item = writer._queue.get_nowait()  # pyright: ignore[reportPrivateUsage]
        assert item["_msg"] == "hello"
        assert item["_stream_id"] == "svc.host"
        assert "_time" in item


@pytest.mark.asyncio
async def test_ingest_drops_when_queue_full() -> None:
    """Filling the queue then ingest() drops + increments dropped_count."""
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            loop=asyncio.get_running_loop(),
            queue_size=2,
        )
        writer.ingest("s", "1")
        writer.ingest("s", "2")
        assert writer.dropped_count == 0
        writer.ingest("s", "3")  # full -> dropped
        assert writer.dropped_count == 1


@pytest.mark.asyncio
async def test_run_flusher_batches_and_posts(httpx_mock: HTTPXMock) -> None:
    """Worker drains queue and POSTs the batch."""
    httpx_mock.add_response(
        url=f"{_VL_URL}/insert/jsonline",
        method="POST",
        status_code=200,
    )
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            loop=asyncio.get_running_loop(),
            batch_size=10,
            batch_timeout_s=0.05,
        )
        writer.ingest("s", "a")
        writer.ingest("s", "b")
        task = asyncio.create_task(writer.run_flusher())
        # Give worker time to drain + POST.
        await asyncio.sleep(0.2)
        await writer.aclose()
        await task
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1
    body = requests[0].read().decode("utf-8")
    assert '"_msg": "a"' in body
    assert '"_msg": "b"' in body
    assert writer.error_count == 0


@pytest.mark.asyncio
async def test_run_flusher_handles_500_error(httpx_mock: HTTPXMock) -> None:
    """HTTP 500 increments error_count, worker keeps running."""
    httpx_mock.add_response(
        url=f"{_VL_URL}/insert/jsonline",
        method="POST",
        status_code=500,
        text="vl error",
    )
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            loop=asyncio.get_running_loop(),
            batch_size=1,
            batch_timeout_s=0.05,
        )
        writer.ingest("s", "x")
        task = asyncio.create_task(writer.run_flusher())
        await asyncio.sleep(0.2)
        await writer.aclose()
        await task
    assert writer.error_count == 1


@pytest.mark.asyncio
async def test_run_flusher_handles_transport_error(httpx_mock: HTTPXMock) -> None:
    """Transport error increments error_count, worker keeps running."""
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            loop=asyncio.get_running_loop(),
            batch_size=1,
            batch_timeout_s=0.05,
        )
        writer.ingest("s", "x")
        task = asyncio.create_task(writer.run_flusher())
        await asyncio.sleep(0.2)
        await writer.aclose()
        await task
    assert writer.error_count == 1


@pytest.mark.asyncio
async def test_aclose_drains_remaining_queue(httpx_mock: HTTPXMock) -> None:
    """After aclose() the worker exits; queue is fully drained beforehand."""
    httpx_mock.add_response(
        url=f"{_VL_URL}/insert/jsonline",
        method="POST",
        status_code=200,
    )
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            loop=asyncio.get_running_loop(),
            batch_size=100,
            batch_timeout_s=0.05,
        )
        for i in range(5):
            writer.ingest("s", f"line-{i}")
        task = asyncio.create_task(writer.run_flusher())
        await asyncio.sleep(0.1)
        await writer.aclose()
        await task
        assert writer._queue.empty()  # pyright: ignore[reportPrivateUsage]
