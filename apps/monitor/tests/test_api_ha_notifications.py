"""Tests for GET /api/integrations/home-assistant/notifications (STAGE-005-029).

Live persistent-notification bodies over the HA websocket. Verifies:
- happy path (bare-list AND {"notifications": [...]} shapes),
- HaError -> 502 for both `unreachable` and `auth` (never 401),
- empty payload -> 200 rows=[],
- defensive skipping of malformed notifications,
- bodies-never-logged (sentinel never appears in any log record),
- session required (401 without auth).
"""

from __future__ import annotations

import json
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from homelab_monitor.kernel.ha.errors import HaError

_HTTP_OK = 200
_HTTP_UNAUTH = 401
_HTTP_BAD_GATEWAY = 502

_URL = "/api/integrations/home-assistant/notifications"


class _FakeWs:
    """HA WS client double: a fixed send_command result (dict | list | HaError)."""

    def __init__(self, result: object) -> None:
        self._result = result

    async def send_command(self, type_: str, **fields: object) -> object:
        del type_, fields
        return self._result


def _notification(
    nid: str,
    *,
    title: str | None = "T",
    message: str = "M",
    created_at: str | None = "2026-01-01T00:00:00+00:00",
) -> dict[str, object]:
    row: dict[str, object] = {"notification_id": nid, "message": message}
    if title is not None:
        row["title"] = title
    if created_at is not None:
        row["created_at"] = created_at
    return row


def _app_of(client: AsyncClient) -> FastAPI:
    return cast(FastAPI, client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]


def _set_ws(client: AsyncClient, result: object) -> None:
    _app_of(client).state.ha_ws_client = _FakeWs(result)


@pytest.mark.asyncio
async def test_notifications_happy_path_dict_wrapped(
    authenticated_client: AsyncClient,
) -> None:
    """{"notifications": [...]} shape -> 200 with rows; total==returned==len."""
    _set_ws(
        authenticated_client,
        {"notifications": [_notification("n1"), _notification("n2")]},
    )
    resp = await authenticated_client.get(_URL)
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004 -- planted count
    assert body["returned"] == 2  # noqa: PLR2004 -- planted count
    assert len(body["rows"]) == 2  # noqa: PLR2004 -- planted count
    first = body["rows"][0]
    assert first["notification_id"] == "n1"
    assert first["title"] == "T"
    assert first["message"] == "M"
    assert first["created_at"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_notifications_happy_path_bare_list(
    authenticated_client: AsyncClient,
) -> None:
    """Bare list shape -> 200 with rows."""
    _set_ws(authenticated_client, [_notification("only")])
    resp = await authenticated_client.get(_URL)
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 1
    assert body["returned"] == 1
    assert body["rows"][0]["notification_id"] == "only"


@pytest.mark.asyncio
async def test_notifications_empty_returns_200_empty_rows(
    authenticated_client: AsyncClient,
) -> None:
    """Degenerate empty dict -> 200 rows=[], total=0, returned=0."""
    _set_ws(authenticated_client, {})
    resp = await authenticated_client.get(_URL)
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["returned"] == 0


@pytest.mark.asyncio
async def test_notifications_ha_error_unreachable_maps_to_502(
    authenticated_client: AsyncClient,
) -> None:
    """HaError(unreachable) -> 502 upstream_unavailable."""
    _set_ws(authenticated_client, HaError(reason="unreachable", message="boom"))
    resp = await authenticated_client.get(_URL)
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_notifications_ha_error_auth_maps_to_502_not_401(
    authenticated_client: AsyncClient,
) -> None:
    """HaError(auth) -> 502 (a bad HA token is a server config problem, NOT 401)."""
    _set_ws(
        authenticated_client,
        HaError(reason="auth", message="bad token", status=401),
    )
    resp = await authenticated_client.get(_URL)
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert resp.status_code != _HTTP_UNAUTH
    assert resp.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_notifications_skips_malformed_entries(
    authenticated_client: AsyncClient,
) -> None:
    """Non-dict element and a dict missing notification_id are skipped, not fatal."""
    _set_ws(
        authenticated_client,
        {
            "notifications": [
                "not-a-dict",
                {"message": "no id here"},
                {"notification_id": "", "message": "empty id"},
                _notification("good"),
            ]
        },
    )
    resp = await authenticated_client.get(_URL)
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["total"] == 1
    assert body["rows"][0]["notification_id"] == "good"


@pytest.mark.asyncio
async def test_notifications_coerces_missing_message_and_optional_fields(
    authenticated_client: AsyncClient,
) -> None:
    """Missing message -> "", missing title/created_at -> null."""
    _set_ws(
        authenticated_client,
        {"notifications": [{"notification_id": "bare"}]},
    )
    resp = await authenticated_client.get(_URL)
    assert resp.status_code == _HTTP_OK
    row = resp.json()["rows"][0]
    assert row["notification_id"] == "bare"
    assert row["message"] == ""
    assert row["title"] is None
    assert row["created_at"] is None


@pytest.mark.asyncio
async def test_notifications_requires_session(
    authenticated_client: AsyncClient,
) -> None:
    """No session -> 401."""
    app = _app_of(authenticated_client)
    app.state.ha_ws_client = _FakeWs({"notifications": [_notification("x")]})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(_URL)
    assert resp.status_code == _HTTP_UNAUTH


@pytest.mark.asyncio
async def test_notifications_bodies_never_logged(
    authenticated_client: AsyncClient,
) -> None:
    """Sentinels appear in the RESPONSE but in NO captured log record."""
    sentinel_msg = "SENSITIVE-BODY-SENTINEL-9f3a"
    sentinel_title = "SENTINEL-TITLE-9f3a"
    _set_ws(
        authenticated_client,
        {"notifications": [_notification("s1", title=sentinel_title, message=sentinel_msg)]},
    )
    with capture_logs() as captured:
        resp = await authenticated_client.get(_URL)
    assert resp.status_code == _HTTP_OK
    body_text = resp.text
    # Present in the response body delivered to the authenticated session.
    assert sentinel_msg in body_text
    assert sentinel_title in body_text
    # Absent from EVERY captured log event.
    for event in captured:
        serialized = json.dumps(event)
        assert sentinel_msg not in serialized
        assert sentinel_title not in serialized


@pytest.mark.asyncio
async def test_notifications_502_body_never_echoes_content(
    authenticated_client: AsyncClient,
) -> None:
    """The HaError->502 path returns no notification content (HaError carries none)."""
    sentinel = "SHOULD-NOT-LEAK-9f3a"
    # HaError.message is internal; ensure it is NOT echoed in the 502 body.
    _set_ws(
        authenticated_client,
        HaError(reason="bad_response", message=sentinel),
    )
    resp = await authenticated_client.get(_URL)
    assert resp.status_code == _HTTP_BAD_GATEWAY
    assert sentinel not in resp.text
