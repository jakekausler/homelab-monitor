"""Route tests for GET /api/logs/export (STAGE-004-020).

Tests the streaming export endpoint with both txt and json formatters,
error cases, and pre-flight non-200 handling.
"""

from __future__ import annotations

import re

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock

_QUERY_RE = re.compile(r"http://vl-test:9428/select/logsql/query.*")


def _vm_startup(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )


def _ndjson(*objs: str) -> str:
    return "".join(o + "\n" for o in objs)


@pytest.mark.asyncio
async def test_export_requires_session(
    authenticated_client: AsyncClient,
) -> None:
    """Anon client (no session cookie) → 401."""
    from typing import cast  # noqa: PLC0415

    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(
            "/api/logs/export",
            params={
                "expr": "*",
                "start": "2026-05-07T00:00:00Z",
                "end": "2026-05-07T01:00:00Z",
            },
        )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_export_txt_happy_path(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL returns 2 NDJSON lines → 200 with txt format and correct content."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    ndjson = _ndjson(
        '{"_stream_id":"svc.host","_msg":"hello","_time":"2026-05-07T00:00:00Z","severity":"error","service":"nginx"}',
        '{"_stream_id":"svc.host","_msg":"world","_time":"2026-05-07T00:00:01Z","severity":"info","service":"nginx"}',
    )
    httpx_mock.add_response(url=_QUERY_RE, method="GET", text=ndjson)

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "format": "txt",
            "max": 100,
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-disposition"].startswith('attachment; filename="logs_')
    assert resp.headers["content-disposition"].endswith('Z.txt"')

    text = resp.text
    assert text == (
        "2026-05-07T00:00:00Z [error] nginx: hello\n2026-05-07T00:00:01Z [info] nginx: world\n"
    )


@pytest.mark.asyncio
async def test_export_json_happy_path(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL returns 2 NDJSON lines → 200 with json format."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    ndjson = _ndjson(
        '{"_stream_id":"svc.host","_msg":"hello","_time":"2026-05-07T00:00:00Z","severity":"error","service":"nginx"}',
        '{"_stream_id":"svc.host","_msg":"world","_time":"2026-05-07T00:00:01Z","severity":"info","service":"nginx"}',
    )
    httpx_mock.add_response(url=_QUERY_RE, method="GET", text=ndjson)

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "format": "json",
            "max": 100,
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers["content-disposition"].startswith('attachment; filename="logs_')
    assert resp.headers["content-disposition"].endswith('Z.json"')

    body: list[dict[str, object]] = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2  # noqa: PLR2004
    assert body[0]["message"] == "hello"
    assert body[0]["severity"] == "error"
    assert body[0]["service"] == "nginx"


@pytest.mark.asyncio
async def test_export_json_empty_result(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL returns empty body, json format → 200 with b"[]"."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    httpx_mock.add_response(url=_QUERY_RE, method="GET", text="")

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "format": "json",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.text == "[]"


@pytest.mark.asyncio
async def test_export_txt_empty_result(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL returns empty body, txt format → 200 with empty body."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    httpx_mock.add_response(url=_QUERY_RE, method="GET", text="")

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "format": "txt",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.text == ""


@pytest.mark.asyncio
async def test_export_cap_enforced(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL returns 5 NDJSON lines but request max=2 → exactly 2 lines emitted."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    ndjson = _ndjson(
        '{"_stream_id":"s","_msg":"1","_time":"2026-05-07T00:00:00Z"}',
        '{"_stream_id":"s","_msg":"2","_time":"2026-05-07T00:00:01Z"}',
        '{"_stream_id":"s","_msg":"3","_time":"2026-05-07T00:00:02Z"}',
        '{"_stream_id":"s","_msg":"4","_time":"2026-05-07T00:00:03Z"}',
        '{"_stream_id":"s","_msg":"5","_time":"2026-05-07T00:00:04Z"}',
    )
    httpx_mock.add_response(url=_QUERY_RE, method="GET", text=ndjson)

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "format": "txt",
            "max": 2,
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.text.count("\n") == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_export_bad_format_422(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid format param → 422."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "format": "csv",
        },
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_export_max_out_of_range_422(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max out of range [1, 100000] → 422."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "max": 0,
        },
    )
    assert resp.status_code == 422  # noqa: PLR2004

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "max": 100001,
        },
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_export_invalid_window_400(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid time window → 400."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "not-a-date",
            "end": "also-bad",
        },
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_time_format"


