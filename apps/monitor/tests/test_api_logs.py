"""Tests for ``GET /api/logs/query`` and ``GET /api/logs/streams``."""

from __future__ import annotations

import re
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock


@pytest.mark.asyncio
async def test_query_requires_session(authenticated_client: AsyncClient) -> None:
    """Missing session cookie returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(
            "/api/logs/query",
            params={
                "expr": "*",
                "start": "2026-05-07T00:00:00+00:00",
                "end": "2026-05-07T00:10:00+00:00",
            },
        )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_query_proxies_vl_success(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: VL returns NDJSON; endpoint parses entries."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    ndjson = (
        '{"_stream_id": "svc.host", "_msg": "hello",'
        ' "_time": "2026-05-07T00:00:00+00:00"}\n'
        '{"_stream_id": "svc.host", "_msg": "world",'
        ' "_time": "2026-05-07T00:00:01+00:00", "level": "info"}\n'
    )
    # Register permissive mock for lifespan startup request to VictoriaMetrics
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )
    httpx_mock.add_response(
        url="http://vl-test:9428/select/logsql/query?query=%2A&start=2026-05-07T00%3A00%3A00Z&end=2026-05-07T01%3A00%3A00Z&limit=101",
        method="GET",
        text=ndjson,
    )
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "limit": 100,
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert len(body["lines"]) == 2  # noqa: PLR2004
    assert body["lines"][0]["stream"] == "svc.host"
    assert body["lines"][0]["message"] == "hello"
    assert body["lines"][1]["fields"]["level"] == "info"


@pytest.mark.asyncio
async def test_query_502_on_vl_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL returning 500 surfaces as 502 ``upstream_unavailable``."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    httpx_mock.add_response(
        url="http://vl-test:9428/select/logsql/query?query=%2A&start=2026-05-07T00%3A00%3A00Z&end=2026-05-07T01%3A00%3A00Z&limit=101",
        method="GET",
        status_code=500,
        text="vl error",
    )
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "limit": 100,
        },
    )
    assert resp.status_code == 502  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_query_502_on_transport_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL transport error surfaces as 502."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "limit": 100,
        },
    )
    assert resp.status_code == 502  # noqa: PLR2004


@pytest.mark.asyncio
async def test_query_rejects_long_expr(
    authenticated_client: AsyncClient,
) -> None:
    """expr > 4096 chars returns 400."""
    long_expr = "a" * 5000
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={"expr": long_expr, "start": "a", "end": "b", "limit": 100},
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_expr"


@pytest.mark.asyncio
async def test_query_tolerates_blank_lines(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty NDJSON lines are skipped silently."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    ndjson = '\n{"_stream_id": "s", "_msg": "x", "_time": "t"}\n\n'
    httpx_mock.add_response(
        url="http://vl-test:9428/select/logsql/query?query=%2A&start=2026-05-07T00%3A00%3A00Z&end=2026-05-07T01%3A00%3A00Z&limit=101",
        method="GET",
        text=ndjson,
    )
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "limit": 100,
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert len(resp.json()["lines"]) == 1


@pytest.mark.asyncio
async def test_query_skips_malformed_json_line(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines that don't parse as JSON are skipped."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    ndjson = 'not-json\n{"_stream_id": "s", "_msg": "ok", "_time": "t"}\n'
    httpx_mock.add_response(
        url="http://vl-test:9428/select/logsql/query?query=%2A&start=2026-05-07T00%3A00%3A00Z&end=2026-05-07T01%3A00%3A00Z&limit=101",
        method="GET",
        text=ndjson,
    )
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "limit": 100,
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert len(resp.json()["lines"]) == 1


@pytest.mark.asyncio
async def test_streams_requires_session(authenticated_client: AsyncClient) -> None:
    """Missing session cookie returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/logs/streams")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_streams_returns_state(authenticated_client: AsyncClient) -> None:
    """The streams endpoint returns the current state map (initially empty)."""
    resp = await authenticated_client.get("/api/logs/streams")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert isinstance(body["streams"], list)


@pytest.mark.asyncio
async def test_streams_returns_populated_state(
    authenticated_client: AsyncClient,
) -> None:
    """When the state is populated, the endpoint returns the entries."""
    from fastapi import FastAPI  # noqa: PLC0415  # pyright: ignore[reportUnusedImport]

    from homelab_monitor.kernel.api.schemas import LogsStreamSummary  # noqa: PLC0415

    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.state.log_stream_state[("hostA", "svcA")] = LogsStreamSummary(
        host="hostA",
        service="svcA",
        last_seen="2026-05-07T00:00:00+00:00",
        lines_per_sec=12.5,
        bytes_today=1024,
    )
    resp = await authenticated_client.get("/api/logs/streams")
    assert resp.status_code == 200  # noqa: PLR2004
    streams = resp.json()["streams"]
    assert len(streams) == 1
    assert streams[0]["host"] == "hostA"
    assert streams[0]["bytes_today"] == 1024  # noqa: PLR2004


