"""Tests for GET /api/integrations/unifi/* endpoints."""

from __future__ import annotations

import json as _json
import re
from collections.abc import Callable
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.db.repositories.unifi_clients_repository import UnifiClientRepo
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

_VM_URL = "http://vm-test:8428"

# Named constants to avoid PLR2004 in DNS enrichment tests.
_DNS_BLOCKED_COUNT = 2
_SIGNAL_DBM_DNS = -70.0

# Named constants to avoid PLR2004 in asserts.
_HTTP_OK = 200
_HTTP_UNAUTH = 401
_HTTP_NOT_FOUND = 404
_HTTP_BAD_GATEWAY = 502

# Test data constants
_DEVICE_UP = 3
_DEVICE_TOTAL = 4
_THREAT_COUNT = 2
_DEFAULT_LIMIT = 100
_CLIENT_TOTAL = 3
_OFFSET_PAST_END = 10
_LIMIT_2 = 2
_POOL_SIZE = 100.0
_RESERVATION_COUNT = 5
_OCCUPANCY = 0.25
_POOR_SIGNAL = 3
_POOR_SAT = 2
_HIGH_RETRIES = 1
_SSIDS_COUNT = 2
_SSID_1_COUNT = 5
_BAND_COUNT = 2
_BAND_1_COUNT = 4
_LINK_COUNT = 2
_LINK_1_COUNT = 5
_SIGNAL_DBM = -60.0
_TX_RATE_BPS = 1000000.0
_RX_RATE_BPS = 5000000.0
_DPI_BYTES = 123456.0
_THREATS_COUNT = 2
_THREAT_COUNT_1 = 5
_THREAT_COUNT_2 = 3
_CLIENT_COUNT_2_ITEMS = 2


def _vector_response(
    value_str: str, ts: float = 1714867200.0, labels: dict[str, str] | None = None
) -> dict[str, object]:
    """Return a mock VM instant query response with one sample."""
    if labels is None:
        labels = {}
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": labels, "value": [ts, value_str]}],
        },
    }


def _empty_vector_response() -> dict[str, object]:
    """Return a mock VM instant query response with no samples."""
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": []},
    }


def _query_of(request: httpx.Request) -> str:
    """Extract the 'query' parameter from a request URL."""
    qs = parse_qs(urlparse(str(request.url)).query)
    return qs["query"][0]


