"""Tests for :class:`VictoriaLogsWriter`."""

from __future__ import annotations

import asyncio
import contextlib

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


@pytest.mark.asyncio
async def test_vl_writer_recovers_after_500() -> None:
    """Worker stays alive after a 500; next batch posts successfully."""
    transport_responses = [
        httpx.Response(500, text="server error"),  # first batch fails
        httpx.Response(204),  # second batch succeeds
    ]

    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        resp = transport_responses[min(request_count, len(transport_responses) - 1)]
        request_count += 1
        return resp

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://vl") as client:
        writer = VictoriaLogsWriter(
            vl_url="http://vl",
            http_client=client,
            queue_size=10,
            batch_size=1,
            batch_timeout_s=0.1,
        )
        flusher_task = asyncio.create_task(writer.run_flusher())
        try:
            writer.ingest("svc", "first event")
            await asyncio.sleep(0.5)  # allow first batch to fail
            assert writer.error_count >= 1

            writer.ingest("svc", "second event")
            await asyncio.sleep(0.5)  # allow second batch to succeed
            assert request_count >= 2  # noqa: PLR2004 -- both batches attempted
        finally:
            await writer.aclose()
            with contextlib.suppress(asyncio.CancelledError):
                await flusher_task


@pytest.mark.asyncio
async def test_vl_writer_cancellation_clean() -> None:
    """Cancelling flusher mid-run completes without spurious errors."""
    transport = httpx.MockTransport(lambda req: httpx.Response(204))
    async with httpx.AsyncClient(transport=transport, base_url="http://vl") as client:
        writer = VictoriaLogsWriter(
            vl_url="http://vl",
            http_client=client,
            queue_size=10,
            batch_size=1,
            batch_timeout_s=0.1,
        )
        flusher_task = asyncio.create_task(writer.run_flusher())
        try:
            writer.ingest("svc", "x")
            await asyncio.sleep(0.05)
            flusher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flusher_task
            assert flusher_task.cancelled()
        finally:
            with contextlib.suppress(Exception):
                await writer.aclose()


@pytest.mark.asyncio
async def test_flusher_skips_sentinel_in_drain(httpx_mock: HTTPXMock) -> None:
    """Sentinel items in the drain loop are skipped; only real events are posted."""
    httpx_mock.add_response(
        url=f"{_VL_URL}/insert/jsonline",
        method="POST",
        status_code=200,
    )
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            batch_size=10,
            batch_timeout_s=0.05,
        )
        # Enqueue a real event first, then a sentinel, then another real event.
        writer.ingest("s", "real-one")
        writer._queue.put_nowait({"_sentinel": ""})  # pyright: ignore[reportPrivateUsage]
        writer.ingest("s", "real-two")

        task = asyncio.create_task(writer.run_flusher())
        await asyncio.sleep(0.3)
        await writer.aclose()
        await task

    requests = httpx_mock.get_requests()
    assert len(requests) >= 1
    all_body = "".join(r.read().decode() for r in requests)
    assert "real-one" in all_body
    assert "real-two" in all_body
    assert "_sentinel" not in all_body


@pytest.mark.asyncio
async def test_flusher_handles_unexpected_exception(httpx_mock: HTTPXMock) -> None:
    """An unexpected exception inside _post_batch is counted and flusher continues."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        writer = VictoriaLogsWriter(
            vl_url="http://vl-test",
            http_client=client,
            batch_size=1,
            batch_timeout_s=0.05,
        )
        writer.ingest("s", "first")
        task = asyncio.create_task(writer.run_flusher())
        await asyncio.sleep(0.4)
        await writer.aclose()
        await task

    assert writer.error_count >= 1


@pytest.mark.asyncio
async def test_post_batch_empty_is_noop() -> None:
    """_post_batch with an empty list makes no HTTP requests."""
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            batch_size=10,
            batch_timeout_s=0.05,
        )
        # pytest-httpx will raise if any unexpected request is made.
        await writer._post_batch([])  # pyright: ignore[reportPrivateUsage]
    assert writer.error_count == 0


@pytest.mark.asyncio
async def test_ingest_with_service_and_source_type() -> None:
    """Branch: service + source_type provided -> both added to event."""
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            queue_size=10,
        )
        writer.ingest(
            stream="pihole-queries",
            line='{"query_id": 1}',
            ts="2026-01-01T00:00:00Z",
            service="pihole-queries",
            source_type="pihole",
        )
        assert writer._queue.qsize() == 1  # pyright: ignore[reportPrivateUsage]
        item = writer._queue.get_nowait()  # pyright: ignore[reportPrivateUsage]
        assert item["_msg"] == '{"query_id": 1}'
        assert item["_stream_id"] == "pihole-queries"
        assert item["_time"] == "2026-01-01T00:00:00Z"
        assert item["service"] == "pihole-queries"
        assert item["source_type"] == "pihole"


@pytest.mark.asyncio
async def test_ingest_omits_service_and_source_type_when_not_provided() -> None:
    """Branch: both omitted -> event carries only builtins (backward compat)."""
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            queue_size=10,
        )
        writer.ingest(stream="svc.host", line="hello")
        assert writer._queue.qsize() == 1  # pyright: ignore[reportPrivateUsage]
        item = writer._queue.get_nowait()  # pyright: ignore[reportPrivateUsage]
        assert "service" not in item
        assert "source_type" not in item
        assert item["_msg"] == "hello"
        assert "_time" in item


@pytest.mark.asyncio
async def test_ingest_with_service_only() -> None:
    """Branch: only service provided -> service present, source_type absent."""
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            queue_size=10,
        )
        writer.ingest(stream="s", line="m", service="svc-name")
        assert writer._queue.qsize() == 1  # pyright: ignore[reportPrivateUsage]
        item = writer._queue.get_nowait()  # pyright: ignore[reportPrivateUsage]
        assert item["service"] == "svc-name"
        assert "source_type" not in item


@pytest.mark.asyncio
async def test_ingest_with_source_type_only() -> None:
    """Branch: only source_type provided -> source_type present, service absent."""
    async with httpx.AsyncClient() as client:
        writer = VictoriaLogsWriter(
            vl_url=_VL_URL,
            http_client=client,
            queue_size=10,
        )
        writer.ingest(stream="s", line="m", source_type="pihole")
        assert writer._queue.qsize() == 1  # pyright: ignore[reportPrivateUsage]
        item = writer._queue.get_nowait()  # pyright: ignore[reportPrivateUsage]
        assert item["source_type"] == "pihole"
        assert "service" not in item
