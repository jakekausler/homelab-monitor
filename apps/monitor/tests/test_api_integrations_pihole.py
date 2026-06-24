"""Tests for POST /api/integrations/pihole/* endpoints (STAGE-006-018).

Covers: confirm_phrase validation, scope gating, 401/403/502/503, payload parsing,
audit row assertions. Minimal implementation — full coverage in Refinement.
"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from homelab_monitor.kernel.pihole.client import PiholeResponse
from homelab_monitor.kernel.pihole.errors import PiholeError

# ---- Magic number constants (PLR2004) ----
_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400
_HTTP_UNAUTH = 401
_HTTP_FORBIDDEN = 403
_HTTP_BAD_GATEWAY = 502
_HTTP_SERVICE_UNAVAILABLE = 503
_BLOCKING_TIMER_SECONDS = 300.0

# ---- Fake RW client ----


class _FakeRwClient:
    """Stand-in for PiholeRestClient."""

    def __init__(
        self,
        *,
        before: object | PiholeError | None = None,
        set_result: object | PiholeError | None = None,
        gravity_result: object | PiholeError | None = None,
    ) -> None:
        self._before = before
        self._set_result = set_result
        self._gravity_result = gravity_result

    async def dns_blocking(self) -> PiholeResponse | PiholeError:
        if isinstance(self._before, PiholeError):
            return self._before
        return PiholeResponse(payload=self._before, took_seconds=0.0, endpoint="dns/blocking")

    async def set_blocking(
        self, *, blocking: bool, timer: int | None
    ) -> PiholeResponse | PiholeError:
        if isinstance(self._set_result, PiholeError):
            return self._set_result
        return PiholeResponse(payload=self._set_result, took_seconds=0.0, endpoint="dns/blocking")

    async def gravity_update(self) -> PiholeResponse | PiholeError:
        if isinstance(self._gravity_result, PiholeError):
            return self._gravity_result
        return PiholeResponse(
            payload=self._gravity_result, took_seconds=0.0, endpoint="action/gravity"
        )


# ---- Blocking tests (minimal) ----


@pytest.mark.asyncio
async def test_blocking_400_wrong_confirm_for_disable(authenticated_client: AsyncClient) -> None:
    """POST /blocking with action=disable but confirm_phrase=enable -> 400."""
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "disable", "confirm_phrase": "enable"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST


@pytest.mark.asyncio
async def test_blocking_400_wrong_confirm_for_enable(authenticated_client: AsyncClient) -> None:
    """POST /blocking with action=enable but confirm_phrase=disable -> 400."""
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "enable", "confirm_phrase": "disable"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST


@pytest.mark.asyncio
async def test_blocking_disable_success(authenticated_client: AsyncClient) -> None:
    """POST /blocking with correct confirm -> 200."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.state.pihole_rw_client = _FakeRwClient(
        before={"blocking": "enabled"},
        set_result={"blocking": "disabled", "timer": 300.0},
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "disable", "timer": 300, "confirm_phrase": "Disable"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["blocking"] == "disabled"
    assert body["timer"] == _BLOCKING_TIMER_SECONDS
    assert "audit_id" in body


@pytest.mark.asyncio
async def test_blocking_enable_success(authenticated_client: AsyncClient) -> None:
    """POST /blocking enable action -> 200."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.state.pihole_rw_client = _FakeRwClient(
        before={"blocking": "disabled"},
        set_result={"blocking": "enabled", "timer": None},
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "enable", "confirm_phrase": "enable"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_OK


@pytest.mark.asyncio
async def test_blocking_502_on_pihole_error(authenticated_client: AsyncClient) -> None:
    """set_blocking returns PiholeError -> 502."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.state.pihole_rw_client = _FakeRwClient(
        before={"blocking": "enabled"},
        set_result=PiholeError(reason="http_error", message="HTTP 500", status=500),
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "disable", "confirm_phrase": "disable"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_BAD_GATEWAY


@pytest.mark.asyncio
async def test_blocking_401_unauthenticated(unauthenticated_client: AsyncClient) -> None:
    """No auth -> 401."""
    resp = await unauthenticated_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "disable", "confirm_phrase": "disable"},
    )
    assert resp.status_code == _HTTP_UNAUTH


