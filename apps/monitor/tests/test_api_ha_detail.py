"""Tests for the HA detail endpoints (STAGE-005-027, VM per-series)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock
from structlog.testing import capture_logs

from homelab_monitor.kernel.ha.client import HaState
from homelab_monitor.kernel.ha.errors import HaError

_VM_URL = "http://vm-test:8428"
_VM_QUERY_RE = re.compile(r"http://vm-test:8428/api/v1/query\b.*")

_HTTP_OK = 200
_HTTP_UNAUTH = 401
_HTTP_BAD_GATEWAY = 502
_CAP = 100  # mirrors _ENTITIES_TOP_N (do NOT import the private const)

_DEFAULT_TS = 1714867200.0


def _series_response(
    series: list[tuple[dict[str, str], str]],
    ts: float = _DEFAULT_TS,
) -> dict[str, object]:
    """Build a multi-entry VM vector. Each (labels, value_str) -> one result item."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [ts, value_str]} for labels, value_str in series
            ],
        },
    }


def _empty_vector_response() -> dict[str, object]:
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


def _query_of(request: httpx.Request) -> str:
    qs = parse_qs(urlparse(str(request.url)).query)
    return qs["query"][0]


def _callback_for(
    responses: dict[str, dict[str, object]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Map exact query string -> planted response; unknown query -> empty vector."""

    def _cb(request: httpx.Request) -> httpx.Response:
        query = _query_of(request)
        return httpx.Response(200, json=responses.get(query, _empty_vector_response()))

    return _cb


class _FakeRest:
    """HA REST client double: a fixed get_states result (list[HaState] | HaError)."""

    def __init__(self, result: object) -> None:
        self._result = result

    async def get_states(self) -> object:
        return self._result


class _FakeWs:
    """HA WS client double: a fixed send_command result (dict | list | HaError)."""

    def __init__(self, result: object) -> None:
        self._result = result

    async def send_command(self, type_: str, **fields: object) -> object:
        del type_, fields
        return self._result


def _app(client: AsyncClient) -> FastAPI:
    return cast(FastAPI, client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]


def _state(entity_id: str, attributes: dict[str, object]) -> HaState:
    """Construct an HaState (other fields empty)."""
    return HaState(
        entity_id=entity_id,
        state="on",
        attributes=attributes,
        last_changed="",
        last_updated="",
    )


@pytest.fixture(autouse=True)
def _default_ha_clients(authenticated_client: AsyncClient) -> None:  # pyright: ignore[reportUnusedFunction]
    """Default both HA clients to an HaError so enrichment degrades to None.

    Existing 027 tests don't set the clients; without this they'd 503 from
    get_ha_client / get_ha_ws_client. Enrichment tests override per-test.
    """
    app = _app(authenticated_client)
    app.state.ha_client = _FakeRest(HaError(reason="unreachable", message="boom"))
    app.state.ha_ws_client = _FakeWs(HaError(reason="unreachable", message="boom"))


# ── Entities endpoint tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entities_joins_age_and_sorts_stalest_first(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities: join unavailable + age by entity_id, sort age DESC (stalest first)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_entity_available == 0": _series_response(
            [
                ({"entity_id": "a", "domain": "light"}, "0"),
                ({"entity_id": "b", "domain": "sensor"}, "0"),
            ]
        ),
        "homelab_ha_entity_last_changed_seconds": _series_response(
            [
                ({"entity_id": "a"}, "100"),
                ({"entity_id": "b"}, "500"),
            ]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/entities")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004 -- planted value
    assert body["returned"] == 2  # noqa: PLR2004 -- planted value
    assert body["filtered_to"] == "unavailable"
    assert len(body["entities"]) == 2  # noqa: PLR2004 -- planted value
    # b is stalest (age 500 > 100), so it's first.
    assert body["entities"][0]["entity_id"] == "b"
    assert body["entities"][0]["last_changed_age_seconds"] == 500.0  # noqa: PLR2004 -- planted value
    assert body["entities"][0]["available"] is False
    assert body["entities"][0]["domain"] == "sensor"
    assert body["entities"][1]["entity_id"] == "a"
    assert body["entities"][1]["last_changed_age_seconds"] == 100.0  # noqa: PLR2004 -- planted value


@pytest.mark.asyncio
async def test_entities_caps_at_top_n(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities: >100 unavailable -> cap at 100, total=full count, returned=100."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    # Build 150 unavailable series with ascending ages (stalest first after sort).
    unavailable = [({"entity_id": f"e{i}", "domain": "light"}, "0") for i in range(150)]
    age_series = [({"entity_id": f"e{i}"}, str(i)) for i in range(150)]
    responses = {
        "homelab_ha_entity_available == 0": _series_response(unavailable),
        "homelab_ha_entity_last_changed_seconds": _series_response(age_series),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/entities")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 150  # noqa: PLR2004 -- test constant
    assert body["returned"] == _CAP
    assert len(body["entities"]) == _CAP
    # After sort (DESC), e149 is first (age 149).
    assert body["entities"][0]["entity_id"] == "e149"
    assert body["entities"][0]["last_changed_age_seconds"] == 149.0  # noqa: PLR2004 -- planted value


@pytest.mark.asyncio
async def test_entities_missing_age_defaults_zero(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities: unavailable entity with NO age sample -> age defaults to 0.0."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_entity_available == 0": _series_response(
            [
                ({"entity_id": "noage", "domain": "light"}, "0"),
            ]
        ),
        "homelab_ha_entity_last_changed_seconds": _series_response([]),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/entities")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 1
    assert body["returned"] == 1
    assert body["entities"][0]["entity_id"] == "noage"
    assert body["entities"][0]["last_changed_age_seconds"] == 0.0


@pytest.mark.asyncio
async def test_entities_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities: both queries empty -> entities=[], total=0, returned=0."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_entity_available == 0": _empty_vector_response(),
        "homelab_ha_entity_last_changed_seconds": _empty_vector_response(),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/entities")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["entities"] == []
    assert body["total"] == 0
    assert body["returned"] == 0


@pytest.mark.asyncio
async def test_entities_age_non_numeric(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities: age sample with non-numeric value -> age defaults to 0.0."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_entity_available == 0": _series_response(
            [
                ({"entity_id": "badage", "domain": "light"}, "0"),
            ]
        ),
        "homelab_ha_entity_last_changed_seconds": _series_response(
            [
                ({"entity_id": "badage"}, "notanumber"),
            ]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/entities")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["entities"][0]["entity_id"] == "badage"
    assert body["entities"][0]["last_changed_age_seconds"] == 0.0


@pytest.mark.asyncio
async def test_entities_skips_missing_entity_id_label(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities: unavailable sample with no entity_id label -> row skipped."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_entity_available == 0": _series_response(
            [
                ({"entity_id": "good", "domain": "light"}, "0"),
                ({"domain": "sensor"}, "0"),  # missing entity_id -> skipped
            ]
        ),
        "homelab_ha_entity_last_changed_seconds": _series_response(
            [
                ({"entity_id": "good"}, "200"),
            ]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/entities")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    # Only the valid row is included; the sample without entity_id is skipped.
    assert body["total"] == 1
    assert body["returned"] == 1
    assert len(body["entities"]) == 1
    assert body["entities"][0]["entity_id"] == "good"


@pytest.mark.asyncio
async def test_entities_502_on_vm_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities: VM error -> 502 upstream_unavailable."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_exception(httpx.ConnectError("refused"), is_reusable=True)
    resp = await authenticated_client.get("/api/integrations/home-assistant/entities")
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_entities_requires_session(authenticated_client: AsyncClient) -> None:
    """Entities: missing session -> 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/integrations/home-assistant/entities")
    assert resp.status_code == _HTTP_UNAUTH


# ── Batteries endpoint tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batteries_maps_levels(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batteries: query returns rows with numeric levels, total=returned (no cap)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_battery_level < 20": _series_response(
            [
                ({"entity_id": "x", "domain": "sensor"}, "5"),
                ({"entity_id": "y", "domain": "device_tracker"}, "15"),
            ]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/batteries")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004 -- planted value
    assert body["returned"] == 2  # noqa: PLR2004 -- planted value
    assert body["filtered_to"] == "low_or_critical"
    assert len(body["batteries"]) == 2  # noqa: PLR2004 -- planted value
    assert body["batteries"][0]["entity_id"] == "x"
    assert body["batteries"][0]["level"] == 5.0  # noqa: PLR2004 -- planted value
    assert body["batteries"][0]["domain"] == "sensor"
    assert body["batteries"][1]["entity_id"] == "y"
    assert body["batteries"][1]["level"] == 15.0  # noqa: PLR2004 -- planted value


@pytest.mark.asyncio
async def test_batteries_skips_non_numeric(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batteries: non-numeric level value -> row skipped."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_battery_level < 20": _series_response(
            [
                ({"entity_id": "x", "domain": "sensor"}, "5"),
                ({"entity_id": "y", "domain": "sensor"}, "notanumber"),
            ]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/batteries")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    # Only row x is included; y is skipped because its level is non-numeric.
    assert body["total"] == 1
    assert body["returned"] == 1
    assert len(body["batteries"]) == 1
    assert body["batteries"][0]["entity_id"] == "x"


@pytest.mark.asyncio
async def test_batteries_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batteries: empty query -> batteries=[], total=0, returned=0."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_battery_level < 20": _empty_vector_response(),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/batteries")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["batteries"] == []
    assert body["total"] == 0
    assert body["returned"] == 0


@pytest.mark.asyncio
async def test_batteries_502_on_vm_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batteries: VM error -> 502."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_exception(httpx.ConnectError("refused"), is_reusable=True)
    resp = await authenticated_client.get("/api/integrations/home-assistant/batteries")
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_batteries_requires_session(authenticated_client: AsyncClient) -> None:
    """Batteries: missing session -> 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/integrations/home-assistant/batteries")
    assert resp.status_code == _HTTP_UNAUTH


# ── Updates endpoint tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_updates_reads_title_label(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updates: title label is read from metric, filtered_to is None."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_update_available == 1": _series_response(
            [
                ({"entity_id": "u1", "title": "Core Update"}, "1"),
                ({"entity_id": "u2", "title": "Add-on Update"}, "1"),
            ]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/updates")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004 -- planted value
    assert body["returned"] == 2  # noqa: PLR2004 -- planted value
    assert body["filtered_to"] is None
    assert body["updates"][0]["entity_id"] == "u1"
    assert body["updates"][0]["title"] == "Core Update"
    assert body["updates"][1]["entity_id"] == "u2"
    assert body["updates"][1]["title"] == "Add-on Update"


@pytest.mark.asyncio
async def test_updates_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updates: empty query -> updates=[], total=0, returned=0."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_update_available == 1": _empty_vector_response(),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/updates")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["updates"] == []
    assert body["total"] == 0
    assert body["returned"] == 0


@pytest.mark.asyncio
async def test_updates_skips_missing_entity_id_label(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updates: update sample with no entity_id label -> row skipped."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_update_available == 1": _series_response(
            [
                ({"entity_id": "u_good", "title": "Core Update"}, "1"),
                ({"title": "Orphan Update"}, "1"),  # missing entity_id -> skipped
            ]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/updates")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    # Only the valid row is included; the sample without entity_id is skipped.
    assert body["total"] == 1
    assert body["returned"] == 1
    assert len(body["updates"]) == 1
    assert body["updates"][0]["entity_id"] == "u_good"


@pytest.mark.asyncio
async def test_updates_502_on_vm_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updates: VM error -> 502."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_exception(httpx.ConnectError("refused"), is_reusable=True)
    resp = await authenticated_client.get("/api/integrations/home-assistant/updates")
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_updates_requires_session(authenticated_client: AsyncClient) -> None:
    """Updates: missing session -> 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/integrations/home-assistant/updates")
    assert resp.status_code == _HTTP_UNAUTH


# ── Config-entries endpoint tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_config_entries_coarse_error_state(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config-entries: state is always 'error' (coarse; precise state deferred)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_config_entry_setup_error == 1": _series_response(
            [
                ({"domain": "hue", "title": "Philips Hue"}, "1"),
                ({"domain": "zwave", "title": "Z-Wave"}, "1"),
            ]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/config-entries")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004 -- planted value
    assert body["returned"] == 2  # noqa: PLR2004 -- planted value
    assert body["filtered_to"] == "error"
    assert body["config_entries"][0]["domain"] == "hue"
    assert body["config_entries"][0]["title"] == "Philips Hue"
    assert body["config_entries"][0]["state"] == "error"
    assert body["config_entries"][1]["domain"] == "zwave"
    assert body["config_entries"][1]["state"] == "error"


@pytest.mark.asyncio
async def test_config_entries_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config-entries: empty query -> config_entries=[], total=0, returned=0."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_config_entry_setup_error == 1": _empty_vector_response(),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/config-entries")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["config_entries"] == []
    assert body["total"] == 0
    assert body["returned"] == 0


@pytest.mark.asyncio
async def test_config_entries_skips_missing_domain_label(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config-entries: config-entry sample with no domain label -> row skipped."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_config_entry_setup_error == 1": _series_response(
            [
                ({"domain": "hue", "title": "Philips Hue"}, "1"),
                ({"title": "Unlabelled Entry"}, "1"),  # missing domain -> skipped
            ]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/config-entries")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    # Only the valid row is included; the sample without domain is skipped.
    assert body["total"] == 1
    assert body["returned"] == 1
    assert len(body["config_entries"]) == 1
    assert body["config_entries"][0]["domain"] == "hue"


@pytest.mark.asyncio
async def test_config_entries_502_on_vm_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config-entries: VM error -> 502."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_exception(httpx.ConnectError("refused"), is_reusable=True)
    resp = await authenticated_client.get("/api/integrations/home-assistant/config-entries")
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_config_entries_requires_session(authenticated_client: AsyncClient) -> None:
    """Config-entries: missing session -> 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/integrations/home-assistant/config-entries")
    assert resp.status_code == _HTTP_UNAUTH


# ── Repairs endpoint tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repairs_reads_labels(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairs: domain, issue_id, severity from labels."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_repair_issue == 1": _series_response(
            [
                ({"domain": "zwave", "issue_id": "battery_low", "severity": "warning"}, "1"),
                ({"domain": "mqtt", "issue_id": "connection_lost", "severity": "critical"}, "1"),
            ]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/repairs")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004 -- planted value
    assert body["returned"] == 2  # noqa: PLR2004 -- planted value
    assert body["filtered_to"] is None
    assert body["repairs"][0]["domain"] == "zwave"
    assert body["repairs"][0]["issue_id"] == "battery_low"
    assert body["repairs"][0]["severity"] == "warning"
    assert body["repairs"][1]["domain"] == "mqtt"
    assert body["repairs"][1]["issue_id"] == "connection_lost"
    assert body["repairs"][1]["severity"] == "critical"


@pytest.mark.asyncio
async def test_repairs_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairs: empty query -> repairs=[], total=0, returned=0."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_repair_issue == 1": _empty_vector_response(),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/repairs")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["repairs"] == []
    assert body["total"] == 0
    assert body["returned"] == 0


@pytest.mark.asyncio
async def test_repairs_skips_missing_issue_id_label(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairs: repair sample with no issue_id label -> row skipped."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_repair_issue == 1": _series_response(
            [
                ({"domain": "zwave", "issue_id": "battery_low", "severity": "warning"}, "1"),
                ({"domain": "mqtt", "severity": "critical"}, "1"),  # missing issue_id -> skipped
            ]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses),
        url=_VM_QUERY_RE,
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/repairs")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    # Only the valid row is included; the sample without issue_id is skipped.
    assert body["total"] == 1
    assert body["returned"] == 1
    assert len(body["repairs"]) == 1
    assert body["repairs"][0]["issue_id"] == "battery_low"


@pytest.mark.asyncio
async def test_repairs_502_on_vm_error(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairs: VM error -> 502."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_exception(httpx.ConnectError("refused"), is_reusable=True)
    resp = await authenticated_client.get("/api/integrations/home-assistant/repairs")
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_repairs_requires_session(authenticated_client: AsyncClient) -> None:
    """Repairs: missing session -> 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/integrations/home-assistant/repairs")
    assert resp.status_code == _HTTP_UNAUTH


# ── STAGE-005-031: live-HA enrichment tests ─────────────────────────────────


def _set_rest(client: AsyncClient, result: object) -> None:
    _app(client).state.ha_client = _FakeRest(result)


def _set_ws(client: AsyncClient, result: object) -> None:
    _app(client).state.ha_ws_client = _FakeWs(result)


# --- batteries: device enrichment ---


@pytest.mark.asyncio
async def test_batteries_enriches_device_from_friendly_name(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batteries: device filled from the matching entity's friendly_name."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_battery_level < 20": _series_response(
            [({"entity_id": "sensor.x", "domain": "sensor"}, "5")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_rest(authenticated_client, [_state("sensor.x", {"friendly_name": "Front Door"})])
    resp = await authenticated_client.get("/api/integrations/home-assistant/batteries")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["batteries"][0]["device"] == "Front Door"


@pytest.mark.asyncio
async def test_batteries_ha_down_device_none_still_200(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batteries: get_states HaError -> rows still 200, device None (NOT 502)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_battery_level < 20": _series_response(
            [({"entity_id": "sensor.x", "domain": "sensor"}, "5")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_rest(authenticated_client, HaError(reason="unreachable", message="down"))
    resp = await authenticated_client.get("/api/integrations/home-assistant/batteries")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 1
    assert body["batteries"][0]["device"] is None


@pytest.mark.asyncio
async def test_batteries_row_not_in_snapshot_device_none(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batteries: VM row whose entity is absent from the HA snapshot -> device None."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_battery_level < 20": _series_response(
            [({"entity_id": "sensor.x", "domain": "sensor"}, "5")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_rest(authenticated_client, [_state("sensor.other", {"friendly_name": "Elsewhere"})])
    resp = await authenticated_client.get("/api/integrations/home-assistant/batteries")
    assert resp.status_code == _HTTP_OK
    assert resp.json()["batteries"][0]["device"] is None


@pytest.mark.asyncio
async def test_batteries_attribute_missing_device_none(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batteries: entity present but no friendly_name attribute -> device None."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_battery_level < 20": _series_response(
            [({"entity_id": "sensor.x", "domain": "sensor"}, "5")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_rest(authenticated_client, [_state("sensor.x", {"battery_level": 5})])
    resp = await authenticated_client.get("/api/integrations/home-assistant/batteries")
    assert resp.status_code == _HTTP_OK
    assert resp.json()["batteries"][0]["device"] is None


# --- entities: friendly_name enrichment ---


@pytest.mark.asyncio
async def test_entities_enriches_friendly_name(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities: friendly_name filled from the matching entity."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_entity_available == 0": _series_response(
            [({"entity_id": "light.a", "domain": "light"}, "0")]
        ),
        "homelab_ha_entity_last_changed_seconds": _series_response(
            [({"entity_id": "light.a"}, "100")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_rest(authenticated_client, [_state("light.a", {"friendly_name": "Hallway Light"})])
    resp = await authenticated_client.get("/api/integrations/home-assistant/entities")
    assert resp.status_code == _HTTP_OK
    assert resp.json()["entities"][0]["friendly_name"] == "Hallway Light"


@pytest.mark.asyncio
async def test_entities_ha_down_friendly_name_none_still_200(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities: get_states HaError -> rows 200, friendly_name None (NOT 502)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_entity_available == 0": _series_response(
            [({"entity_id": "light.a", "domain": "light"}, "0")]
        ),
        "homelab_ha_entity_last_changed_seconds": _series_response(
            [({"entity_id": "light.a"}, "100")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_rest(authenticated_client, HaError(reason="timeout", message="slow"))
    resp = await authenticated_client.get("/api/integrations/home-assistant/entities")
    assert resp.status_code == _HTTP_OK
    assert resp.json()["entities"][0]["friendly_name"] is None


# --- updates: version + release_url enrichment ---


@pytest.mark.asyncio
async def test_updates_enriches_versions_and_release_url(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updates: installed/latest version + release_url filled from attributes."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_update_available == 1": _series_response(
            [({"entity_id": "update.core", "title": "Core"}, "1")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_rest(
        authenticated_client,
        [
            _state(
                "update.core",
                {
                    "installed_version": "2026.5.1",
                    "latest_version": "2026.6.0",
                    "release_url": "https://example.com/release",
                },
            )
        ],
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/updates")
    assert resp.status_code == _HTTP_OK
    row = resp.json()["updates"][0]
    assert row["installed_version"] == "2026.5.1"
    assert row["latest_version"] == "2026.6.0"
    assert row["release_url"] == "https://example.com/release"


@pytest.mark.asyncio
async def test_updates_ha_down_versions_none_still_200(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updates: get_states HaError -> rows 200, all version fields None (NOT 502)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_update_available == 1": _series_response(
            [({"entity_id": "update.core", "title": "Core"}, "1")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_rest(authenticated_client, HaError(reason="auth", message="bad token", status=401))
    resp = await authenticated_client.get("/api/integrations/home-assistant/updates")
    assert resp.status_code == _HTTP_OK
    row = resp.json()["updates"][0]
    assert row["installed_version"] is None
    assert row["latest_version"] is None
    assert row["release_url"] is None


@pytest.mark.asyncio
async def test_updates_attribute_missing_versions_none(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updates: entity present but no version attributes -> fields None."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_update_available == 1": _series_response(
            [({"entity_id": "update.core", "title": "Core"}, "1")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_rest(authenticated_client, [_state("update.core", {"title": "Core"})])
    resp = await authenticated_client.get("/api/integrations/home-assistant/updates")
    assert resp.status_code == _HTTP_OK
    row = resp.json()["updates"][0]
    assert row["installed_version"] is None
    assert row["latest_version"] is None
    assert row["release_url"] is None


# --- repairs: description + learn_more_url enrichment ---


@pytest.mark.asyncio
async def test_repairs_enriches_fields_bare_list(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairs: description + learn_more_url filled from a bare-list WS issues snapshot."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_repair_issue == 1": _series_response(
            [({"domain": "zwave", "issue_id": "battery_low", "severity": "warning"}, "1")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_ws(
        authenticated_client,
        [
            {
                "domain": "zwave",
                "issue_id": "battery_low",
                "description": "Z-Wave battery low",
                "learn_more_url": "https://example.com/zwave",
            }
        ],
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/repairs")
    assert resp.status_code == _HTTP_OK
    row = resp.json()["repairs"][0]
    assert row["description"] == "Z-Wave battery low"
    assert row["learn_more_url"] == "https://example.com/zwave"


@pytest.mark.asyncio
async def test_repairs_enriches_fields_dict_wrapped(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairs: description + learn_more_url filled from a {"issues":[...]} WS snapshot."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_repair_issue == 1": _series_response(
            [({"domain": "mqtt", "issue_id": "conn", "severity": "critical"}, "1")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_ws(
        authenticated_client,
        {
            "issues": [
                {
                    "domain": "mqtt",
                    "issue_id": "conn",
                    "description": "MQTT lost",
                    "learn_more_url": "https://example.com/mqtt",
                }
            ]
        },
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/repairs")
    assert resp.status_code == _HTTP_OK
    row = resp.json()["repairs"][0]
    assert row["description"] == "MQTT lost"
    assert row["learn_more_url"] == "https://example.com/mqtt"


@pytest.mark.asyncio
async def test_repairs_ha_down_fields_none_still_200(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairs: send_command HaError -> rows 200, both fields None (NOT 502)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_repair_issue == 1": _series_response(
            [({"domain": "zwave", "issue_id": "battery_low", "severity": "warning"}, "1")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_ws(authenticated_client, HaError(reason="unreachable", message="ws down"))
    resp = await authenticated_client.get("/api/integrations/home-assistant/repairs")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 1
    assert body["repairs"][0]["description"] is None
    assert body["repairs"][0]["learn_more_url"] is None


@pytest.mark.asyncio
async def test_repairs_issue_not_in_snapshot_fields_none(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairs: VM row absent from the WS snapshot -> both enriched fields None."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_repair_issue == 1": _series_response(
            [({"domain": "zwave", "issue_id": "battery_low", "severity": "warning"}, "1")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_ws(
        authenticated_client,
        [
            {
                "domain": "other",
                "issue_id": "different",
                "description": "elsewhere",
                "learn_more_url": "https://example.com/elsewhere",
            }
        ],
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/repairs")
    assert resp.status_code == _HTTP_OK
    row = resp.json()["repairs"][0]
    assert row["description"] is None
    assert row["learn_more_url"] is None


@pytest.mark.asyncio
async def test_repairs_missing_fields_none(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairs: issue present but no description / learn_more_url keys -> both None."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    responses = {
        "homelab_ha_repair_issue == 1": _series_response(
            [({"domain": "zwave", "issue_id": "battery_low", "severity": "warning"}, "1")]
        ),
    }
    httpx_mock.add_callback(
        _callback_for(responses), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )
    _set_ws(
        authenticated_client,
        [{"domain": "zwave", "issue_id": "battery_low", "translation_key": "tk"}],
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/repairs")
    assert resp.status_code == _HTTP_OK
    row = resp.json()["repairs"][0]
    assert row["description"] is None
    assert row["learn_more_url"] is None


# --- VM-down still 502 (enrichment doesn't change the VM-down contract) ---


@pytest.mark.asyncio
async def test_batteries_vm_down_still_502_with_enrichment(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batteries: VM error -> 502 even though the HA client is healthy."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_exception(httpx.ConnectError("refused"), is_reusable=True)
    _set_rest(authenticated_client, [_state("sensor.x", {"friendly_name": "X"})])
    resp = await authenticated_client.get("/api/integrations/home-assistant/batteries")
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_repairs_vm_down_still_502_with_enrichment(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairs: VM error -> 502 even though the WS client is healthy."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_exception(httpx.ConnectError("refused"), is_reusable=True)
    _set_ws(
        authenticated_client,
        [{"domain": "z", "issue_id": "i", "description": "d", "learn_more_url": "u"}],
    )
    resp = await authenticated_client.get("/api/integrations/home-assistant/repairs")
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.json()["error"]["code"] == "upstream_unavailable"


# --- MANDATORY: enriched values never logged (mirror the 029 sentinel test) ---


@pytest.mark.asyncio
async def test_enriched_values_never_logged(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sentinels appear in the RESPONSE but in NO captured log record.

    Injects a sentinel friendly_name (battery device) AND a sentinel repair
    description; asserts each appears in the respective response body but never
    in any structlog event (D-ENRICH-PRIVACY: enriched fields are never logged).
    """
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    device_sentinel = "DEVICE-NAME-SENTINEL-9f3a"
    description_sentinel = "REPAIR-DESC-SENTINEL-9f3a"

    battery_resp = {
        "homelab_ha_battery_level < 20": _series_response(
            [({"entity_id": "sensor.x", "domain": "sensor"}, "5")]
        ),
    }
    repair_resp = {
        "homelab_ha_repair_issue == 1": _series_response(
            [({"domain": "zwave", "issue_id": "battery_low", "severity": "warning"}, "1")]
        ),
    }
    # Both query strings map through one callback (unknown -> empty vector).
    combined = {**battery_resp, **repair_resp}
    httpx_mock.add_callback(
        _callback_for(combined), url=_VM_QUERY_RE, method="GET", is_reusable=True
    )

    _set_rest(authenticated_client, [_state("sensor.x", {"friendly_name": device_sentinel})])
    _set_ws(
        authenticated_client,
        [{"domain": "zwave", "issue_id": "battery_low", "description": description_sentinel}],
    )

    with capture_logs() as captured:
        batt = await authenticated_client.get("/api/integrations/home-assistant/batteries")
        rep = await authenticated_client.get("/api/integrations/home-assistant/repairs")

    assert batt.status_code == _HTTP_OK
    assert rep.status_code == _HTTP_OK
    # Present in the response bodies delivered to the authenticated session.
    assert device_sentinel in batt.text
    assert description_sentinel in rep.text
    # Absent from EVERY captured log event.
    for event in captured:
        serialized = json.dumps(event)
        assert device_sentinel not in serialized
        assert description_sentinel not in serialized
