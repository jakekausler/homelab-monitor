"""Tests for GET /api/alerts and GET /api/alerts/{id}."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient


def _alertmanager_payload(
    *,
    status: str = "firing",
    alertname: str = "TestAlert",
    severity: str = "warning",
    fingerprint: str = "",
    **labels: str,
) -> dict[str, Any]:
    """Build a minimal Alertmanager v2 webhook payload."""
    return {
        "version": "4",
        "groupKey": "{}:{alertname}",
        "status": status,
        "receiver": "homelab-monitor",
        "groupLabels": {"alertname": alertname},
        "commonLabels": {"alertname": alertname, "severity": severity, **labels},
        "commonAnnotations": {},
        "externalURL": "",
        "alerts": [
            {
                "status": status,
                "labels": {"alertname": alertname, "severity": severity, **labels},
                "annotations": {},
                "startsAt": "2026-05-07T00:00:00+00:00",
                "endsAt": "" if status == "firing" else "2026-05-07T00:05:00+00:00",
                "generatorURL": "",
                "fingerprint": fingerprint,
            }
        ],
    }


async def _ingest(client: AsyncClient, payload: dict[str, Any]) -> None:
    """POST to /api/alerts/ingest using the api_token_client."""
    resp = await client.post("/api/alerts/ingest", json=payload)
    assert resp.status_code == 202  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_alerts_requires_session() -> None:
    """GET /api/alerts without session returns 401."""
    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/alerts")
        assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_alerts_returns_paginated_items(
    authenticated_client: AsyncClient,
    api_token_client: AsyncClient,
) -> None:
    """GET /api/alerts returns items list and next_cursor fields."""
    await _ingest(
        api_token_client, _alertmanager_payload(alertname="PagAlert1", fingerprint="pag-fp-1")
    )
    await _ingest(
        api_token_client, _alertmanager_payload(alertname="PagAlert2", fingerprint="pag-fp-2")
    )

    resp = await authenticated_client.get("/api/alerts")
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert "items" in data
    assert "next_cursor" in data
    assert len(data["items"]) >= 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_alerts_filter_by_status(
    authenticated_client: AsyncClient,
    api_token_client: AsyncClient,
) -> None:
    """?status=firing only returns firing rows; ?status=resolved returns resolved rows."""
    await _ingest(
        api_token_client, _alertmanager_payload(alertname="StatusFiring", fingerprint="status-fp-f")
    )
    await _ingest(
        api_token_client,
        _alertmanager_payload(alertname="StatusResolved", fingerprint="status-fp-r"),
    )
    # Resolve the second one
    await _ingest(
        api_token_client,
        _alertmanager_payload(
            status="resolved", alertname="StatusResolved", fingerprint="status-fp-r"
        ),
    )

    firing_resp = await authenticated_client.get("/api/alerts?status=firing")
    assert firing_resp.status_code == 200  # noqa: PLR2004
    firing_names = {it["labels"].get("alertname") for it in firing_resp.json()["items"]}
    assert "StatusFiring" in firing_names
    assert "StatusResolved" not in firing_names

    resolved_resp = await authenticated_client.get("/api/alerts?status=resolved")
    assert resolved_resp.status_code == 200  # noqa: PLR2004
    resolved_names = {it["labels"].get("alertname") for it in resolved_resp.json()["items"]}
    assert "StatusResolved" in resolved_names


@pytest.mark.asyncio
async def test_list_alerts_filter_by_severity(
    authenticated_client: AsyncClient,
    api_token_client: AsyncClient,
) -> None:
    """?severity=critical only returns critical-severity rows."""
    await _ingest(
        api_token_client,
        _alertmanager_payload(alertname="CritAlert", severity="critical", fingerprint="sev-fp-c"),
    )
    await _ingest(
        api_token_client,
        _alertmanager_payload(alertname="WarnAlert", severity="warning", fingerprint="sev-fp-w"),
    )

    resp = await authenticated_client.get("/api/alerts?severity=critical")
    assert resp.status_code == 200  # noqa: PLR2004
    names = {it["labels"].get("alertname") for it in resp.json()["items"]}
    assert "CritAlert" in names
    assert "WarnAlert" not in names


@pytest.mark.asyncio
async def test_list_alerts_filter_by_source_tool(
    authenticated_client: AsyncClient,
    api_token_client: AsyncClient,
) -> None:
    """?source_tool=netdata only returns rows with that source_tool."""
    # source_tool is read from labels["source_tool"] in the ingest handler
    await _ingest(
        api_token_client,
        _alertmanager_payload(
            alertname="NetdataAlert",
            severity="warning",
            fingerprint="st-fp-n",
            source_tool="netdata",
        ),
    )
    await _ingest(
        api_token_client,
        _alertmanager_payload(alertname="OtherAlert", severity="warning", fingerprint="st-fp-o"),
    )

    resp = await authenticated_client.get("/api/alerts?source_tool=netdata")
    assert resp.status_code == 200  # noqa: PLR2004
    items = resp.json()["items"]
    assert all(it["source_tool"] == "netdata" for it in items)
    names = {it["labels"].get("alertname") for it in items}
    assert "NetdataAlert" in names


@pytest.mark.asyncio
async def test_list_alerts_filter_by_fingerprint(
    authenticated_client: AsyncClient,
    api_token_client: AsyncClient,
) -> None:
    """?fingerprint=<fp> returns only the matching row."""
    await _ingest(
        api_token_client, _alertmanager_payload(alertname="FpAlert", fingerprint="exact-fp-xyz")
    )
    await _ingest(
        api_token_client,
        _alertmanager_payload(alertname="OtherFpAlert", fingerprint="other-fp-xyz"),
    )

    resp = await authenticated_client.get("/api/alerts?fingerprint=exact-fp-xyz")
    assert resp.status_code == 200  # noqa: PLR2004
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["fingerprint"] == "exact-fp-xyz"


@pytest.mark.asyncio
async def test_get_alert_detail_returns_outcome_history(
    authenticated_client: AsyncClient,
    api_token_client: AsyncClient,
) -> None:
    """GET /api/alerts/{id} returns alert + outcomes + payload."""
    await _ingest(
        api_token_client, _alertmanager_payload(alertname="DetailAlert", fingerprint="detail-fp-1")
    )

    # Get the alert id from listing
    list_resp = await authenticated_client.get("/api/alerts?fingerprint=detail-fp-1")
    assert list_resp.status_code == 200  # noqa: PLR2004
    items = list_resp.json()["items"]
    assert len(items) == 1
    alert_id = items[0]["id"]

    # Ack the alert to create an outcome row
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    ack_resp = await authenticated_client.post(
        f"/api/alerts/{alert_id}/ack",
        json={"comment": None},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert ack_resp.status_code == 200  # noqa: PLR2004

    # Now check detail endpoint
    detail_resp = await authenticated_client.get(f"/api/alerts/{alert_id}")
    assert detail_resp.status_code == 200  # noqa: PLR2004
    data = detail_resp.json()
    assert "alert" in data
    assert "outcomes" in data
    assert "payload" in data
    assert len(data["outcomes"]) >= 1
    assert data["outcomes"][0]["outcome"] == "acked"


@pytest.mark.asyncio
async def test_get_alert_detail_404_for_unknown(
    authenticated_client: AsyncClient,
) -> None:
    """GET /api/alerts/{unknown-id} returns 404."""
    resp = await authenticated_client.get("/api/alerts/nonexistent-alert-id-xyz")
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_listing_token_auth_rejected(api_token_client: AsyncClient) -> None:
    """GET /api/alerts with token auth (no session) returns 401 (session-only endpoint)."""
    resp = await api_token_client.get("/api/alerts")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_alerts_malformed_cursor_returns_400(authenticated_client: AsyncClient) -> None:
    """Malformed cursor in GET /api/alerts returns 400, not 500."""
    resp = await authenticated_client.get("/api/alerts?cursor=not-a-cursor")
    assert resp.status_code == 400  # noqa: PLR2004