@pytest.mark.asyncio
async def test_query_rejects_invalid_timestamp_format(
    authenticated_client: AsyncClient,
) -> None:
    """Non-ISO-8601 start timestamp returns 400 with code 'invalid_time_format'."""
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "not-a-date",
            "end": "2026-05-07T01:00:00Z",
        },
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_time_format"


@pytest.mark.asyncio
async def test_query_rejects_inverted_range(
    authenticated_client: AsyncClient,
) -> None:
    """end before start returns 400 with code 'invalid_range'."""
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-05-07T01:00:00Z",
            "end": "2026-05-07T00:00:00Z",
        },
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_range"


@pytest.mark.asyncio
async def test_query_rejects_range_too_wide(
    authenticated_client: AsyncClient,
) -> None:
    """Range exceeding MAX_RANGE_DAYS (30) returns 400 with code 'range_too_wide'."""
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-04-01T00:00:00Z",  # 90 days
        },
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "range_too_wide"


@pytest.mark.asyncio
async def test_query_accepts_naive_timestamps(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naive (tz-less) ISO timestamps are accepted and normalized to UTC."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    # Register permissive mock for lifespan startup request to VictoriaMetrics
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )
    httpx_mock.add_response(method="GET", text="")
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00",
            "end": "2026-05-07T01:00:00",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_query_skips_non_dict_json_line(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NDJSON lines that parse as non-dict JSON (e.g., arrays) are skipped."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    ndjson = '[1,2,3]\n{"_stream_id": "s", "_msg": "kept", "_time": "2026-05-07T00:00:00+00:00"}\n'
    httpx_mock.add_response(
        url="http://vl-test:9428/select/logsql/query?query=%2A&start=2026-05-07T00%3A00%3A00Z&end=2026-05-07T01%3A00%3A00Z&limit=101",
        method="GET",
        text=ndjson,
    )
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "limit": 100,
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert len(body["lines"]) == 1
    assert body["lines"][0]["message"] == "kept"


@pytest.mark.asyncio
async def test_query_400_on_malformed_cursor(
    authenticated_client: AsyncClient,
) -> None:
    """A malformed cursor returns 400 with code 'invalid_cursor'."""
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "cursor": "!!!garbage!!!",
        },
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_services_requires_session(authenticated_client: AsyncClient) -> None:
    """Missing session cookie returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(
            "/api/logs/services",
            params={
                "start": "2026-05-07T00:00:00Z",
                "end": "2026-05-07T01:00:00Z",
            },
        )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_services_happy_path(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: VL returns services; endpoint parses and sorts."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    ndjson = (
        '{"_stream_id":"","_msg":"","_time":"","service":"nginx","count":"10"}\n'
        '{"_stream_id":"","_msg":"","_time":"","service":"ssh","count":"3"}\n'
    )
    # Register permissive mock for lifespan startup request to VictoriaMetrics
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text=ndjson,
    )
    resp = await authenticated_client.get(
        "/api/logs/services",
        params={
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert len(body["services"]) == 2  # noqa: PLR2004
    assert body["services"][0]["service"] == "nginx"
    assert body["services"][0]["count"] == 10  # noqa: PLR2004
    assert body["services"][1]["service"] == "ssh"
    assert body["services"][1]["count"] == 3  # noqa: PLR2004
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_services_truncated(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When services exceed limit, truncated is True and only top N returned."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    ndjson = (
        '{"_stream_id":"","_msg":"","_time":"","service":"nginx","count":"10"}\n'
        '{"_stream_id":"","_msg":"","_time":"","service":"ssh","count":"3"}\n'
    )
    # Register permissive mock for lifespan startup request to VictoriaMetrics
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text=ndjson,
    )
    resp = await authenticated_client.get(
        "/api/logs/services",
        params={
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "limit": 1,
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert len(body["services"]) == 1
    assert body["services"][0]["service"] == "nginx"
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_services_rejects_invalid_timestamp_format(
    authenticated_client: AsyncClient,
) -> None:
    """Non-ISO-8601 start timestamp returns 400."""
    resp = await authenticated_client.get(
        "/api/logs/services",
        params={
            "start": "not-a-date",
            "end": "2026-05-07T01:00:00Z",
        },
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_time_format"


@pytest.mark.asyncio
async def test_services_rejects_inverted_range(
    authenticated_client: AsyncClient,
) -> None:
    """end before start returns 400."""
    resp = await authenticated_client.get(
        "/api/logs/services",
        params={
            "start": "2026-05-07T01:00:00Z",
            "end": "2026-05-07T00:00:00Z",
        },
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_range"


@pytest.mark.asyncio
async def test_services_rejects_range_too_wide(
    authenticated_client: AsyncClient,
) -> None:
    """Range exceeding MAX_RANGE_DAYS returns 400."""
    resp = await authenticated_client.get(
        "/api/logs/services",
        params={
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-04-01T00:00:00Z",  # 90 days
        },
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "range_too_wide"


@pytest.mark.asyncio
async def test_services_502_on_vl_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL returning 500 surfaces as 502 ``upstream_unavailable``."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        status_code=500,
        text="vl error",
    )
    resp = await authenticated_client.get(
        "/api/logs/services",
        params={
            "start": "2026-05-07T02:00:00Z",
            "end": "2026-05-07T03:00:00Z",
        },
    )
    assert resp.status_code == 502  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_services_skips_rows_missing_service_or_count(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows missing service or count fields are silently skipped."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    ndjson = (
        '{"_stream_id":"","_msg":"","_time":"","service":"","count":"5"}\n'
        '{"_stream_id":"","_msg":"","_time":"","service":"nginx","count":"10"}\n'
        '{"_stream_id":"","_msg":"","_time":"","service":"ssh"}\n'
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text=ndjson,
    )
    resp = await authenticated_client.get(
        "/api/logs/services",
        params={
            "start": "2026-05-07T04:00:00Z",
            "end": "2026-05-07T05:00:00Z",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert len(body["services"]) == 1
    assert body["services"][0]["service"] == "nginx"


@pytest.mark.asyncio
async def test_services_skips_rows_with_non_integer_count(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows with non-integer count are silently skipped (ValueError branch)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    ndjson = (
        '{"_stream_id":"","_msg":"","_time":"","service":"broken","count":"abc"}\n'
        '{"_stream_id":"","_msg":"","_time":"","service":"nginx","count":"7"}\n'
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text=ndjson,
    )
    resp = await authenticated_client.get(
        "/api/logs/services",
        params={
            "start": "2026-05-07T05:00:00Z",
            "end": "2026-05-07T06:00:00Z",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert len(body["services"]) == 1
    assert body["services"][0]["service"] == "nginx"
    assert body["services"][0]["count"] == 7  # noqa: PLR2004


@pytest.mark.asyncio
async def test_services_cache_hit(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identical requests use cache; second request has no VL call."""
    from homelab_monitor.kernel.api.routers import logs as logs_router  # noqa: PLC0415
    from homelab_monitor.kernel.logs.services import (  # noqa: PLC0415
        ServicesCache,
    )

    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    ndjson = '{"_stream_id":"","_msg":"","_time":"","service":"nginx","count":"10"}\n'
    # Register permissive mock for lifespan startup request to VictoriaMetrics
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )
    # Single non-repeating response — second request must hit cache
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text=ndjson,
        is_optional=False,
    )

    # Inject a cache with fixed clock so both requests hit within TTL
    monkeypatch.setattr(logs_router, "_services_cache", ServicesCache(clock=lambda: 0.0))

    params = {
        "start": "2026-05-07T00:00:00Z",
        "end": "2026-05-07T01:00:00Z",
    }

    # First request should hit VL
    resp1 = await authenticated_client.get("/api/logs/services", params=params)
    assert resp1.status_code == 200  # noqa: PLR2004

    # Second request should hit cache (no new VL call)
    resp2 = await authenticated_client.get("/api/logs/services", params=params)
    assert resp2.status_code == 200  # noqa: PLR2004
    assert resp1.json() == resp2.json()


