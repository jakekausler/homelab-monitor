"""Tests for GET /api/integrations/home-assistant/summary."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock

_VM_URL = "http://vm-test:8428"

# Named constants to avoid PLR2004 in asserts.
_HTTP_OK = 200
_HTTP_UNAUTH = 401
_HTTP_BAD_GATEWAY = 502

# A full planted VM state: query-expression -> count value (as VM string).
# Absent expressions intentionally return an EMPTY vector (the missing-series
# edge case) so the endpoint must default them to 0.
_PLANTED_COUNTS: dict[str, str] = {
    "count(homelab_ha_entity_available)": "42",
    "count(homelab_ha_entity_available == 1)": "40",
    "count(homelab_ha_entity_available == 0)": "2",
    "count(homelab_ha_battery_level < 10)": "1",
    "count(homelab_ha_battery_level >= 10 and homelab_ha_battery_level < 20)": "3",
    "count(homelab_ha_update_available == 1)": "5",
    "count(homelab_ha_update_available)": "12",
    "count(homelab_ha_config_entry_loaded == 1)": "30",
    "count(homelab_ha_config_entry_setup_error == 1)": "1",
    "count(homelab_ha_repair_issue == 1)": "2",
    "count(homelab_ha_persistent_notification == 1)": "4",
}

_HA_UP_TS = 1714867200.0


def _vector_response(value_str: str, ts: float = 1714867200.0) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [ts, value_str]}],
        },
    }


def _empty_vector_response() -> dict[str, object]:
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": []},
    }


def _query_of(request: httpx.Request) -> str:
    qs = parse_qs(urlparse(str(request.url)).query)
    return qs["query"][0]


def _make_callback(
    counts: dict[str, str],
    *,
    ha_up_value: str | None,
    ha_up_ts: float = _HA_UP_TS,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a pytest_httpx callback that answers each instant query.

    counts: expr -> value string. Expressions NOT in `counts` return an empty
    vector (forces the endpoint's default-to-0 path).
    ha_up_value: the homelab_ha_up scalar value string, or None to return an
    empty vector (no homelab_ha_up data -> ha_up=false, last_seen=None).
    """

    def _callback(request: httpx.Request) -> httpx.Response:
        query = _query_of(request)
        if query == "homelab_ha_up":
            if ha_up_value is None:
                return httpx.Response(200, json=_empty_vector_response())
            return httpx.Response(200, json=_vector_response(ha_up_value, ha_up_ts))
        if query in counts:
            return httpx.Response(200, json=_vector_response(counts[query]))
        # Missing-series edge case: empty vector -> field must default to 0.
        return httpx.Response(200, json=_empty_vector_response())

    return _callback


