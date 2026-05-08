"""Tests for ``GET /api/metrics/range`` — VictoriaMetrics range proxy."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock


@pytest.mark.asyncio
async def test_range_requires_session(authenticated_client: AsyncClient) -> None:
    """Missing session cookie returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(
            "/api/metrics/range",
            params={
                "expr": "up",
                "start": "2026-05-07T00:00:00+00:00",
                "end": "2026-05-07T00:10:00+00:00",
                "step": "10s",
            },
        )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_range_proxies_vm_success(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: VM returns matrix data; endpoint surfaces it as MetricsRangeResponse."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://vm-test:8428")
    httpx_mock.add_response(
        url="http://vm-test:8428/api/v1/query_range?query=up&start=2026-05-07T00%3A00%3A00%2B00%3A00&end=2026-05-07T00%3A10%3A00%2B00%3A00&step=10s",
        method="GET",
        json={
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"__name__": "up", "instance": "monitor:9090"},
                        "values": [[1714867200.0, "1"], [1714867210.0, "1"]],
                    },
                ],
            },
        },
    )
    resp = await authenticated_client.get(
        "/api/metrics/range",
        params={
            "expr": "up",
            "start": "2026-05-07T00:00:00+00:00",
            "end": "2026-05-07T00:10:00+00:00",
            "step": "10s",
        },
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["status"] == "success"
    assert body["data"]["resultType"] == "matrix"
    assert len(body["data"]["result"]) == 1
    assert body["data"]["result"][0]["metric"]["__name__"] == "up"
    assert body["data"]["result"][0]["values"][0] == [1714867200.0, "1"]


@pytest.mark.asyncio
async def test_range_502_on_vm_error_status(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VM returning 500 surfaces as 502 ``upstream_unavailable``."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://vm-test:8428")
    httpx_mock.add_response(
        url="http://vm-test:8428/api/v1/query_range?query=up&start=a&end=b&step=10s",
        method="GET",
        status_code=500,
        text="vm error",
    )
    resp = await authenticated_client.get(
        "/api/metrics/range",
        params={"expr": "up", "start": "a", "end": "b", "step": "10s"},
    )
    assert resp.status_code == 502  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_range_502_on_transport_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VM transport error (timeout / connection refused) surfaces as 502."""
    import httpx  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://vm-test:8428")
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    resp = await authenticated_client.get(
        "/api/metrics/range",
        params={"expr": "up", "start": "a", "end": "b", "step": "10s"},
    )
    assert resp.status_code == 502  # noqa: PLR2004


@pytest.mark.asyncio
async def test_range_csrf_not_enforced_on_get(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET endpoint accepts requests without an X-CSRF-Token header."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://vm-test:8428")
    httpx_mock.add_response(
        url="http://vm-test:8428/api/v1/query_range?query=up&start=a&end=b&step=10s",
        method="GET",
        json={"status": "success", "data": {"resultType": "matrix", "result": []}},
    )
    resp = await authenticated_client.get(
        "/api/metrics/range",
        params={"expr": "up", "start": "a", "end": "b", "step": "10s"},
        headers={},
    )
    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_range_handles_missing_data_key(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VM response without a 'data' key returns a valid empty result (line 112 fallback)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://vm-test:8428")
    httpx_mock.add_response(
        url="http://vm-test:8428/api/v1/query_range?query=up&start=a&end=b&step=10s",
        method="GET",
        json={"status": "success"},
    )
    resp = await authenticated_client.get(
        "/api/metrics/range",
        params={"expr": "up", "start": "a", "end": "b", "step": "10s"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["data"]["result"] == []


@pytest.mark.asyncio
async def test_range_handles_empty_result_list(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VM response with empty result list produces zero parsed results (branch 124->129)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://vm-test:8428")
    httpx_mock.add_response(
        url="http://vm-test:8428/api/v1/query_range?query=up&start=a&end=b&step=10s",
        method="GET",
        json={
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        },
    )
    resp = await authenticated_client.get(
        "/api/metrics/range",
        params={"expr": "up", "start": "a", "end": "b", "step": "10s"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["data"]["result"] == []


@pytest.mark.asyncio
async def test_range_handles_non_list_values(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Result item with non-list 'values' field falls through to empty values (branch 124->129)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://vm-test:8428")
    httpx_mock.add_response(
        url="http://vm-test:8428/api/v1/query_range?query=up&start=a&end=b&step=10s",
        method="GET",
        json={
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": {"job": "node"}, "values": None},
                ],
            },
        },
    )
    resp = await authenticated_client.get(
        "/api/metrics/range",
        params={"expr": "up", "start": "a", "end": "b", "step": "10s"},
    )
    expected_status = 200
    assert resp.status_code == expected_status
    result = resp.json()["data"]["result"]
    assert len(result) == 1
    assert result[0]["values"] == []


@pytest.mark.asyncio
async def test_range_handles_non_dict_result_item(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-dict items in result list are skipped via 'continue' (line 118)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://vm-test:8428")
    httpx_mock.add_response(
        url="http://vm-test:8428/api/v1/query_range?query=up&start=a&end=b&step=10s",
        method="GET",
        json={
            "status": "success",
            "data": {
                "resultType": "matrix",
                # First item is a string (not a dict), second is valid.
                "result": [
                    "not-a-dict",
                    {
                        "metric": {"__name__": "up"},
                        "values": [[1714867200.0, "1"]],
                    },
                ],
            },
        },
    )
    resp = await authenticated_client.get(
        "/api/metrics/range",
        params={"expr": "up", "start": "a", "end": "b", "step": "10s"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    # Only the valid dict item produced a result.
    assert len(body["data"]["result"]) == 1
    assert body["data"]["result"][0]["metric"]["__name__"] == "up"


@pytest.mark.asyncio
async def test_range_handles_malformed_value_pair(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Value pairs that are not list/tuple or too short are skipped (branch 126->125)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://vm-test:8428")
    httpx_mock.add_response(
        url="http://vm-test:8428/api/v1/query_range?query=up&start=a&end=b&step=10s",
        method="GET",
        json={
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"__name__": "up"},
                        # First pair is a single-element list (too short),
                        # second is a plain string (not list/tuple),
                        # third is valid.
                        "values": [
                            [1714867200.0],
                            "bad-pair",
                            [1714867210.0, "1"],
                        ],
                    },
                ],
            },
        },
    )
    resp = await authenticated_client.get(
        "/api/metrics/range",
        params={"expr": "up", "start": "a", "end": "b", "step": "10s"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    result = body["data"]["result"][0]
    # Only the valid pair survives.
    assert result["values"] == [[1714867210.0, "1"]]


@pytest.mark.asyncio
async def test_range_skips_unparseable_value_pair(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Value pairs with non-numeric timestamps are suppressed by contextlib.suppress."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://vm-test:8428")
    httpx_mock.add_response(
        url="http://vm-test:8428/api/v1/query_range?query=up&start=a&end=b&step=10s",
        method="GET",
        json={
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"__name__": "up"},
                        # First pair: timestamp is not castable to float.
                        # Second pair: valid.
                        "values": [
                            ["not-a-float", "1"],
                            [1714867210.0, "1"],
                        ],
                    },
                ],
            },
        },
    )
    resp = await authenticated_client.get(
        "/api/metrics/range",
        params={"expr": "up", "start": "a", "end": "b", "step": "10s"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    result = body["data"]["result"][0]
    # The unparseable pair is suppressed; only the valid pair remains.
    assert result["values"] == [[1714867210.0, "1"]]
