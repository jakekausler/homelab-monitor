"""Tests for POST /api/alerts/ingest."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

# Project test conventions:
# - Framework: pytest-asyncio with authenticated_client / api_token_client fixtures
# - Assertions:
# - Inline imports:
# - Private access: # pyright: ignore[reportPrivateUsage]


def _alertmanager_payload(
    *,
    status: str = "firing",
    alertname: str = "TestAlert",
    severity: str = "warning",
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
                "fingerprint": "",  # let the server compute
            }
        ],
    }


@pytest.mark.asyncio
async def test_ingest_firing_creates_row_and_dispatches(
    api_token_client: AsyncClient,
) -> None:
    """POST /api/alerts/ingest firing creates a DB row and returns 202."""
    payload = _alertmanager_payload(alertname="DiskFull", severity="critical")
    resp = await api_token_client.post("/api/alerts/ingest", json=payload)
    assert resp.status_code == 202  # noqa: PLR2004
    data = resp.json()
    assert data["received"] == 1
    assert data["ingested"] == 1


@pytest.mark.asyncio
async def test_ingest_dedup_same_fingerprint(
    api_token_client: AsyncClient,
) -> None:
    """Two firing POSTs with the same labels dedup to one row (ingested=1 both times)."""
    payload = _alertmanager_payload(alertname="DedupAlert", severity="warning")
    resp1 = await api_token_client.post("/api/alerts/ingest", json=payload)
    assert resp1.status_code == 202  # noqa: PLR2004
    assert resp1.json()["ingested"] == 1

    resp2 = await api_token_client.post("/api/alerts/ingest", json=payload)
    assert resp2.status_code == 202  # noqa: PLR2004
    # Second ingest bumps last_seen — still ingested=1 (state-changed)
    assert resp2.json()["ingested"] == 1
    assert resp2.json()["received"] == 1


@pytest.mark.asyncio
async def test_ingest_resolved_marks_row(
    api_token_client: AsyncClient,
    authenticated_client: AsyncClient,
) -> None:
    """firing then resolved: GET /api/alerts shows status=resolved."""
    payload_fire = _alertmanager_payload(alertname="ResolveMe", severity="warning")
    resp = await api_token_client.post("/api/alerts/ingest", json=payload_fire)
    assert resp.status_code == 202  # noqa: PLR2004

    payload_resolve = _alertmanager_payload(
        status="resolved", alertname="ResolveMe", severity="warning"
    )
    resp2 = await api_token_client.post("/api/alerts/ingest", json=payload_resolve)
    assert resp2.status_code == 202  # noqa: PLR2004
    assert resp2.json()["ingested"] == 1

    # Verify via listing endpoint (session auth)
    list_resp = await authenticated_client.get("/api/alerts?status=resolved")
    assert list_resp.status_code == 200  # noqa: PLR2004
    items = list_resp.json()["items"]
    names = [it["labels"].get("alertname") for it in items]
    assert "ResolveMe" in names


@pytest.mark.asyncio
async def test_ingest_resolved_without_prior_firing_ignored(
    api_token_client: AsyncClient,
) -> None:
    """resolved with no prior firing row: ingested=0, no error."""
    payload = _alertmanager_payload(status="resolved", alertname="GhostAlert", severity="info")
    resp = await api_token_client.post("/api/alerts/ingest", json=payload)
    assert resp.status_code == 202  # noqa: PLR2004
    data = resp.json()
    assert data["received"] == 1
    assert data["ingested"] == 0


@pytest.mark.asyncio
async def test_ingest_refire_after_resolve_creates_new_row(
    api_token_client: AsyncClient,
    authenticated_client: AsyncClient,
) -> None:
    """fire → resolve → fire again creates a second distinct row."""
    payload_fire = _alertmanager_payload(alertname="Flapper", severity="warning")
    await api_token_client.post("/api/alerts/ingest", json=payload_fire)

    payload_resolve = _alertmanager_payload(
        status="resolved", alertname="Flapper", severity="warning"
    )
    await api_token_client.post("/api/alerts/ingest", json=payload_resolve)

    # Fire again
    resp = await api_token_client.post("/api/alerts/ingest", json=payload_fire)
    assert resp.status_code == 202  # noqa: PLR2004
    assert resp.json()["ingested"] == 1

    # Two rows total: one resolved + one firing
    list_resp = await authenticated_client.get("/api/alerts")
    assert list_resp.status_code == 200  # noqa: PLR2004
    items = [it for it in list_resp.json()["items"] if it["labels"].get("alertname") == "Flapper"]
    statuses = {it["status"] for it in items}
    assert "firing" in statuses
    assert "resolved" in statuses


@pytest.mark.asyncio
async def test_ingest_severity_default_warning_when_missing(
    api_token_client: AsyncClient,
    authenticated_client: AsyncClient,
) -> None:
    """Alert without severity label defaults to warning in the DB row."""
    payload: dict[str, Any] = {
        "version": "4",
        "groupKey": "{}:{alertname}",
        "status": "firing",
        "receiver": "homelab-monitor",
        "groupLabels": {"alertname": "NoSeverity"},
        "commonLabels": {"alertname": "NoSeverity"},
        "commonAnnotations": {},
        "externalURL": "",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "NoSeverity"},  # no severity key
                "annotations": {},
                "startsAt": "2026-05-07T00:00:00+00:00",
                "endsAt": "",
                "generatorURL": "",
                "fingerprint": "no-severity-fp",
            }
        ],
    }
    resp = await api_token_client.post("/api/alerts/ingest", json=payload)
    assert resp.status_code == 202  # noqa: PLR2004

    list_resp = await authenticated_client.get("/api/alerts?severity=warning")
    assert list_resp.status_code == 200  # noqa: PLR2004
    items = list_resp.json()["items"]
    names = [it["labels"].get("alertname") for it in items]
    assert "NoSeverity" in names


@pytest.mark.asyncio
async def test_ingest_token_auth_succeeds(api_token_client: AsyncClient) -> None:
    """Bearer token with ALERTS_INGEST_WRITE scope returns 202."""
    payload = _alertmanager_payload(alertname="TokenAuth", severity="info")
    resp = await api_token_client.post("/api/alerts/ingest", json=payload)
    assert resp.status_code == 202  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ingest_session_auth_succeeds(authenticated_client: AsyncClient) -> None:
    """Cookie session with CSRF returns 202."""
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    payload = _alertmanager_payload(alertname="SessionAuth", severity="warning")
    resp = await authenticated_client.post(
        "/api/alerts/ingest",
        json=payload,
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == 202  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ingest_anonymous_returns_401() -> None:
    """No auth header returns 401."""
    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = _alertmanager_payload()
        resp = await client.post("/api/alerts/ingest", json=payload)
        assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ingest_session_without_csrf_returns_403(
    authenticated_client: AsyncClient,
) -> None:
    """Session cookie without CSRF header returns 403."""
    payload = _alertmanager_payload(alertname="NoCsrf", severity="warning")
    resp = await authenticated_client.post("/api/alerts/ingest", json=payload)
    # No X-CSRF-Token header → CSRF check fails
    assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ingest_token_without_required_scope_returns_403(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token with READ_STATUS but not ALERTS_INGEST_WRITE returns 403."""
    import base64  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
        from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

        plaintext, _ = make_api_token(prefix="test")
        await app.state.auth_repo.create_api_token(
            name="read-only-token",
            scopes={Scope.READ_STATUS},  # missing ALERTS_INGEST_WRITE
            plaintext_token=plaintext,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            payload = _alertmanager_payload()
            resp = await client.post("/api/alerts/ingest", json=payload)
            assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ingest_malformed_payload_returns_422(api_token_client: AsyncClient) -> None:
    """Malformed body (missing required fields) returns 422."""
    resp = await api_token_client.post(
        "/api/alerts/ingest",
        json={"not": "valid"},
    )
    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ingest_invalid_severity_falls_back_to_warning(
    api_token_client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Severity label that is not a valid Severity enum value falls back to WARNING."""
    payload = _alertmanager_payload(
        alertname="WeirdAlert",
        severity="neutral",  # not a valid Severity (info/warning/critical)
    )
    with caplog.at_level(logging.WARNING, logger="homelab_monitor"):
        resp = await api_token_client.post("/api/alerts/ingest", json=payload)
    assert resp.status_code == 202  # noqa: PLR2004
    assert resp.json()["ingested"] == 1

    # Confirm the warning was logged (structlog uses module-level logger names)
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("alerts.ingest.severity_invalid" in r.getMessage() for r in warning_records)


@pytest.mark.asyncio
async def test_ingest_does_not_log_full_payload_at_info(
    api_token_client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ingest handler must NOT log full label dicts at INFO level."""
    secret_label_value = "super-secret-hostname-12345"
    payload = _alertmanager_payload(
        alertname="SecretAlert", severity="warning", host=secret_label_value
    )
    with caplog.at_level(logging.INFO, logger="homelab_monitor"):
        resp = await api_token_client.post("/api/alerts/ingest", json=payload)
    assert resp.status_code == 202  # noqa: PLR2004
    for record in caplog.records:
        if record.levelno == logging.INFO:
            assert secret_label_value not in record.getMessage()


@pytest.mark.asyncio
async def test_warning_does_not_leak_host_label(
    api_token_client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F10: WARNING-level records (e.g., severity_invalid) must not leak label values.

    Triggers ``alerts.ingest.severity_invalid`` (invalid severity) AND
    captures all WARNING records to assert no record contains the host
    label value.
    """
    secret_host = "private-internal-host-9999"
    payload = _alertmanager_payload(
        alertname="WarnLeakTest",
        severity="not_a_real_level",  # forces severity_invalid warning
        host=secret_host,
    )
    with caplog.at_level(logging.WARNING, logger="homelab_monitor"):
        resp = await api_token_client.post("/api/alerts/ingest", json=payload)
    assert resp.status_code == 202  # noqa: PLR2004

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("alerts.ingest.severity_invalid" in r.getMessage() for r in warning_records), (
        "expected severity_invalid warning to fire"
    )
    for r in warning_records:
        assert secret_host not in r.getMessage(), (
            f"WARNING record leaked host label: {r.getMessage()!r}"
        )


@pytest.mark.asyncio
async def test_ingest_token_with_only_ingest_scope_returns_202(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F21: a token holding ONLY ALERTS_INGEST_WRITE (no other scopes) succeeds."""
    import base64  # noqa: PLC0415

    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
        from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

        plaintext, _ = make_api_token(prefix="test")
        await app.state.auth_repo.create_api_token(
            name="ingest-only-token",
            scopes={Scope.ALERTS_INGEST_WRITE},  # ONLY this scope
            plaintext_token=plaintext,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            payload = _alertmanager_payload(alertname="ScopeOnly", severity="info")
            resp = await client.post("/api/alerts/ingest", json=payload)
            assert resp.status_code == 202  # noqa: PLR2004
            assert resp.json()["ingested"] == 1
