"""Tests for POST /api/integrations/ha/event (HA webhook ingester)."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository

_ACCEPTED = 202
_FORBIDDEN = 403
_UNAUTHENTICATED = 401
_UNPROCESSABLE = 422


@asynccontextmanager
async def _ha_client(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scopes_csv: str = "ha:event:write",
) -> AsyncGenerator[tuple[AsyncClient, SqliteRepository], None]:
    """Spin a self-contained app with a token carrying ``scopes_csv``.

    Yields (client, repo). The real token plaintext is set as the Bearer header;
    the no-leak test recovers it from ``client.headers["Authorization"]``.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
    monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415
    from homelab_monitor.kernel.auth.api_tokens import make_api_token  # noqa: PLC0415
    from homelab_monitor.kernel.auth.scopes import Scope  # noqa: PLC0415

    scope_set = {Scope(s) for s in scopes_csv.split(",") if s}
    app = create_app(lifespan_enabled=True)
    async with app.router.lifespan_context(app):
        plaintext, _ = make_api_token(prefix="test")
        await app.state.auth_repo.create_api_token(
            name="ha-token",
            scopes=scope_set,
            plaintext_token=plaintext,
        )
        repo: SqliteRepository = app.state.repo
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            yield client, repo


async def _fetch_latest_audit(repo: SqliteRepository, what: str) -> tuple[str, str, str]:
    """Return (who, what, after_json) for the most recent audit row matching ``what``."""
    row = await repo.fetch_one(
        text(
            "SELECT who, what, after_json FROM audit_log WHERE what = :w ORDER BY id DESC LIMIT 1"
        ),
        {"w": what},
    )
    assert row is not None
    return row[0], row[1], row[2]