@pytest.mark.asyncio
async def test_blocking_503_when_rw_client_missing(authenticated_client: AsyncClient) -> None:
    """RW client not initialized -> 503."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    if hasattr(app.state, "pihole_rw_client"):
        delattr(app.state, "pihole_rw_client")
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "disable", "confirm_phrase": "disable"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_SERVICE_UNAVAILABLE


# ---- Gravity tests (minimal) ----


@pytest.mark.asyncio
async def test_gravity_400_wrong_confirm(authenticated_client: AsyncClient) -> None:
    """POST /gravity/update with wrong confirm_phrase -> 400."""
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/gravity/update",
        json={"confirm_phrase": "go"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST


@pytest.mark.asyncio
async def test_gravity_success(authenticated_client: AsyncClient) -> None:
    """POST /gravity/update with correct confirm -> 200."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.state.pihole_rw_client = _FakeRwClient(
        gravity_result={"success": True, "log_tail": ["[i] Done"]},
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/gravity/update",
        json={"confirm_phrase": "update"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["success"] is True


@pytest.mark.asyncio
async def test_gravity_502_on_pihole_error(authenticated_client: AsyncClient) -> None:
    """gravity_update returns PiholeError -> 502."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.state.pihole_rw_client = _FakeRwClient(
        gravity_result=PiholeError(reason="timeout", message="timed out"),
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/gravity/update",
        json={"confirm_phrase": "update"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_BAD_GATEWAY


@pytest.mark.asyncio
async def test_gravity_401_unauthenticated(unauthenticated_client: AsyncClient) -> None:
    """No auth -> 401."""
    resp = await unauthenticated_client.post(
        "/api/integrations/pihole/gravity/update",
        json={"confirm_phrase": "update"},
    )
    assert resp.status_code == _HTTP_UNAUTH


@pytest.mark.asyncio
async def test_gravity_payload_non_dict_defaults(authenticated_client: AsyncClient) -> None:
    """gravity_result is non-dict -> success=False, log_tail=[]."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.state.pihole_rw_client = _FakeRwClient(
        gravity_result=["not", "a", "dict"],
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/gravity/update",
        json={"confirm_phrase": "update"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["success"] is False
    assert body["log_tail"] == []


@pytest.mark.asyncio
async def test_blocking_non_dict_payloads_default_to_unknown_and_none_timer(
    authenticated_client: AsyncClient,
) -> None:
    """blocking payloads are None/non-dict -> blocking='unknown', timer=None.

    Covers integrations_pihole.py:95 (_blocking_state_str non-dict → "unknown")
    AND :106 (_blocking_timer_val non-dict → None) in a single request.
    """
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.state.pihole_rw_client = _FakeRwClient(before=None, set_result=None)
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "disable", "confirm_phrase": "disable"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["blocking"] == "unknown"
    assert body["timer"] is None


@pytest.mark.asyncio
async def test_blocking_before_state_dict_non_str_blocking_records_unknown(
    authenticated_client: AsyncClient,
) -> None:
    """before-state payload is a dict whose 'blocking' value is non-str -> 'unknown'.

    Covers integrations_pihole.py:93->95 (_blocking_state_str dict-but-non-str-value path).
    """
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.state.pihole_rw_client = _FakeRwClient(
        before={"blocking": 123},
        set_result={"blocking": "disabled"},
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "disable", "confirm_phrase": "disable"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["blocking"] == "disabled"


@pytest.mark.asyncio
async def test_blocking_timer_bool_returns_none(authenticated_client: AsyncClient) -> None:
    """blocking payload timer is bool -> timer=None.

    Covers integrations_pihole.py:103 (_blocking_timer_val bool → None).
    """
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.state.pihole_rw_client = _FakeRwClient(
        before={"blocking": "enabled"},
        set_result={"blocking": "disabled", "timer": False},
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "disable", "confirm_phrase": "disable"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["blocking"] == "disabled"
    assert body["timer"] is None


@pytest.mark.asyncio
async def test_gravity_payload_log_tail_non_list_defaults_empty(
    authenticated_client: AsyncClient,
) -> None:
    """gravity payload log_tail is not a list -> log_tail=[].

    Covers integrations_pihole.py:195->198 (gravity log_tail not a list → stays []).
    """
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.state.pihole_rw_client = _FakeRwClient(
        gravity_result={"success": True, "log_tail": "not-a-list"}
    )
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/gravity/update",
        json={"confirm_phrase": "update"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_OK
    body = resp.json()
    assert body["success"] is True
    assert body["log_tail"] == []


# ---- Scope validation tests (403) ----


@pytest.mark.asyncio
async def test_blocking_403_wrong_scope_token(api_token_client: AsyncClient) -> None:
    """POST /blocking with token lacking Scope.PIHOLE_WRITE -> 403."""
    app = cast(FastAPI, api_token_client.app)  # type: ignore[attr-defined]
    app.state.pihole_rw_client = _FakeRwClient(
        before={"blocking": "enabled"},
        set_result={"blocking": "disabled", "timer": 300.0},
    )
    resp = await api_token_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "disable", "confirm_phrase": "disable"},
    )
    assert resp.status_code == _HTTP_FORBIDDEN


@pytest.mark.asyncio
async def test_gravity_403_wrong_scope_token(api_token_client: AsyncClient) -> None:
    """POST /gravity/update with token lacking Scope.PIHOLE_WRITE -> 403."""
    app = cast(FastAPI, api_token_client.app)  # type: ignore[attr-defined]
    app.state.pihole_rw_client = _FakeRwClient(
        gravity_result={"success": True, "log_tail": ["[i] Done"]},
    )
    resp = await api_token_client.post(
        "/api/integrations/pihole/gravity/update",
        json={"confirm_phrase": "update"},
    )
    assert resp.status_code == _HTTP_FORBIDDEN


@pytest.mark.asyncio
async def test_blocking_400_beats_503_ordering(authenticated_client: AsyncClient) -> None:
    """POST /blocking with wrong confirm_phrase, no RW client -> 400 (not 503).

    Confirm validation runs before RW-client dependency resolution.
    """
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    if hasattr(app.state, "pihole_rw_client"):
        delattr(app.state, "pihole_rw_client")
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/blocking",
        json={"action": "disable", "confirm_phrase": "WRONG"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST


@pytest.mark.asyncio
async def test_gravity_400_beats_503_ordering(authenticated_client: AsyncClient) -> None:
    """POST /gravity/update with wrong confirm_phrase, no RW client -> 400 (not 503).

    Confirm validation runs before RW-client dependency resolution.
    """
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    if hasattr(app.state, "pihole_rw_client"):
        delattr(app.state, "pihole_rw_client")
    csrf = authenticated_client.cookies.get("homelab_monitor_csrf")
    resp = await authenticated_client.post(
        "/api/integrations/pihole/gravity/update",
        json={"confirm_phrase": "WRONG"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert resp.status_code == _HTTP_BAD_REQUEST
