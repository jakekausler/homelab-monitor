"""Tests for the HA detail endpoints (STAGE-005-027, VM per-series)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock

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