def _make_callback(
    queries: dict[str, dict[str, object]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a pytest_httpx callback that answers each instant query.

    queries: query expression -> response dict. Queries NOT in the dict return
    an empty vector (forces the endpoint's default-to-None/0/False path).
    """

    def _callback(request: httpx.Request) -> httpx.Response:
        query = _query_of(request)
        if query in queries:
            return httpx.Response(200, json=queries[query])
        # Missing-series edge case: empty vector -> field must default.
        return httpx.Response(200, json=_empty_vector_response())

    return _callback


# ── /summary tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_requires_session(authenticated_client: AsyncClient) -> None:
    """Missing session cookie returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/integrations/unifi/summary")
    assert resp.status_code == _HTTP_UNAUTH


@pytest.mark.asyncio
async def test_summary_with_data(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: controller up, devices, threats, teleport, wan all ok."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_up": _vector_response("1"),
        "count(homelab_unifi_device_state == 1)": _vector_response("3"),
        "count(homelab_unifi_device_state)": _vector_response("4"),
        "count(homelab_unifi_ips_threat == 1)": _vector_response("2"),
        "homelab_unifi_teleport_up": _vector_response("1"),
        "homelab_unifi_wan_up": _vector_response("1"),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/summary")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["controller_up"] is True
    assert body["controller_reason"] is None
    assert body["devices_up"] == _DEVICE_UP
    assert body["devices_total"] == _DEVICE_TOTAL
    assert body["threat_count"] == _THREAT_COUNT
    assert body["teleport_up"] is True
    assert body["wan_up"] is True
    assert body["last_seen"] is not None


@pytest.mark.asyncio
async def test_summary_controller_down_with_reason(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Controller down but sample present with reason label."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_up": _vector_response("0", labels={"reason": "network_error"}),
        "count(homelab_unifi_device_state == 1)": _empty_vector_response(),
        "count(homelab_unifi_device_state)": _empty_vector_response(),
        "count(homelab_unifi_ips_threat == 1)": _empty_vector_response(),
        "homelab_unifi_teleport_up": _empty_vector_response(),
        "homelab_unifi_wan_up": _empty_vector_response(),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/summary")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["controller_up"] is False
    assert body["controller_reason"] == "network_error"
    assert body["devices_up"] == 0
    assert body["threat_count"] == 0


@pytest.mark.asyncio
async def test_summary_empty_vectors_default(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty vectors -> scalars default to 0, bools default to False, last_seen=None."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {}  # all empty
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/summary")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["controller_up"] is False
    assert body["controller_reason"] is None
    assert body["devices_up"] == 0
    assert body["devices_total"] == 0
    assert body["threat_count"] == 0
    assert body["teleport_up"] is False
    assert body["wan_up"] is False
    assert body["last_seen"] is None


@pytest.mark.asyncio
async def test_summary_vm_failure_502(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VM transport error -> 502 upstream_unavailable."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    httpx_mock.add_exception(
        httpx.ConnectError("vm unreachable"),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/summary")
    assert resp.status_code == _HTTP_BAD_GATEWAY


# ── /clients tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clients_requires_session(authenticated_client: AsyncClient) -> None:
    """Missing session cookie returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/integrations/unifi/clients")
    assert resp.status_code == _HTTP_UNAUTH


@pytest.mark.asyncio
async def test_clients_empty_registry(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Empty registry -> clients=[], total=0."""
    resp = await authenticated_client.get("/api/integrations/unifi/clients")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["clients"] == []
    assert body["total"] == 0
    assert body["limit"] == _DEFAULT_LIMIT
    assert body["offset"] == 0


@pytest.mark.asyncio
async def test_clients_populated_with_pagination(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
) -> None:
    """Populated registry -> rows mapped, pagination applied."""
    # Seed 3 clients
    async with repo.transaction() as conn:
        await UnifiClientRepo.upsert_client_conn(
            conn,
            mac="aa:00:00:00:00:01",
            ip="192.168.2.50",
            hostname="client1",
            name="Client 1",
            oui="Intel",
            network="LAN",
            ap_mac="ap:01",
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=True,
            first_seen="2024-01-01T00:00:00Z",
            last_seen="2024-01-01T01:00:00Z",
        )
        await UnifiClientRepo.upsert_client_conn(
            conn,
            mac="aa:00:00:00:00:02",
            ip="192.168.2.51",
            hostname="client2",
            name="Client 2",
            oui="Apple",
            network="IoT",
            ap_mac="ap:02",
            sw_mac=None,
            sw_port=None,
            use_fixedip=True,
            fixed_ip="192.168.2.100",
            online=True,
            first_seen="2024-01-01T00:00:00Z",
            last_seen="2024-01-01T02:00:00Z",
        )
        await UnifiClientRepo.upsert_client_conn(
            conn,
            mac="aa:00:00:00:00:03",
            ip="192.168.2.52",
            hostname="client3",
            name="Client 3",
            oui="Asus",
            network="LAN",
            ap_mac="ap:03",
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=False,
            first_seen="2024-01-01T00:00:00Z",
            last_seen="2024-01-01T03:00:00Z",
        )

    resp = await authenticated_client.get("/api/integrations/unifi/clients")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == _CLIENT_TOTAL
    assert len(body["clients"]) == _CLIENT_TOTAL
    assert body["limit"] == _DEFAULT_LIMIT
    assert body["offset"] == 0

    # Test pagination: offset past end
    resp = await authenticated_client.get("/api/integrations/unifi/clients?offset=10")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == _CLIENT_TOTAL
    assert body["clients"] == []
    assert body["offset"] == _OFFSET_PAST_END

    # Test limit
    resp = await authenticated_client.get("/api/integrations/unifi/clients?limit=2")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == _CLIENT_TOTAL
    assert len(body["clients"]) == _CLIENT_COUNT_2_ITEMS
    assert body["limit"] == _LIMIT_2


# ── /clients/{mac} tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_detail_not_found(
    authenticated_client: AsyncClient,
) -> None:
    """Unknown mac -> 404 not_found (registry lookup only, no VM calls)."""
    resp = await authenticated_client.get("/api/integrations/unifi/clients/unknown:mac")
    assert resp.status_code == _HTTP_NOT_FOUND


@pytest.mark.asyncio
async def test_client_detail_found_with_vm_series(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found client + VM series populated -> full detail."""
    _VL_URL = "http://vl-test:9428"
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", _VL_URL)

    # Seed client
    async with repo.transaction() as conn:
        await UnifiClientRepo.upsert_client_conn(
            conn,
            mac="aa:00:00:00:00:01",
            ip="192.168.2.50",
            hostname="testclient",
            name="Test Client",
            oui="Intel",
            network="LAN",
            ap_mac="ap:01",
            sw_mac="sw:01",
            sw_port=5,
            use_fixedip=True,
            fixed_ip="192.168.2.100",
            online=True,
            first_seen="2024-01-01T00:00:00Z",
            last_seen="2024-01-01T01:00:00Z",
        )
        await UnifiClientRepo.set_lease_expiry_conn(
            conn, mac="aa:00:00:00:00:01", lease_expiry="2024-01-02T00:00:00Z"
        )

    queries: dict[str, dict[str, object]] = {
        'homelab_unifi_client_signal_dbm{mac="aa:00:00:00:00:01"}': _vector_response("-60"),
        'homelab_unifi_client_tx_rate_bps{mac="aa:00:00:00:00:01"}': _vector_response("1000000"),
        'homelab_unifi_client_rx_rate_bps{mac="aa:00:00:00:00:01"}': _vector_response("5000000"),
        'homelab_unifi_client_dpi_bytes{client="aa:00:00:00:00:01"}': _vector_response(
            "123456", labels={"client": "aa:00:00:00:00:01", "app": "netflix", "cat": "video"}
        ),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    # VL mock for DNS enrichment - empty response (no DNS data)
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query\b.*"),
        method="GET",
        text="",
        is_reusable=True,
    )

    resp = await authenticated_client.get("/api/integrations/unifi/clients/aa:00:00:00:00:01")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["mac"] == "aa:00:00:00:00:01"
    assert body["ip"] == "192.168.2.50"
    assert body["hostname"] == "testclient"
    assert body["name"] == "Test Client"
    assert body["use_fixedip"] is True
    assert body["fixed_ip"] == "192.168.2.100"
    assert body["online"] is True
    assert body["is_host"] is False
    assert body["lease_expiry"] == "2024-01-02T00:00:00Z"
    assert body["series"]["signal_dbm"] == _SIGNAL_DBM
    assert body["series"]["tx_rate_bps"] == _TX_RATE_BPS
    assert body["series"]["rx_rate_bps"] == _RX_RATE_BPS
    assert len(body["dpi"]) == 1
    assert body["dpi"][0]["app"] == "netflix"
    assert body["dpi"][0]["bytes"] == _DPI_BYTES
    assert body["dns"] is None


@pytest.mark.asyncio
async def test_client_detail_found_no_vm_series(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found client + empty VM series -> series all None, dpi=[]."""
    _VL_URL = "http://vl-test:9428"
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", _VL_URL)

    # Seed client
    async with repo.transaction() as conn:
        await UnifiClientRepo.upsert_client_conn(
            conn,
            mac="aa:00:00:00:00:02",
            ip="192.168.2.51",
            hostname="offline",
            name="Offline Client",
            oui="Apple",
            network="IoT",
            ap_mac=None,
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=False,
            first_seen="2024-01-01T00:00:00Z",
            last_seen="2024-01-01T01:00:00Z",
        )

    queries: dict[str, dict[str, object]] = {}  # all empty
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    # VL mock for DNS enrichment - empty response (no DNS data)
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query\b.*"),
        method="GET",
        text="",
        is_reusable=True,
    )

    resp = await authenticated_client.get("/api/integrations/unifi/clients/aa:00:00:00:00:02")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["mac"] == "aa:00:00:00:00:02"
    assert body["online"] is False
    assert body["series"]["signal_dbm"] is None
    assert body["series"]["tx_rate_bps"] is None
    assert body["series"]["rx_rate_bps"] is None
    assert body["dpi"] == []
    assert body["dns"] is None


@pytest.mark.asyncio
async def test_client_detail_vm_failure_502(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found client + VM error -> 502 (404 checked first)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

    # Seed client
    async with repo.transaction() as conn:
        await UnifiClientRepo.upsert_client_conn(
            conn,
            mac="aa:00:00:00:00:03",
            ip="192.168.2.52",
            hostname="client3",
            name="Client 3",
            oui="Asus",
            network="LAN",
            ap_mac=None,
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=True,
            first_seen="2024-01-01T00:00:00Z",
            last_seen="2024-01-01T01:00:00Z",
        )

    httpx_mock.add_exception(
        httpx.ConnectError("vm unreachable"),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )

    resp = await authenticated_client.get("/api/integrations/unifi/clients/aa:00:00:00:00:03")
    assert resp.status_code == _HTTP_BAD_GATEWAY


_VL_URL = "http://vl-test:9428"

_MAC_DNS = "bb:00:00:00:00:01"
_MAC_DNS_IP = "192.168.2.60"


def _seed_mac_queries(
    queries: dict[str, dict[str, object]] | None = None,
) -> dict[str, dict[str, object]]:
    """Return the 4 per-client VM instant query responses for _MAC_DNS."""
    result: dict[str, dict[str, object]] = {
        f'homelab_unifi_client_signal_dbm{{mac="{_MAC_DNS}"}}': _vector_response("-70"),
        f'homelab_unifi_client_tx_rate_bps{{mac="{_MAC_DNS}"}}': _empty_vector_response(),
        f'homelab_unifi_client_rx_rate_bps{{mac="{_MAC_DNS}"}}': _empty_vector_response(),
        f'homelab_unifi_client_dpi_bytes{{client="{_MAC_DNS}"}}': _empty_vector_response(),
    }
    if queries:
        result.update(queries)
    return result


def _pihole_ndjson(records: list[dict[str, object]]) -> str:
    """Encode a list of pihole-queries records as VictoriaLogs NDJSON body.

    Each NDJSON line has _msg = JSON-encoded pihole record, _time, _stream_id.
    VlLogLine.message = str(obj.get("_msg", "")) per _parse_one().
    """
    lines: list[str] = []
    for i, rec in enumerate(records):
        vl_line = _json.dumps(
            {
                "_time": f"2024-05-05T00:00:{i:02d}Z",
                "_msg": _json.dumps(rec),
                "_stream_id": "{}",
            }
        )
        lines.append(vl_line)
    return "\n".join(lines)


async def _seed_dns_client(repo: SqliteRepository) -> None:
    """Seed the client row + a recent observation for _MAC_DNS / _MAC_DNS_IP."""
    now = utc_now_iso()
    async with repo.transaction() as conn:
        await UnifiClientRepo.upsert_client_conn(
            conn,
            mac=_MAC_DNS,
            ip=_MAC_DNS_IP,
            hostname="dns-test",
            name="DNS Test",
            oui=None,
            network="LAN",
            ap_mac=None,
            sw_mac=None,
            sw_port=None,
            use_fixedip=False,
            fixed_ip=None,
            online=True,
            first_seen=now,
            last_seen=now,
        )


@pytest.mark.asyncio
async def test_client_detail_dns_populated(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL returns pihole-queries records → dns fields populated."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", _VL_URL)

    await _seed_dns_client(repo)

    # Two GRAVITY records on ads.example, one FORWARDED on cdn.example.
    vl_body = _pihole_ndjson(
        [
            {
                "client_ip": _MAC_DNS_IP,
                "domain": "ads.example",
                "status": "GRAVITY",
                "time": 1714867200.0,
            },
            {
                "client_ip": _MAC_DNS_IP,
                "domain": "ads.example",
                "status": "GRAVITY",
                "time": 1714867210.0,
            },
            {
                "client_ip": _MAC_DNS_IP,
                "domain": "cdn.example",
                "status": "FORWARDED",
                "time": 1714867220.0,
            },
        ]
    )
    httpx_mock.add_callback(
        _make_callback(_seed_mac_queries()),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query\b.*"),
        method="GET",
        text=vl_body,
        is_reusable=True,
    )

    resp = await authenticated_client.get(f"/api/integrations/unifi/clients/{_MAC_DNS}")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["dns"] is not None
    assert body["dns"]["blocked_count"] == _DNS_BLOCKED_COUNT
    assert body["dns"]["top_domains"][0] == "ads.example"
    assert "cdn.example" in body["dns"]["top_domains"]
    assert body["dns"]["last_query_at"] is not None
    assert body["dns"]["last_query_at"].endswith("Z")


@pytest.mark.asyncio
async def test_client_detail_dns_postfilter_drops_false_positive(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VL record whose parsed client_ip != queried IP is excluded (false positive)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", _VL_URL)

    await _seed_dns_client(repo)

    # The record phrase-matched the IP string but belongs to a different client.
    vl_body = _pihole_ndjson(
        [
            {
                "client_ip": "192.168.2.99",
                "domain": "false.pos",
                "status": "FORWARDED",
                "time": 1714867200.0,
            },
        ]
    )
    httpx_mock.add_callback(
        _make_callback(_seed_mac_queries()),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query\b.*"),
        method="GET",
        text=vl_body,
        is_reusable=True,
    )

    resp = await authenticated_client.get(f"/api/integrations/unifi/clients/{_MAC_DNS}")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["dns"] is None


@pytest.mark.asyncio
async def test_client_detail_dns_empty_when_no_records(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL returns empty body → no records → dns is None."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", _VL_URL)

    await _seed_dns_client(repo)

    httpx_mock.add_callback(
        _make_callback(_seed_mac_queries()),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query\b.*"),
        method="GET",
        text="",
        is_reusable=True,
    )

    resp = await authenticated_client.get(f"/api/integrations/unifi/clients/{_MAC_DNS}")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["dns"] is None


@pytest.mark.asyncio
async def test_client_detail_dns_vl_error_degrades_to_none(
    authenticated_client: AsyncClient,
    repo: SqliteRepository,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VL transport error → dns=None; endpoint returns 200 (DNS is supplementary)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", _VL_URL)

    await _seed_dns_client(repo)

    httpx_mock.add_callback(
        _make_callback(_seed_mac_queries()),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    httpx_mock.add_exception(
        httpx.ConnectError("vl unreachable"),
        url=re.compile(r"http://vl-test:9428/select/logsql/query\b.*"),
        method="GET",
        is_reusable=True,
    )

    resp = await authenticated_client.get(f"/api/integrations/unifi/clients/{_MAC_DNS}")
    # Must be 200, not 502 — VL outage must NOT break the page.
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["dns"] is None
    # Rest of detail is intact.
    assert body["mac"] == _MAC_DNS
    assert body["series"]["signal_dbm"] == _SIGNAL_DBM_DNS


@pytest.mark.asyncio
async def test_client_mac_injection_escaped(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malicious mac with embedded quote is escaped in PromQL label
    value."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    malicious_mac = 'aa"} or on() homelab_unifi_up{x="'
    escaped_mac = r"aa\"} or on() homelab_unifi_up{x=\""

    seen_queries: list[str] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        seen_queries.append(_query_of(request))
        return httpx.Response(200, json=_empty_vector_response())

    httpx_mock.add_callback(
        _capture,
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    await authenticated_client.get(f"/api/integrations/unifi/devices/{malicious_mac}")
    state_queries = [q for q in seen_queries if "device_state" in q]
    assert len(state_queries) >= 1
    for q in state_queries:
        assert escaped_mac in q, f"Unescaped value found in query: {q!r}"


@pytest.mark.asyncio
async def test_device_detail_injection_escaped(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """device path param with embedded double-quote is escaped in all
    PromQL queries."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    malicious_device = 'USW"}'
    escaped_device = r"USW\"}"

    seen_queries: list[str] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        seen_queries.append(_query_of(request))
        return httpx.Response(200, json=_empty_vector_response())

    httpx_mock.add_callback(
        _capture,
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get(f"/api/integrations/unifi/devices/{malicious_device}")
    assert resp.status_code == _HTTP_NOT_FOUND
    assert len(seen_queries) >= 1
    for q in seen_queries:
        assert escaped_device in q, f"Unescaped injection found in: {q!r}"


# ── /threats tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_threats_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty threats vector -> threats=[]."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {}
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/threats")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["threats"] == []


@pytest.mark.asyncio
async def test_threats_with_data(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threat samples -> mapped to rows by type label."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_threat": {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"type": "malware"}, "value": [1714867200.0, "5"]},
                    {"metric": {"type": "phishing"}, "value": [1714867200.0, "3"]},
                ],
            },
        },
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/threats")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert len(body["threats"]) == _THREATS_COUNT
    assert body["threats"][0]["threat_type"] == "malware"
    assert body["threats"][0]["count"] == _THREAT_COUNT_1
    assert body["threats"][1]["threat_type"] == "phishing"
    assert body["threats"][1]["count"] == _THREAT_COUNT_2


# ── /network/dhcp tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dhcp_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty dhcp vectors -> networks=[]."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {}
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/dhcp")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["networks"] == []


@pytest.mark.asyncio
async def test_dhcp_with_occupancy(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network with pool_size + clients -> occupancy computed, dhcp_enabled from pool presence."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_dhcp_pool_size": _vector_response("100", labels={"network": "LAN"}),
        "homelab_unifi_dhcp_pool_start": _vector_response(
            "192.168.1.100", labels={"network": "LAN"}
        ),
        "homelab_unifi_dhcp_pool_end": _vector_response("192.168.1.199", labels={"network": "LAN"}),
        "homelab_unifi_dhcp_reservation_count": _vector_response("5", labels={"network": "LAN"}),
        "homelab_unifi_client_count_by_network": _vector_response("25", labels={"network": "LAN"}),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/dhcp")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert len(body["networks"]) == 1
    net = body["networks"][0]
    assert net["network"] == "LAN"
    assert net["pool_size"] == _POOL_SIZE
    assert net["pool_start"] == "192.168.1.100"
    assert net["pool_end"] == "192.168.1.199"
    assert net["dhcp_enabled"] is True
    assert net["reservation_count"] == _RESERVATION_COUNT
    assert net["occupancy"] == _OCCUPANCY  # 25 / 100


@pytest.mark.asyncio
async def test_dhcp_no_occupancy_when_pool_zero(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network with pool_size=0 -> occupancy=None, dhcp_enabled=True (pool_size present)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_dhcp_pool_size": _vector_response("0", labels={"network": "Guest"}),
        "homelab_unifi_dhcp_reservation_count": _empty_vector_response(),
        "homelab_unifi_client_count_by_network": _empty_vector_response(),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/dhcp")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert len(body["networks"]) == 1
    net = body["networks"][0]
    assert net["network"] == "Guest"
    assert net["pool_size"] == 0.0
    assert net["occupancy"] is None
    assert net["dhcp_enabled"] is True  # pool_size metric present -> enabled


# ── /network/wifi tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wifi_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty vectors -> defaults (0 for counts, [] for lists)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {}
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/wifi")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["poor_signal"] == 0
    assert body["poor_satisfaction"] == 0
    assert body["high_retries"] == 0
    assert body["ssids"] == []
    assert body["by_band"] == []
    assert body["by_link"] == []


@pytest.mark.asyncio
async def test_wifi_with_data(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WiFi data -> correct counts and lists."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_clients_poor_signal": _vector_response("3"),
        "homelab_unifi_clients_poor_satisfaction": _vector_response("2"),
        "homelab_unifi_clients_high_retries": _vector_response("1"),
        "homelab_unifi_ssid_client_count": {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"ssid": "HomeWiFi"}, "value": [1714867200.0, "5"]},
                    {"metric": {"ssid": "GuestWiFi"}, "value": [1714867200.0, "2"]},
                ],
            },
        },
        "homelab_unifi_client_count_by_band": {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"band": "2.4ghz"}, "value": [1714867200.0, "4"]},
                    {"metric": {"band": "5ghz"}, "value": [1714867200.0, "3"]},
                ],
            },
        },
        "homelab_unifi_client_count_by_link": {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"link": "wireless"}, "value": [1714867200.0, "5"]},
                    {"metric": {"link": "wired"}, "value": [1714867200.0, "2"]},
                ],
            },
        },
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/wifi")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["poor_signal"] == _POOR_SIGNAL
    assert body["poor_satisfaction"] == _POOR_SAT
    assert body["high_retries"] == _HIGH_RETRIES
    assert len(body["ssids"]) == _SSIDS_COUNT
    assert body["ssids"][0]["ssid"] == "HomeWiFi"
    assert body["ssids"][0]["count"] == _SSID_1_COUNT
    assert len(body["by_band"]) == _BAND_COUNT
    assert body["by_band"][0]["key"] == "2.4ghz"
    assert body["by_band"][0]["count"] == _BAND_1_COUNT
    assert len(body["by_link"]) == _LINK_COUNT
    assert body["by_link"][0]["key"] == "wireless"
    assert body["by_link"][0]["count"] == _LINK_1_COUNT


# ── _sample_float None branch ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sample_float_non_numeric_defaults(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric VM sample value -> _sample_float returns None -> field defaults."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    # Use /network/wan which maps many float fields; feed a non-numeric value
    # for latency to exercise the ValueError branch in _sample_float.
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_wan_latency_seconds": _vector_response("not-a-number"),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/wan")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    # NaN is non-numeric for float() -> _sample_float returns None -> latency_seconds=None
    assert body["latency_seconds"] is None


# ── /devices tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_devices_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty VM vectors -> devices=[]."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {}
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/devices")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["devices"] == []


@pytest.mark.asyncio
async def test_devices_with_data(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device state + per-device metrics -> device row fields populated."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_device_state": _vector_response(
            "1", labels={"device": "USW-Pro-48", "model": "USW-Pro-48-G2", "kind": "switch"}
        ),
        "homelab_unifi_device_cpu_percent": _vector_response(
            "42.5", labels={"device": "USW-Pro-48"}
        ),
        "homelab_unifi_device_mem_percent": _vector_response(
            "30.0", labels={"device": "USW-Pro-48"}
        ),
        "homelab_unifi_device_temperature_celsius": _vector_response(
            "55.0", labels={"device": "USW-Pro-48"}
        ),
        "homelab_unifi_device_uptime_seconds": _vector_response(
            "86400", labels={"device": "USW-Pro-48"}
        ),
        "homelab_unifi_device_update_available": _vector_response(
            "0", labels={"device": "USW-Pro-48"}
        ),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/devices")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert len(body["devices"]) == 1
    dev = body["devices"][0]
    assert dev["mac"] == "USW-Pro-48"
    assert dev["model"] == "USW-Pro-48-G2"
    assert dev["kind"] == "switch"
    assert dev["up"] is True
    assert dev["cpu_pct"] == 42.5  # noqa: PLR2004
    assert dev["mem_pct"] == 30.0  # noqa: PLR2004
    assert dev["update_available"] is False
    assert "satisfaction" not in body["devices"][0]


# ── /devices/{device} tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_devices_duplicate_state_sample_and_metric_unknown_device(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two state samples for same device -> index only once (branch 501->499).
    CPU sample for unknown device -> skipped (branch 524->522)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        # Two samples with same device name -> hits the `if device_name not in` False branch  # noqa: E501
        "homelab_unifi_device_state": {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"device": "SW1", "model": "USW-Lite-8-PoE", "kind": "switch"},
                        "value": [1714867200.0, "1"],
                    },
                    {
                        "metric": {"device": "SW1", "model": "USW-Lite-8-PoE", "kind": "switch"},
                        "value": [1714867200.0, "1"],
                    },
                ],
            },
        },
        # CPU sample for unknown device -> hits `if device_name in devices_dict` False
        "homelab_unifi_device_cpu_percent": _vector_response(
            "20.0", labels={"device": "UNKNOWN-DEVICE"}
        ),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/devices")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    # Duplicate state sample -> only one device in result
    assert len(body["devices"]) == 1
    assert body["devices"][0]["mac"] == "SW1"
    # CPU from unknown device was ignored
    assert body["devices"][0]["cpu_pct"] is None


@pytest.mark.asyncio
async def test_device_detail_not_found(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """device_state empty vector -> 404 not_found."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {}
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/devices/no-such-device")
    assert resp.status_code == _HTTP_NOT_FOUND


@pytest.mark.asyncio
async def test_device_detail_found(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """device_state present -> 200 with ports/radios/outlets/temps."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    device = "USW-Pro-48"
    queries: dict[str, dict[str, object]] = {
        f'homelab_unifi_device_state{{device="{device}"}}': _vector_response(
            "1", labels={"device": device, "model": "USW-Pro-48-G2", "kind": "switch"}
        ),
        f'homelab_unifi_port_up{{device="{device}"}}': _vector_response(
            "1", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_speed_bps{{device="{device}"}}': _vector_response(
            "1000000000", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_poe_power_watts{{device="{device}"}}': _vector_response(
            "8.5", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_poe_current_ma{{device="{device}"}}': _vector_response(
            "175.0", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_poe_voltage{{device="{device}"}}': _vector_response(
            "48.0", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_poe_good{{device="{device}"}}': _vector_response(
            "1", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_rx_bytes{{device="{device}"}}': _vector_response(
            "123456", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_tx_bytes{{device="{device}"}}': _vector_response(
            "654321", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_rx_errors{{device="{device}"}}': _vector_response(
            "0", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_tx_errors{{device="{device}"}}': _vector_response(
            "0", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_rx_dropped{{device="{device}"}}': _vector_response(
            "0", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_tx_dropped{{device="{device}"}}': _vector_response(
            "0", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_mac_table_count{{device="{device}"}}': _vector_response(
            "12", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_link_down_count{{device="{device}"}}': _vector_response(
            "2", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_port_satisfaction{{device="{device}"}}': _vector_response(
            "95.0", labels={"device": device, "port": "1"}
        ),
        f'homelab_unifi_radio_cu_total{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_radio_cu_self_rx{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_radio_cu_self_tx{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_radio_num_sta{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_radio_tx_power{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_radio_tx_retries_pct{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_radio_satisfaction{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_radio_channel{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_radio_bandwidth_mhz{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_outlet_relay_state{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_device_temperature_celsius{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_device_cpu_percent{{device="{device}"}}': _vector_response(
            "50.0", labels={"device": device}
        ),
        f'homelab_unifi_device_mem_percent{{device="{device}"}}': _vector_response(
            "60.0", labels={"device": device}
        ),
        f'homelab_unifi_device_load1{{device="{device}"}}': _vector_response(
            "0.75", labels={"device": device}
        ),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get(f"/api/integrations/unifi/devices/{device}")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["mac"] == device
    assert len(body["ports"]) == 1
    assert body["ports"][0]["port_idx"] == "1"
    assert body["ports"][0]["up"] is True
    assert body["ports"][0]["speed_bps"] == 1000000000  # noqa: PLR2004
    assert body["ports"][0]["poe_power_watts"] == 8.5  # noqa: PLR2004
    assert body["ports"][0]["poe_current_ma"] == 175.0  # noqa: PLR2004
    assert body["ports"][0]["poe_voltage"] == 48.0  # noqa: PLR2004
    assert body["ports"][0]["poe_good"] is True
    assert body["ports"][0]["rx_bytes"] == 123456  # noqa: PLR2004
    assert body["ports"][0]["tx_bytes"] == 654321  # noqa: PLR2004
    assert body["ports"][0]["rx_errors"] == 0.0
    assert body["ports"][0]["tx_errors"] == 0.0
    assert body["ports"][0]["rx_dropped"] == 0.0
    assert body["ports"][0]["tx_dropped"] == 0.0
    assert body["ports"][0]["mac_table_count"] == 12  # noqa: PLR2004
    assert body["ports"][0]["link_down_count"] == 2  # noqa: PLR2004
    assert body["ports"][0]["satisfaction"] == 95.0  # noqa: PLR2004
    assert "speed_mbps" not in body["ports"][0]
    assert "errors" not in body["ports"][0]
    assert body["cpu_pct"] == 50.0  # noqa: PLR2004
    assert body["mem_pct"] == 60.0  # noqa: PLR2004
    assert body["load"] == 0.75  # noqa: PLR2004


@pytest.mark.asyncio
async def test_device_detail_with_radios_outlets_temps(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device with radio, outlet, and temp samples -> all loop bodies exercised."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    device = "UAP-AC-Pro"
    queries: dict[str, dict[str, object]] = {
        f'homelab_unifi_device_state{{device="{device}"}}': _vector_response(
            "1", labels={"device": device, "model": "UAP-AC-Pro", "kind": "ap"}
        ),
        f'homelab_unifi_port_up{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_speed_bps{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_poe_power_watts{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_poe_current_ma{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_poe_voltage{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_poe_good{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_rx_bytes{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_tx_bytes{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_rx_errors{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_tx_errors{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_rx_dropped{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_tx_dropped{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_mac_table_count{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_link_down_count{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_port_satisfaction{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_radio_cu_total{{device="{device}"}}': _vector_response(
            "35.0", labels={"device": device, "radio": "ng"}
        ),
        f'homelab_unifi_radio_cu_self_rx{{device="{device}"}}': _vector_response(
            "10.0", labels={"device": device, "radio": "ng"}
        ),
        f'homelab_unifi_radio_cu_self_tx{{device="{device}"}}': _vector_response(
            "5.0", labels={"device": device, "radio": "ng"}
        ),
        f'homelab_unifi_radio_num_sta{{device="{device}"}}': _vector_response(
            "8", labels={"device": device, "radio": "ng"}
        ),
        f'homelab_unifi_radio_tx_power{{device="{device}"}}': _vector_response(
            "23.0", labels={"device": device, "radio": "ng"}
        ),
        f'homelab_unifi_radio_tx_retries_pct{{device="{device}"}}': _vector_response(
            "3.5", labels={"device": device, "radio": "ng"}
        ),
        f'homelab_unifi_radio_satisfaction{{device="{device}"}}': _vector_response(
            "88.0", labels={"device": device, "radio": "ng"}
        ),
        f'homelab_unifi_radio_channel{{device="{device}"}}': _vector_response(
            "6", labels={"device": device, "radio": "ng"}
        ),
        f'homelab_unifi_radio_bandwidth_mhz{{device="{device}"}}': _vector_response(
            "40", labels={"device": device, "radio": "ng"}
        ),
        f'homelab_unifi_outlet_relay_state{{device="{device}"}}': _vector_response(
            "1", labels={"device": device, "outlet": "1", "name": "plug1"}
        ),
        f'homelab_unifi_device_temperature_celsius{{device="{device}"}}': _vector_response(
            "45.0", labels={"device": device, "name": "CPU", "type": "cpu"}
        ),
        f'homelab_unifi_device_cpu_percent{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_device_mem_percent{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
        f'homelab_unifi_device_load1{{device="{device}"}}': {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        },
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get(f"/api/integrations/unifi/devices/{device}")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["mac"] == device
    assert len(body["radios"]) == 1
    assert body["radios"][0]["radio"] == "ng"
    assert body["radios"][0]["cu_total"] == 35.0  # noqa: PLR2004
    assert body["radios"][0]["cu_self_rx"] == 10.0  # noqa: PLR2004
    assert body["radios"][0]["cu_self_tx"] == 5.0  # noqa: PLR2004
    assert body["radios"][0]["num_sta"] == 8  # noqa: PLR2004
    assert body["radios"][0]["tx_power"] == 23.0  # noqa: PLR2004
    assert body["radios"][0]["tx_retries_pct"] == 3.5  # noqa: PLR2004
    assert body["radios"][0]["satisfaction"] == 88.0  # noqa: PLR2004
    assert body["radios"][0]["channel"] == 6  # noqa: PLR2004
    assert body["radios"][0]["bandwidth_mhz"] == 40  # noqa: PLR2004
    assert len(body["outlets"]) == 1
    assert body["outlets"][0]["outlet"] == "1"
    assert body["outlets"][0]["name"] == "plug1"
    assert body["outlets"][0]["relay_state"] is True
    assert len(body["temps"]) == 1
    assert body["temps"][0]["name"] == "CPU"
    assert body["temps"][0]["celsius"] == 45.0  # noqa: PLR2004


# ── /dpi tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_device_detail_port_absent_metrics_are_none(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Port up present but all other port metrics absent -> None fields."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    device = "USW-Lite"
    queries: dict[str, dict[str, object]] = {
        f'homelab_unifi_device_state{{device="{device}"}}': _vector_response(
            "1", labels={"device": device, "model": "USW-Lite-8-PoE", "kind": "switch"}
        ),
        f'homelab_unifi_port_up{{device="{device}"}}': _vector_response(
            "1", labels={"device": device, "port": "2"}
        ),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get(f"/api/integrations/unifi/devices/{device}")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    port = body["ports"][0]
    assert port["port_idx"] == "2"
    assert port["up"] is True
    assert port["speed_bps"] is None
    assert port["poe_power_watts"] is None
    assert port["poe_current_ma"] is None
    assert port["poe_voltage"] is None
    assert port["poe_good"] is None
    assert port["rx_bytes"] is None
    assert port["tx_bytes"] is None
    assert port["rx_errors"] is None
    assert port["tx_errors"] is None
    assert port["rx_dropped"] is None
    assert port["tx_dropped"] is None
    assert port["mac_table_count"] is None
    assert port["link_down_count"] is None
    assert port["satisfaction"] is None
    assert body["radios"] == []


@pytest.mark.asyncio
async def test_dpi_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty DPI vector -> apps=[]."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {}
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/dpi")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["apps"] == []


@pytest.mark.asyncio
async def test_dpi_with_data(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DPI samples -> mapped rows with client/app/cat/bytes."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_client_dpi_bytes": {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"client": "aa:bb:cc:dd:ee:ff", "app": "netflix", "cat": "video"},
                        "value": [1714867200.0, "123456"],
                    }
                ],
            },
        },
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/dpi")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert len(body["apps"]) == 1
    assert body["apps"][0]["client"] == "aa:bb:cc:dd:ee:ff"
    assert body["apps"][0]["app"] == "netflix"
    assert body["apps"][0]["cat"] == "video"
    assert body["apps"][0]["bytes"] == _DPI_BYTES


# ── /teleport tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_teleport_up(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teleport up=1 with version label -> teleport_up=True, version populated."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_teleport_up": _vector_response("1"),
        "homelab_unifi_teleport_version": _vector_response("1", labels={"version": "3.4.5"}),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/teleport")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["teleport_up"] is True
    assert body["version"] == "3.4.5"
    assert body["reason"] is None


@pytest.mark.asyncio
async def test_teleport_down_no_sample(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teleport down with empty up_samples (no sample at all) -> reason=None."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    # Empty vector: _bool_metric([]) == False so not teleport_up == True,
    # but first_sample([]) == None -> reason stays None (branch 796->799).
    queries: dict[str, dict[str, object]] = {}
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/teleport")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["teleport_up"] is False
    assert body["reason"] is None


@pytest.mark.asyncio
async def test_teleport_down_with_reason(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teleport up=0 with reason label -> teleport_up=False, reason populated."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_teleport_up": _vector_response("0", labels={"reason": "tunnel_error"}),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/teleport")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["teleport_up"] is False
    assert body["reason"] == "tunnel_error"
    assert body["version"] is None


# ── /controller-health tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_controller_health_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty vectors -> controller_up=False, up_reasons=[], api_took_seconds=[]."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {}
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/controller-health")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["controller_up"] is False
    assert body["up_reasons"] == []
    assert body["api_took_seconds"] == []


@pytest.mark.asyncio
async def test_controller_health_up_no_reason_label(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Controller up=1 with no reason label -> up_reasons=[] (no reason in labels)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_up": _vector_response("1"),  # no reason label
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/controller-health")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["controller_up"] is True
    assert body["up_reasons"] == []


@pytest.mark.asyncio
async def test_controller_health_with_data(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Controller up=1 with reason + api_took -> fields populated."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_up": _vector_response("1", labels={"reason": "ok"}),
        "homelab_unifi_api_took_seconds": _vector_response(
            "0.05", labels={"endpoint": "/stat/sta"}
        ),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/controller-health")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["controller_up"] is True
    assert body["up_reasons"] == ["ok"]
    assert len(body["api_took_seconds"]) == 1
    assert body["api_took_seconds"][0]["endpoint"] == "/stat/sta"
    assert body["api_took_seconds"][0]["seconds"] == 0.05  # noqa: PLR2004


# ── /network/wan tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wan_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty WAN vectors -> all None/False defaults."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {}
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/wan")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["wan_up"] is False
    assert body["latency_seconds"] is None
    assert body["download_mbps"] is None
    assert body["failover_capable"] is False
    assert body["failover_active"] is False


@pytest.mark.asyncio
async def test_wan_with_data(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WAN up=1 + latency + speedtest download -> fields populated."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_wan_up": _vector_response("1"),
        "homelab_unifi_wan_latency_seconds": _vector_response("0.012"),
        "homelab_unifi_speedtest_download_mbps": _vector_response("500.0"),
        "homelab_unifi_wan_failover_capable": _vector_response("1"),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/wan")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["wan_up"] is True
    assert body["latency_seconds"] == 0.012  # noqa: PLR2004
    assert body["download_mbps"] == 500.0  # noqa: PLR2004
    assert body["failover_capable"] is True


# ── /network/dhcp pool_bounds_and_enabled ─────────────────────────────────────


@pytest.mark.asyncio
async def test_dhcp_samples_with_empty_network_label(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DHCP samples with missing network label -> skipped (no network in labels)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    # Each metric returns sample with no network label -> all if-network branches False  # noqa: E501
    empty_network_response: dict[str, object] = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {}, "value": [1714867200.0, "1"]},
            ],
        },
    }
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_dhcp_pool_size": empty_network_response,
        "homelab_unifi_dhcp_pool_start": empty_network_response,
        "homelab_unifi_dhcp_pool_end": empty_network_response,
        "homelab_unifi_dhcp_reservation_count": empty_network_response,
        "homelab_unifi_client_count_by_network": empty_network_response,
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/dhcp")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    # All samples skipped -> no networks
    assert body["networks"] == []


@pytest.mark.asyncio
async def test_dhcp_pool_bounds_and_enabled(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DHCP pool_start/end + reservations + clients -> dhcp_enabled from pool presence."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_dhcp_pool_size": _vector_response("50", labels={"network": "LAN"}),
        "homelab_unifi_dhcp_pool_start": _vector_response("10.0.0.100", labels={"network": "LAN"}),
        "homelab_unifi_dhcp_pool_end": _vector_response("10.0.0.149", labels={"network": "LAN"}),
        "homelab_unifi_dhcp_reservation_count": _vector_response("3", labels={"network": "LAN"}),
        "homelab_unifi_client_count_by_network": _vector_response("10", labels={"network": "LAN"}),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/dhcp")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert len(body["networks"]) == 1
    net = body["networks"][0]
    assert net["network"] == "LAN"
    assert net["pool_size"] == 50.0  # noqa: PLR2004
    assert net["pool_start"] == "10.0.0.100"
    assert net["pool_end"] == "10.0.0.149"
    assert net["dhcp_enabled"] is True
    assert net["reservation_count"] == 3  # noqa: PLR2004
    assert net["occupancy"] == 0.2  # noqa: PLR2004  # 10 / 50


@pytest.mark.asyncio
async def test_dhcp_disabled_from_missing_pool_metrics(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network with only clients+reservations (no pool metrics) -> dhcp_enabled=False."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_dhcp_reservation_count": _vector_response(
            "2", labels={"network": "Default"}
        ),
        "homelab_unifi_client_count_by_network": _vector_response(
            "57", labels={"network": "Default"}
        ),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/dhcp")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert len(body["networks"]) == 1
    net = body["networks"][0]
    assert net["network"] == "Default"
    assert net["pool_size"] is None
    assert net["pool_start"] is None
    assert net["pool_end"] is None
    assert net["dhcp_enabled"] is False  # no pool metrics -> not DHCP-enabled
    assert net["reservation_count"] == 2  # noqa: PLR2004
    assert net["occupancy"] is None  # pool_size is None


# ── /network/dns-posture tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dns_posture_empty(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty DNS posture vector -> networks=[]."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {}
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/dns-posture")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["networks"] == []


@pytest.mark.asyncio
async def test_dns_posture_with_data(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS posture sample with network+dns labels -> handout row."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_dhcp_dns_primary": _vector_response(
            "1", labels={"network": "LAN", "dns": "192.168.2.1"}
        ),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/dns-posture")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert len(body["networks"]) == 1
    assert body["networks"][0]["network"] == "LAN"
    assert body["networks"][0]["dns"] == "192.168.2.1"


@pytest.mark.asyncio
async def test_dns_posture_drift_true(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handout dns != configured expected -> drift True, expected_dns echoed."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_EXPECTED_DNS_STEERING_IP", "192.168.2.148")
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_dhcp_dns_primary": _vector_response(
            "1", labels={"network": "LAN", "dns": "192.168.2.1"}
        ),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/dns-posture")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert len(body["networks"]) == 1
    row = body["networks"][0]
    assert row["network"] == "LAN"
    assert row["dns"] == "192.168.2.1"
    assert row["expected_dns"] == "192.168.2.148"
    assert row["drift"] is True


@pytest.mark.asyncio
async def test_dns_posture_drift_false_match(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handout dns == configured expected -> drift False, expected_dns echoed."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_EXPECTED_DNS_STEERING_IP", "192.168.2.148")
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_dhcp_dns_primary": _vector_response(
            "1", labels={"network": "LAN", "dns": "192.168.2.148"}
        ),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/dns-posture")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert len(body["networks"]) == 1
    row = body["networks"][0]
    assert row["expected_dns"] == "192.168.2.148"
    assert row["drift"] is False


@pytest.mark.asyncio
async def test_dns_posture_expected_unset(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty expected env -> expected_dns None, drift always False (no false positives)."""
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)
    monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_EXPECTED_DNS_STEERING_IP", "")
    queries: dict[str, dict[str, object]] = {
        "homelab_unifi_dhcp_dns_primary": _vector_response(
            "1", labels={"network": "LAN", "dns": "192.168.2.1"}
        ),
    }
    httpx_mock.add_callback(
        _make_callback(queries),
        url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
        method="GET",
        is_reusable=True,
    )
    resp = await authenticated_client.get("/api/integrations/unifi/network/dns-posture")
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert len(body["networks"]) == 1
    row = body["networks"][0]
    assert row["expected_dns"] is None
    assert row["drift"] is False
