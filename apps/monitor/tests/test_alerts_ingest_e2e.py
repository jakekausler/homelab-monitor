"""End-to-end: AM webhook → /api/alerts/ingest → AlertDispatcher → SSE broker."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403


def _am_v2_payload(
    *,
    status: str = "firing",
    alertname: str = "TestAlert",
    severity: str = "warning",
    **labels: str,
) -> dict[str, Any]:
    """Realistic AM v2 webhook payload."""
    return {
        "version": "4",
        "groupKey": "{}:{alertname}",
        "status": status,
        "receiver": "monitor-webhook",
        "groupLabels": {"alertname": alertname},
        "commonLabels": {"alertname": alertname, "severity": severity, **labels},
        "commonAnnotations": {"summary": "test"},
        "externalURL": "http://alertmanager:9093",
        "alerts": [
            {
                "status": status,
                "labels": {"alertname": alertname, "severity": severity, **labels},
                "annotations": {"summary": "synthetic"},
                "startsAt": "2026-05-08T00:00:00+00:00",
                "endsAt": "" if status == "firing" else "2026-05-08T00:05:00+00:00",
                "generatorURL": "http://vmalert/...",
                "fingerprint": "",  # let server compute
            }
        ],
    }


@pytest.mark.asyncio
async def test_am_v2_firing_payload_dispatches_firing_event_to_sse(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST AM v2 firing payload → 202 + AlertFiringEvent published via SSE broker."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        plaintext, _ = make_api_token(prefix="test")
        await app.state.auth_repo.create_api_token(
            name="ingest-test",
            scopes={Scope.ALERTS_INGEST_WRITE},
            plaintext_token=plaintext,
        )
        broker = app.state.broker
        sub = broker.subscribe()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            resp = await client.post(
                "/api/alerts/ingest",
                json=_am_v2_payload(alertname="VmalertSourced", severity="warning"),
            )
            assert resp.status_code == 202  # noqa: PLR2004
        # Drain the broker until we see the alert.firing event.
        while True:
            evt = await asyncio.wait_for(sub.__anext__(), timeout=2.0)
            if evt.kind == "alert.firing":
                assert evt.payload["labels"]["alertname"] == "VmalertSourced"
                break


@pytest.mark.asyncio
async def test_am_v2_resolved_payload_dispatches_resolved_event_to_sse(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """firing then resolved AM v2 → AlertResolvedEvent on SSE broker."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        plaintext, _ = make_api_token(prefix="test")
        await app.state.auth_repo.create_api_token(
            name="ingest-test",
            scopes={Scope.ALERTS_INGEST_WRITE},
            plaintext_token=plaintext,
        )
        broker = app.state.broker
        sub = broker.subscribe()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            # First POST firing
            resp = await client.post(
                "/api/alerts/ingest",
                json=_am_v2_payload(alertname="TestAlert", status="firing"),
            )
            assert resp.status_code == 202  # noqa: PLR2004

            # Drain until firing event arrives
            while True:
                evt = await asyncio.wait_for(sub.__anext__(), timeout=2.0)
                if evt.kind == "alert.firing":
                    assert evt.payload["labels"]["alertname"] == "TestAlert"
                    break

            # Now POST resolved
            resp = await client.post(
                "/api/alerts/ingest",
                json=_am_v2_payload(alertname="TestAlert", status="resolved"),
            )
            assert resp.status_code == 202  # noqa: PLR2004

        # Drain until resolved event arrives
        while True:
            evt = await asyncio.wait_for(sub.__anext__(), timeout=2.0)
            if evt.kind == "alert.resolved":
                assert evt.payload["labels"]["alertname"] == "TestAlert"
                break


@pytest.mark.asyncio
async def test_am_v2_unauthenticated_returns_401(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Authorization header → 401 (covers vmalert misconfiguration)."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        resp = await client.post(
            "/api/alerts/ingest",
            json=_am_v2_payload(),
        )
        assert resp.status_code == _HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_am_v2_wrong_scope_returns_403(
    db_url: str, master_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token with READ_STATUS but not ALERTS_INGEST_WRITE → 403."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    app = create_app(lifespan_enabled=True)
    async with (
        app.router.lifespan_context(app),
    ):
        plaintext, _ = make_api_token(prefix="test")
        await app.state.auth_repo.create_api_token(
            name="read-only-token",
            scopes={Scope.READ_STATUS},
            plaintext_token=plaintext,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            resp = await client.post(
                "/api/alerts/ingest",
                json=_am_v2_payload(),
            )
            assert resp.status_code == _HTTP_FORBIDDEN