@pytest.mark.asyncio
async def test_export_services_composition(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """services param is composed into the query and reaches VL."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    ndjson = _ndjson(
        '{"_stream_id":"s","_msg":"test","_time":"2026-05-07T00:00:00Z","service":"nginx","source_type":"docker"}',
    )
    httpx_mock.add_response(url=_QUERY_RE, method="GET", text=ndjson)

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "services": "docker:nginx",
            "format": "txt",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004

    # Verify the composition reached VL.
    req = httpx_mock.get_requests()[-1]
    query_param = httpx.URL(str(req.url)).params["query"]
    assert "service:" in query_param
    assert "source_type:" in query_param


@pytest.mark.asyncio
async def test_export_502_on_vl_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL returns non-200 → 502 upstream_unavailable (pre-flight)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    httpx_mock.add_response(url=_QUERY_RE, method="GET", status_code=500, text="boom")

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "format": "txt",
        },
    )
    assert resp.status_code == 502  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_export_502_on_transport_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport error from VL → 502."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    httpx_mock.add_exception(httpx.ConnectError("refused"))

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "format": "txt",
        },
    )
    assert resp.status_code == 502  # noqa: PLR2004


@pytest.mark.asyncio
async def test_export_none_severity_and_service_formatting(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Line with no severity and no service → None formatting."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    ndjson = _ndjson('{"_stream_id":"s","_msg":"bare","_time":"2026-05-07T00:00:00Z"}')
    httpx_mock.add_response(url=_QUERY_RE, method="GET", text=ndjson)

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "format": "txt",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.text == "2026-05-07T00:00:00Z [unknown] : bare\n"


@pytest.mark.asyncio
async def test_export_expr_too_long_400(
    authenticated_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """expr longer than 4096 chars → 400 invalid_expr (line 245 coverage)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    oversized_expr = "x" * 4097

    resp = await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": oversized_expr,
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
        },
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_expr"


@pytest.mark.asyncio
async def test_export_sends_limit_param_to_vl(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The export route forwards the requested max as the VL `limit` query param."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    httpx_mock.add_response(url=_QUERY_RE, method="GET", text="")

    await authenticated_client.get(
        "/api/logs/export",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "format": "txt",
            "max": 42,
        },
    )

    vl_requests = [r for r in httpx_mock.get_requests() if "9428" in str(r.url)]
    assert len(vl_requests) >= 1
    sent_limit = httpx.URL(str(vl_requests[-1].url)).params["limit"]
    assert sent_limit == "42"


@pytest.mark.asyncio
async def test_export_base_exception_in_preflight(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-VictoriaLogsClientError during pre-flight anext hits BaseException cleanup branch.

    Covers lines 284-286: ``except BaseException: await source.aclose(); raise``.
    VL HTTP layer succeeds; from_victorialogs_line is patched to raise RuntimeError so
    the _mapped generator propagates through anext() as a non-VL exception.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)

    ndjson = _ndjson(
        '{"_stream_id":"s","_msg":"hello","_time":"2026-05-07T00:00:00Z"}',
    )
    httpx_mock.add_response(url=_QUERY_RE, method="GET", text=ndjson)

    def _boom(_line: object) -> object:
        raise RuntimeError("parse exploded")

    monkeypatch.setattr(
        "homelab_monitor.kernel.api.routers.logs.from_victorialogs_line",
        _boom,
    )

    # The ASGI test transport re-raises the unhandled exception (it is not converted
    # to a 500 response). The `except BaseException: await source.aclose(); raise`
    # cleanup branch still executes before the exception propagates.
    with pytest.raises(RuntimeError, match="parse exploded"):
        await authenticated_client.get(
            "/api/logs/export",
            params={
                "expr": "*",
                "start": "2026-05-07T00:00:00Z",
                "end": "2026-05-07T01:00:00Z",
                "format": "txt",
            },
        )
