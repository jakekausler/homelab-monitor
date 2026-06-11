"""Tests for ``GET /api/metrics/metric-names`` — VictoriaMetrics __name__ proxy."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock

_VM = "http://vm-test:8428"
_LABEL_URL = f"{_VM}/api/v1/label/__name__/values"


@pytest.mark.asyncio
async def test_metric_names_requires_session(authenticated_client: AsyncClient) -> None:
    """Missing session cookie returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/metrics/metric-names")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_metric_names_success(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: VM returns a list of names; endpoint surfaces them in `names`."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM)
    httpx_mock.add_response(
        url=_LABEL_URL,
        method="GET",
        json={"status": "success", "data": ["up", "node_cpu_seconds_total", "go_goroutines"]},
    )
    resp = await authenticated_client.get("/api/metrics/metric-names")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["names"] == ["up", "node_cpu_seconds_total", "go_goroutines"]


@pytest.mark.asyncio
async def test_metric_names_skips_non_string_items(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-string entries in VM's data list are skipped."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM)
    httpx_mock.add_response(
        url=_LABEL_URL,
        method="GET",
        json={"status": "success", "data": ["up", 123, None, "go_goroutines"]},
    )
    resp = await authenticated_client.get("/api/metrics/metric-names")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["names"] == ["up", "go_goroutines"]


@pytest.mark.asyncio
async def test_metric_names_status_not_success_returns_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 200 with status != 'success' yields an empty names list (no 502)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM)
    httpx_mock.add_response(
        url=_LABEL_URL,
        method="GET",
        json={"status": "error", "error": "boom"},
    )
    resp = await authenticated_client.get("/api/metrics/metric-names")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["names"] == []


@pytest.mark.asyncio
async def test_metric_names_missing_data_returns_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status=success but data missing / non-list yields an empty names list."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM)
    httpx_mock.add_response(
        url=_LABEL_URL,
        method="GET",
        json={"status": "success", "data": "not-a-list"},
    )
    resp = await authenticated_client.get("/api/metrics/metric-names")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["names"] == []


@pytest.mark.asyncio
async def test_metric_names_502_on_vm_error_status(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VM returning HTTP 500 surfaces as 502 ``upstream_unavailable``."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM)
    httpx_mock.add_response(url=_LABEL_URL, method="GET", status_code=500, text="vm error")
    resp = await authenticated_client.get("/api/metrics/metric-names")
    assert resp.status_code == 502  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_metric_names_502_on_transport_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VM transport error (connection refused / timeout) surfaces as 502."""
    import httpx  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM)
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    resp = await authenticated_client.get("/api/metrics/metric-names")
    assert resp.status_code == 502  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_metric_names_csrf_not_enforced_on_get(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET endpoint accepts requests without an X-CSRF-Token header."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM)
    httpx_mock.add_response(
        url=_LABEL_URL,
        method="GET",
        json={"status": "success", "data": []},
    )
    resp = await authenticated_client.get("/api/metrics/metric-names", headers={})
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["names"] == []