@pytest.mark.asyncio
async def test_query_with_services_composes_filter(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """services param is composed into the query as (service:...) AND (expr)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    ndjson = '{"_stream_id": "svc.host", "_msg": "test", "_time": "2026-05-07T00:00:00+00:00"}\n'
    # Register permissive mock for lifespan startup request to VictoriaMetrics
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )
    # Capture the VL query to verify composition
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text=ndjson,
    )
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "services": "home-assistant",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004

    # Verify the VL call included the composed query
    requests = httpx_mock.get_requests()
    vl_request = next(r for r in requests if "logsql/query" in r.url.path)
    query_param = vl_request.url.params["query"]
    assert 'service:"home-assistant"' in query_param
    assert "AND" in query_param


@pytest.mark.asyncio
async def test_query_without_services_unchanged(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent services param leaves query unchanged."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    ndjson = '{"_stream_id": "svc.host", "_msg": "test", "_time": "2026-05-07T00:00:00+00:00"}\n'
    # Register permissive mock for lifespan startup request to VictoriaMetrics
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text=ndjson,
    )
    resp = await authenticated_client.get(
        "/api/logs/query",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004

    # Verify the VL call has unmodified query
    requests = httpx_mock.get_requests()
    vl_request = next(r for r in requests if "logsql/query" in r.url.path)
    query_param = vl_request.url.params["query"]
    assert query_param == "*"