async def test_ha_event_with_scope_returns_202_and_writes_audit(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST with scope returns 202 and writes audit row with api-token principal."""
    async with _ha_client(db_url, master_key, monkeypatch) as (client, repo):
        resp = await client.post(
            "/api/integrations/ha/event",
            json={
                "event_type": "test.event",
                "data": {"entity_id": "sensor.x", "value": 22.5},
                "severity": "warning",
                "title": "Test",
            },
        )
        assert resp.status_code == _ACCEPTED
        assert resp.json() == {"status": "accepted"}

        who, what, after_json = await _fetch_latest_audit(repo, "ha_event.test.event")
        assert who.startswith("api-token:")
        assert what == "ha_event.test.event"

        after = json.loads(after_json)
        assert after["event_type"] == "test.event"
        assert after["data"] == {"entity_id": "sensor.x", "value": 22.5}
        assert after["severity"] == "warning"
        assert after["title"] == "Test"


async def test_ha_event_data_persisted_verbatim(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Event data is persisted verbatim."""
    async with _ha_client(db_url, master_key, monkeypatch) as (client, repo):
        resp = await client.post(
            "/api/integrations/ha/event",
            json={"event_type": "sensor.update", "data": {"entity_id": "sensor.x", "value": 22.5}},
        )
        assert resp.status_code == _ACCEPTED

        _who, _what, after_json = await _fetch_latest_audit(repo, "ha_event.sensor.update")
        after = json.loads(after_json)
        assert after["data"] == {"entity_id": "sensor.x", "value": 22.5}


async def test_ha_event_default_data_is_empty_dict(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing data field defaults to empty dict."""
    async with _ha_client(db_url, master_key, monkeypatch) as (client, repo):
        resp = await client.post(
            "/api/integrations/ha/event",
            json={"event_type": "no.data.event"},
        )
        assert resp.status_code == _ACCEPTED

        _who, _what, after_json = await _fetch_latest_audit(repo, "ha_event.no.data.event")
        after = json.loads(after_json)
        assert after["data"] == {}
        assert after["severity"] is None
        assert after["title"] is None


async def test_ha_event_without_scope_returns_403(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token without ha:event:write scope returns 403."""
    async with _ha_client(db_url, master_key, monkeypatch, scopes_csv="read:status") as (
        client,
        _repo,
    ):
        resp = await client.post(
            "/api/integrations/ha/event",
            json={"event_type": "denied.event"},
        )
        assert resp.status_code == _FORBIDDEN


async def test_ha_event_unauthenticated_returns_401(
    unauthenticated_client: AsyncClient,
) -> None:
    """No auth returns 401."""
    resp = await unauthenticated_client.post(
        "/api/integrations/ha/event", json={"event_type": "x.event"}
    )
    assert resp.status_code == _UNAUTHENTICATED


async def test_ha_event_missing_event_type_returns_422(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing event_type returns 422."""
    async with _ha_client(db_url, master_key, monkeypatch) as (client, _repo):
        resp = await client.post(
            "/api/integrations/ha/event",
            json={"data": {"a": 1}},
        )
        assert resp.status_code == _UNPROCESSABLE


@pytest.mark.parametrize(
    "invalid_event_type",
    [
        "bad event/type",  # space and slash
        "has space",  # space
        "slash/here",  # slash
        "",  # empty (min_length=1)
    ],
)
async def test_ha_event_invalid_event_type_pattern_returns_422(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    invalid_event_type: str,
) -> None:
    """Invalid event_type pattern returns 422."""
    async with _ha_client(db_url, master_key, monkeypatch) as (client, _repo):
        resp = await client.post(
            "/api/integrations/ha/event",
            json={"event_type": invalid_event_type},
        )
        assert resp.status_code == _UNPROCESSABLE


async def test_ha_event_extra_top_level_field_returns_422(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra top-level field returns 422 (extra='forbid')."""
    async with _ha_client(db_url, master_key, monkeypatch) as (client, _repo):
        resp = await client.post(
            "/api/integrations/ha/event",
            json={"event_type": "x.event", "unexpected": "field"},
        )
        assert resp.status_code == _UNPROCESSABLE


async def test_ha_event_invalid_severity_returns_422(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid severity value returns 422."""
    async with _ha_client(db_url, master_key, monkeypatch) as (client, _repo):
        resp = await client.post(
            "/api/integrations/ha/event",
            json={"event_type": "x.event", "severity": "error"},
        )
        assert resp.status_code == _UNPROCESSABLE


async def test_ha_event_does_not_leak_token_or_payload(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bearer token plaintext must not appear in audit rows or logs."""
    async with _ha_client(db_url, master_key, monkeypatch) as (client, repo):
        # Recover the plaintext token from the Authorization header the client carries.
        auth_header = client.headers["Authorization"]
        token_plaintext = auth_header.removeprefix("Bearer ")
        with caplog.at_level(logging.DEBUG, logger="homelab_monitor"):
            resp = await client.post(
                "/api/integrations/ha/event",
                json={"event_type": "leak.check", "data": {"k": "v"}},
            )
        assert resp.status_code == _ACCEPTED
        who, what, after_json = await _fetch_latest_audit(repo, "ha_event.leak.check")
        # Token plaintext must not appear in any audit column.
        assert token_plaintext not in who
        assert token_plaintext not in what
        assert token_plaintext not in (after_json or "")
        # Token plaintext must not appear in captured logs.
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        assert token_plaintext not in log_text
        assert token_plaintext not in repr(caplog.records)


async def test_ha_event_does_not_log_payload_at_info(
    db_url: str,
    master_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Payload body must not appear in INFO-level logs."""
    async with _ha_client(db_url, master_key, monkeypatch) as (client, _repo):
        with caplog.at_level(logging.INFO, logger="homelab_monitor"):
            resp = await client.post(
                "/api/integrations/ha/event",
                json={"event_type": "noisy.event", "data": {"secret_field": "do-not-log-me-12345"}},
            )
        assert resp.status_code == _ACCEPTED
        info_records = [r for r in caplog.records if r.levelno >= logging.INFO]
        joined = "\n".join(r.getMessage() for r in info_records)
        assert "do-not-log-me-12345" not in joined


async def test_ha_event_via_session_writes_user_principal_label(
    authenticated_client: AsyncClient,
) -> None:
    """POST via session auth writes user:<username> principal label."""
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/ha/event",
        json={"event_type": "session.event"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _ACCEPTED
    repo = cast("SqliteRepository", authenticated_client.app.state.repo)  # type: ignore[attr-defined]
    who, _what, _after_json = await _fetch_latest_audit(repo, "ha_event.session.event")
    assert who.startswith("user:")
