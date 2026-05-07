"""Tests for POST /api/alerts/{id}/ack and POST /api/alerts/{id}/dismiss."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient


def _alertmanager_payload(
    *,
    alertname: str = "TestAlert",
    severity: str = "warning",
    fingerprint: str = "",
) -> dict[str, Any]:
    """Build a minimal Alertmanager v2 firing payload."""
    return {
        "version": "4",
        "groupKey": "{}:{alertname}",
        "status": "firing",
        "receiver": "homelab-monitor",
        "groupLabels": {"alertname": alertname},
        "commonLabels": {"alertname": alertname, "severity": severity},
        "commonAnnotations": {},
        "externalURL": "",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": alertname, "severity": severity},
                "annotations": {},
                "startsAt": "2026-05-07T00:00:00+00:00",
                "endsAt": "",
                "generatorURL": "",
                "fingerprint": fingerprint,
            }
        ],
    }


async def _ingest_and_get_id(
    api_token_client: AsyncClient,
    authenticated_client: AsyncClient,
    *,
    alertname: str,
    fingerprint: str,
) -> str:
    """Ingest an alert and return its database id."""
    resp = await api_token_client.post(
        "/api/alerts/ingest",
        json=_alertmanager_payload(alertname=alertname, fingerprint=fingerprint),
    )
    assert resp.status_code == 202  # noqa: PLR2004

    list_resp = await authenticated_client.get(f"/api/alerts?fingerprint={fingerprint}")
    assert list_resp.status_code == 200  # noqa: PLR2004
    items = list_resp.json()["items"]
    assert len(items) == 1
    return str(items[0]["id"])


@pytest.mark.asyncio
async def test_ack_creates_outcome_row_and_sets_ack_at(
    authenticated_client: AsyncClient,
    api_token_client: AsyncClient,
) -> None:
    """POST /{id}/ack returns AckResponse and the alert gets ack_at set."""
    alert_id = await _ingest_and_get_id(
        api_token_client,
        authenticated_client,
        alertname="AckAlert",
        fingerprint="ack-fp-1",
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        f"/api/alerts/{alert_id}/ack",
        json={"comment": None},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["alert_id"] == alert_id
    assert data["ack_at"].endswith("+00:00")

    # Confirm ack_at is set on the alert row via detail endpoint
    detail = await authenticated_client.get(f"/api/alerts/{alert_id}")
    assert detail.status_code == 200  # noqa: PLR2004
    alert = detail.json()["alert"]
    assert alert["ack_at"] is not None
    assert alert["ack_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_ack_404_for_unknown_alert(
    authenticated_client: AsyncClient,
) -> None:
    """POST /{unknown-id}/ack returns 404."""
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/alerts/nonexistent-alert-id-abc/ack",
        json={"comment": None},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ack_requires_csrf(
    authenticated_client: AsyncClient,
    api_token_client: AsyncClient,
) -> None:
    """POST /{id}/ack without CSRF header returns 403."""
    alert_id = await _ingest_and_get_id(
        api_token_client,
        authenticated_client,
        alertname="AckNoCsrf",
        fingerprint="ack-fp-nocsrf",
    )
    resp = await authenticated_client.post(
        f"/api/alerts/{alert_id}/ack",
        json={"comment": None},
        # No X-CSRF-Token header
    )
    assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_dismiss_creates_outcome_row_does_not_change_status(
    authenticated_client: AsyncClient,
    api_token_client: AsyncClient,
) -> None:
    """POST /{id}/dismiss records outcome but leaves status=firing."""
    alert_id = await _ingest_and_get_id(
        api_token_client,
        authenticated_client,
        alertname="DismissAlert",
        fingerprint="dismiss-fp-1",
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        f"/api/alerts/{alert_id}/dismiss",
        json={"comment": None},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    data = resp.json()
    assert data["alert_id"] == alert_id
    assert data["dismissed_at"].endswith("+00:00")

    # Status must remain firing — dismissal doesn't resolve
    detail = await authenticated_client.get(f"/api/alerts/{alert_id}")
    assert detail.status_code == 200  # noqa: PLR2004
    alert = detail.json()["alert"]
    assert alert["status"] == "firing"


@pytest.mark.asyncio
async def test_dismiss_404_for_unknown_alert(
    authenticated_client: AsyncClient,
) -> None:
    """POST /{unknown-id}/dismiss returns 404."""
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/alerts/nonexistent-alert-id-def/dismiss",
        json={"comment": None},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_outcomes_listed_in_get_detail(
    authenticated_client: AsyncClient,
    api_token_client: AsyncClient,
) -> None:
    """After ack + dismiss, GET /{id} outcomes list contains both entries in descending order."""
    alert_id = await _ingest_and_get_id(
        api_token_client,
        authenticated_client,
        alertname="MultiOutcome",
        fingerprint="multi-outcome-fp",
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")

    await authenticated_client.post(
        f"/api/alerts/{alert_id}/ack",
        json={"comment": None},
        headers={"X-CSRF-Token": csrf or ""},
    )
    await authenticated_client.post(
        f"/api/alerts/{alert_id}/dismiss",
        json={"comment": None},
        headers={"X-CSRF-Token": csrf or ""},
    )

    detail = await authenticated_client.get(f"/api/alerts/{alert_id}")
    assert detail.status_code == 200  # noqa: PLR2004
    outcomes = detail.json()["outcomes"]
    assert len(outcomes) == 2  # noqa: PLR2004
    outcome_values = [o["outcome"] for o in outcomes]
    assert "acked" in outcome_values
    assert "dismissed" in outcome_values
    # Most-recent first (dismissed came after ack)
    assert outcomes[0]["outcome"] == "dismissed"
