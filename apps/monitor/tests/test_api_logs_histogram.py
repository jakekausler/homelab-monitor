"""Tests for GET /api/logs/histogram (STAGE-004-019)."""

from __future__ import annotations

import re
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock

_HITS_RE = re.compile(r"http://vl-test:9428/select/logsql/hits.*")


def _vm_startup(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://victoriametrics:8428/.*"),
        json={"data": {"resultType": "vector", "result": []}},
        is_optional=True,
    )


def _hits_payload() -> dict[str, object]:
    return {
        "hits": [
            {
                "fields": {"severity": "error"},
                "timestamps": ["2026-05-07T00:00:00Z"],
                "values": [4],
            },
            {
                "fields": {"severity": "info"},
                "timestamps": ["2026-05-07T00:30:00Z"],
                "values": [9],
            },
        ]
    }


@pytest.mark.asyncio
async def test_histogram_requires_session(authenticated_client: AsyncClient) -> None:
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(
            "/api/logs/histogram",
            params={"expr": "*", "start": "2026-05-07T00:00:00Z", "end": "2026-05-07T01:00:00Z"},
        )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_histogram_happy_path(
    authenticated_client: AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homelab_monitor.kernel.api.routers import logs as logs_router  # noqa: PLC0415
    from homelab_monitor.kernel.logs.histogram import HistogramCache  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setattr(logs_router, "_histogram_cache", HistogramCache(clock=lambda: 0.0))
    _vm_startup(httpx_mock)
    httpx_mock.add_response(url=_HITS_RE, method="GET", json=_hits_payload())
    resp = await authenticated_client.get(
        "/api/logs/histogram",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "buckets": 60,
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert "bucket_duration_ms" in body
    assert isinstance(body["buckets"], list)
    # All buckets carry the three coarse keys.
    for b in body["buckets"]:
        assert set(b["counts_by_severity"]) == {"error", "warn", "info"}
    # Aggregate sanity: total error == 4, total info == 9 across all buckets.
    total_error = sum(b["counts_by_severity"]["error"] for b in body["buckets"])
    total_info = sum(b["counts_by_severity"]["info"] for b in body["buckets"])
    assert total_error == 4  # noqa: PLR2004
    assert total_info == 9  # noqa: PLR2004


@pytest.mark.asyncio
async def test_histogram_invalid_window(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get(
        "/api/logs/histogram",
        params={"expr": "*", "start": "not-a-date", "end": "2026-05-07T01:00:00Z"},
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_time_format"


@pytest.mark.asyncio
async def test_histogram_rejects_long_expr(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get(
        "/api/logs/histogram",
        params={"expr": "a" * 5000, "start": "a", "end": "b"},
    )
    assert resp.status_code == 400  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "invalid_expr"


@pytest.mark.asyncio
async def test_histogram_502_on_vl_error(
    authenticated_client: AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homelab_monitor.kernel.api.routers import logs as logs_router  # noqa: PLC0415
    from homelab_monitor.kernel.logs.histogram import HistogramCache  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setattr(logs_router, "_histogram_cache", HistogramCache(clock=lambda: 0.0))
    _vm_startup(httpx_mock)
    httpx_mock.add_response(url=_HITS_RE, method="GET", status_code=500, text="err")
    resp = await authenticated_client.get(
        "/api/logs/histogram",
        params={"expr": "*", "start": "2026-05-07T00:00:00Z", "end": "2026-05-07T01:00:00Z"},
    )
    assert resp.status_code == 502  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_histogram_buckets_rejected_above_max(authenticated_client: AsyncClient) -> None:
    """buckets above max returns 422 (FastAPI le-validation)."""
    resp = await authenticated_client.get(
        "/api/logs/histogram",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "buckets": 99999,
        },
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_histogram_composes_services(
    authenticated_client: AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homelab_monitor.kernel.api.routers import logs as logs_router  # noqa: PLC0415
    from homelab_monitor.kernel.logs.histogram import HistogramCache  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setattr(logs_router, "_histogram_cache", HistogramCache(clock=lambda: 0.0))
    _vm_startup(httpx_mock)
    httpx_mock.add_response(url=_HITS_RE, method="GET", json={"hits": []})
    resp = await authenticated_client.get(
        "/api/logs/histogram",
        params={
            "expr": "*",
            "start": "2026-05-07T00:00:00Z",
            "end": "2026-05-07T01:00:00Z",
            "services": "docker:home-assistant",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    hits_req = next(r for r in httpx_mock.get_requests() if "hits" in r.url.path)
    qp = hits_req.url.params["query"]
    assert 'service:"home-assistant"' in qp
    assert 'source_type:"docker"' in qp


@pytest.mark.asyncio
async def test_histogram_cache_hit(
    authenticated_client: AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second identical request hits cache (no extra VL call)."""
    from homelab_monitor.kernel.api.routers import logs as logs_router  # noqa: PLC0415
    from homelab_monitor.kernel.logs.histogram import HistogramCache  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    _vm_startup(httpx_mock)
    httpx_mock.add_response(url=_HITS_RE, method="GET", json=_hits_payload(), is_optional=False)
    monkeypatch.setattr(logs_router, "_histogram_cache", HistogramCache(clock=lambda: 0.0))
    params = {"expr": "*", "start": "2026-05-07T00:00:00Z", "end": "2026-05-07T01:00:00Z"}
    r1 = await authenticated_client.get("/api/logs/histogram", params=params)
    r2 = await authenticated_client.get("/api/logs/histogram", params=params)
    assert r1.status_code == 200  # noqa: PLR2004
    assert r2.status_code == 200  # noqa: PLR2004
    assert r1.json() == r2.json()
