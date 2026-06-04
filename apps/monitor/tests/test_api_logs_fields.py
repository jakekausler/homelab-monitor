"""Tests for GET /api/logs/fields (STAGE-004-018)."""

from __future__ import annotations

import re
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock

_FIELD_NAMES_RE = re.compile(r"http://vl-test:9428/select/logsql/field_names.*")
_QUERY_RE = re.compile(r"http://vl-test:9428/select/logsql/query.*")


def _vm_startup(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )


@pytest.mark.asyncio
async def test_fields_requires_session(authenticated_client: AsyncClient) -> None:
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(
            "/api/logs/fields",
            params={"expr": "*", "start": "2026-05-07T00:00:00Z", "end": "2026-05-07T01:00:00Z"},
        )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_fields_happy_path(
    authenticated_client: AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)
    httpx_mock.add_response(
        url=_FIELD_NAMES_RE,
        method="GET",
        json={
            "values": [
                {"value": "_msg", "hits": 10},
                {"value": "level", "hits": 10},
                {"value": "user_id", "hits": 4},
            ]
        },
    )
    httpx_mock.add_response(
        url=_QUERY_RE,
        method="GET",
        text='{"_stream_id":"s","_msg":"m","_time":"t","level":"error","user_id":"42"}\n',
    )
    resp = await authenticated_client.get(
        "/api/logs/fields",
        params={"expr": "*", "start": "2026-05-07T00:00:00Z", "end": "2026-05-07T01:00:00Z"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    names = [f["name"] for f in body["fields"]]
    assert names == ["level", "user_id"]  # coverage desc
    assert body["fields"][0]["coverage"] == 1.0
    assert body["fields"][0]["sample_values"] == ["error"]
    assert body["fields"][1]["type_hint"] == "numeric"
    assert body["sampled_lines"] == 1
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_fields_invalid_window(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get(
        "/api/logs/fields",
        params={"expr": "*", "start": "not-a-date", "end": "2026-05-07T01:00:00Z"},
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_time_format"


@pytest.mark.asyncio
async def test_fields_rejects_long_expr(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get(
        "/api/logs/fields",
        params={"expr": "a" * 5000, "start": "a", "end": "b"},
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_expr"


@pytest.mark.asyncio
async def test_fields_502_on_vl_error(
    authenticated_client: AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homelab_monitor.kernel.api.routers import logs as logs_router  # noqa: PLC0415
    from homelab_monitor.kernel.logs.fields import FieldsCache  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setattr(logs_router, "_fields_cache", FieldsCache(clock=lambda: 0.0))
    _vm_startup(httpx_mock)
    # field_names call fails → fetch_fields raises before query call.
    httpx_mock.add_response(url=_FIELD_NAMES_RE, method="GET", status_code=500, text="err")
    resp = await authenticated_client.get(
        "/api/logs/fields",
        params={"expr": "*", "start": "2026-05-07T00:00:00Z", "end": "2026-05-07T01:00:00Z"},
    )
    assert resp.status_code == 502  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_fields_sample_n_rejected_above_max(
    authenticated_client: AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sample_n above max returns 422 (FastAPI le-validation)."""
    resp = await authenticated_client.get(
        "/api/logs/fields",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "sample_n": 99999,
        },
    )
    assert resp.status_code == 422  # noqa: PLR2004
    monkeypatch.delenv("HOMELAB_MONITOR_VL_URL", raising=False)


@pytest.mark.asyncio
async def test_fields_composes_services(
    authenticated_client: AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)
    httpx_mock.add_response(url=_FIELD_NAMES_RE, method="GET", json={"values": []})
    httpx_mock.add_response(url=_QUERY_RE, method="GET", text="")
    resp = await authenticated_client.get(
        "/api/logs/fields",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "services": "docker:home-assistant",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    fn_req = next(r for r in httpx_mock.get_requests() if "field_names" in r.url.path)
    qp = fn_req.url.params["query"]
    assert 'service:"home-assistant"' in qp
    assert 'source_type:"docker"' in qp


@pytest.mark.asyncio
async def test_fields_excludes_builtins(
    authenticated_client: AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Builtin fields (_msg, _time, _stream_id) are excluded from sample_values."""
    from homelab_monitor.kernel.api.routers import logs as logs_router  # noqa: PLC0415
    from homelab_monitor.kernel.logs.fields import FieldsCache  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setattr(logs_router, "_fields_cache", FieldsCache(clock=lambda: 0.0))
    _vm_startup(httpx_mock)
    httpx_mock.add_response(
        url=_FIELD_NAMES_RE,
        method="GET",
        json={
            "values": [
                {"value": "_time", "hits": 5},
                {"value": "_msg", "hits": 5},
                {"value": "_stream_id", "hits": 5},
                {"value": "custom", "hits": 5},
            ]
        },
    )
    httpx_mock.add_response(
        url=_QUERY_RE,
        method="GET",
        text='{"_stream_id":"s1","_msg":"msg1","_time":"t1","custom":"v1"}\n'
        '{"_stream_id":"s2","_msg":"msg2","_time":"t2","custom":"v2"}\n',
    )
    resp = await authenticated_client.get(
        "/api/logs/fields",
        params={
            "expr": "builtin_test",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    names = [f["name"] for f in body["fields"]]
    assert names == ["custom"]
    assert body["fields"][0]["sample_values"] == ["v1", "v2"]


@pytest.mark.asyncio
async def test_fields_cache_hit(
    authenticated_client: AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second identical request hits cache (no extra VL calls)."""
    from homelab_monitor.kernel.api.routers import logs as logs_router  # noqa: PLC0415
    from homelab_monitor.kernel.logs.fields import FieldsCache  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)
    # Each VL call registered once, non-optional → second request must not call.
    httpx_mock.add_response(
        url=_FIELD_NAMES_RE,
        method="GET",
        json={"values": [{"value": "_msg", "hits": 1}, {"value": "level", "hits": 1}]},
        is_optional=False,
    )
    httpx_mock.add_response(
        url=_QUERY_RE,
        method="GET",
        text='{"_stream_id":"s","_msg":"m","_time":"t","level":"info"}\n',
        is_optional=False,
    )
    monkeypatch.setattr(logs_router, "_fields_cache", FieldsCache(clock=lambda: 0.0))
    params = {"expr": "*", "start": "2026-05-07T00:00:00Z", "end": "2026-05-07T01:00:00Z"}
    r1 = await authenticated_client.get("/api/logs/fields", params=params)
    r2 = await authenticated_client.get("/api/logs/fields", params=params)
    assert r1.status_code == 200  # noqa: PLR2004
    assert r2.status_code == 200  # noqa: PLR2004
    assert r1.json() == r2.json()