@pytest.mark.asyncio
async def test_summary_requires_session(authenticated_client: AsyncClient) -> None:
    """Missing session cookie returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_UNAUTH


@pytest.mark.asyncio
async def test_summary_aggregates_counts(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: planted VM instant queries produce the correct aggregated shape."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_callback(
        _make_callback(_PLANTED_COUNTS, ha_up_value="1"),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["entities"] == {"total": 42, "available": 40, "unavailable": 2}
    assert body["battery"] == {"low": 3, "critical": 1}
    assert body["updates"] == {"available": 5, "total": 12}
    assert body["config_entries"] == {"loaded": 30, "error": 1}
    assert body["repairs"] == 2  # noqa: PLR2004 -- planted count
    assert body["notifications"] == 4  # noqa: PLR2004 -- planted count
    assert body["ha_up"] is True
    assert body["last_seen"] == datetime.fromtimestamp(_HA_UP_TS, tz=UTC).isoformat()


@pytest.mark.asyncio
async def test_summary_missing_series_defaults_to_zero(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """count(... == 0) returning an EMPTY vector defaults the field to 0 (no 500/KeyError)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    # Empty counts dict -> EVERY count query returns an empty vector.
    httpx_mock.add_callback(
        _make_callback({}, ha_up_value="1"),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["entities"] == {"total": 0, "available": 0, "unavailable": 0}
    assert body["battery"] == {"low": 0, "critical": 0}
    assert body["updates"] == {"available": 0, "total": 0}
    assert body["config_entries"] == {"loaded": 0, "error": 0}
    assert body["repairs"] == 0
    assert body["notifications"] == 0
    assert body["ha_up"] is True  # homelab_ha_up still planted


@pytest.mark.asyncio
async def test_summary_ha_down(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """homelab_ha_up == 0 -> ha_up=false, but counts still populated (VM up)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_callback(
        _make_callback(_PLANTED_COUNTS, ha_up_value="0"),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["ha_up"] is False
    assert body["entities"]["total"] == 42  # noqa: PLR2004 -- planted value
    assert body["last_seen"] is not None  # ha_up sample present -> ts derived


@pytest.mark.asyncio
async def test_summary_last_seen_null_when_ha_up_absent(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """homelab_ha_up returns NO data -> ha_up=false AND last_seen=None."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_callback(
        _make_callback(_PLANTED_COUNTS, ha_up_value=None),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["ha_up"] is False
    assert body["last_seen"] is None


@pytest.mark.asyncio
async def test_summary_502_on_vm_transport_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VM unreachable -> 502 upstream_unavailable (NOT 200-with-zeros)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_exception(httpx.ConnectError("refused"), is_reusable=True)
    resp = await authenticated_client.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_summary_502_on_vm_status_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VM returns HTTP 200 with status='error' -> 502 upstream_unavailable."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_callback(
        lambda _: httpx.Response(
            200,
            json={"status": "error", "errorType": "queryParseError", "error": "boom"},
        ),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_summary_502_on_vm_non_200_status(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VM returns a non-200 HTTP status (e.g. 500) -> 502 upstream_unavailable.

    Covers vm_query.py lines 83-88: the ``if resp.status_code != _HTTP_OK`` branch.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_callback(
        lambda _: httpx.Response(500, text="internal server error"),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_summary_non_dict_result_items_skipped(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """result array containing non-dict items -> items skipped, counts default to 0.

    Covers vm_query.py line 106: ``if not isinstance(item, dict): continue``.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

    def _non_dict_callback(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": ["not-a-dict", 123, None],
                },
            },
        )

    httpx_mock.add_callback(
        _non_dict_callback,
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["entities"] == {"total": 0, "available": 0, "unavailable": 0}
    assert body["battery"] == {"low": 0, "critical": 0}
    assert body["updates"] == {"available": 0, "total": 0}
    assert body["config_entries"] == {"loaded": 0, "error": 0}
    assert body["repairs"] == 0
    assert body["notifications"] == 0
    assert body["ha_up"] is False  # ha_up sample also skipped -> defaults False


@pytest.mark.asyncio
async def test_summary_malformed_value_field_skipped(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """result item whose ``value`` is not a list/tuple or has <2 elements -> skipped.

    Covers vm_query.py line 112: the ``if not isinstance(value_raw, ...) or
    len(value_raw) < _VALUE_PAIR_LEN: continue`` branch.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

    def _bad_value_callback(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    # value is a plain string, not a [ts, val] pair.
                    "result": [{"metric": {}, "value": "not-a-pair"}],
                },
            },
        )

    httpx_mock.add_callback(
        _bad_value_callback,
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["entities"] == {"total": 0, "available": 0, "unavailable": 0}
    assert body["repairs"] == 0
    assert body["notifications"] == 0
    assert body["ha_up"] is False  # ha_up value also malformed -> defaults False


@pytest.mark.asyncio
async def test_summary_non_numeric_count_value_defaults_to_zero(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vm_count receives a well-formed pair but non-numeric value_str -> returns 0.

    Covers vm_query.py lines 144-145: ``except (ValueError, TypeError): return 0``
    in ``vm_count``.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

    def _non_numeric_count_callback(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    # Well-formed [ts, value_str] pair, but value is not numeric.
                    "result": [{"metric": {}, "value": [1718200000, "notanumber"]}],
                },
            },
        )

    httpx_mock.add_callback(
        _non_numeric_count_callback,
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    # All count queries return non-numeric -> vm_count's except branch -> 0.
    assert body["entities"] == {"total": 0, "available": 0, "unavailable": 0}
    assert body["repairs"] == 0
    assert body["notifications"] == 0
    # ha_up also gets "notanumber" -> router's except branch -> ha_up=False.
    assert body["ha_up"] is False


@pytest.mark.asyncio
async def test_summary_non_numeric_ha_up_value_defaults_to_false(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """homelab_ha_up returns a well-formed pair but non-numeric value -> ha_up=False.

    Covers integrations_home_assistant.py lines 141-142: the router-level
    ``except (ValueError, TypeError): ha_up = False`` branch.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

    def _non_numeric_ha_up_callback(request: httpx.Request) -> httpx.Response:
        query = _query_of(request)
        if query == "homelab_ha_up":
            # Structurally valid pair, but value_str cannot be parsed as float.
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [{"metric": {}, "value": [_HA_UP_TS, "bad"]}],
                    },
                },
            )
        # All count queries return normal planted data.
        if query in _PLANTED_COUNTS:
            return httpx.Response(200, json=_vector_response(_PLANTED_COUNTS[query]))
        return httpx.Response(200, json=_empty_vector_response())

    httpx_mock.add_callback(
        _non_numeric_ha_up_callback,
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/summary")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    # The router's except branch fires -> ha_up must be False.
    assert body["ha_up"] is False
    # last_seen is still populated because the ha_up_sample IS present (ts is valid).
    assert body["last_seen"] is not None
    # Count fields reflect the planted data normally.
    assert body["entities"]["total"] == 42  # noqa: PLR2004 -- planted value
