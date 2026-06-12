"""Home Assistant REST client (STAGE-005-001).

D-HA-REST-FIRST: REST covers config / states / error-log / service calls.
Websocket (STAGE-005-002) lands beside this as ``kernel/ha/websocket.py``.

Construction (lifespan, once at startup)::

    HomeAssistantRestClient(
        base_url=ha_config.base_url,
        http=shared_http_client,                       # reuse the shared pool
        token_provider=lambda: ttl_resolver.current().get("ha_token"),
    )

The client reuses the SHARED ``httpx.AsyncClient`` (no second connection pool).
The ``token_provider`` is invoked per request (NOT stored) so a rotated /
post-startup-added ``ha_token`` is picked up without a restart, and the token
value is never held in a client field.

SECURITY: the bearer token never appears in any returned ``HaError.message`` nor
in any log line. Error messages are built from method + URL + status only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

import httpx

from homelab_monitor.kernel.ha.errors import HaError

_HTTP_UNAUTHORIZED: Final[int] = 401
_HTTP_FORBIDDEN: Final[int] = 403
_HTTP_OK_FLOOR: Final[int] = 200
_HTTP_OK_CEIL: Final[int] = 300  # exclusive upper bound for the 2xx success band


@dataclass(frozen=True, slots=True)
class HaConfigResult:
    """Parsed subset of HA ``GET /api/config``.

    Only the fields early callers need (smoke test reads ``version``;
    ``time_zone`` is carried for later use). Unknown / missing fields default
    to empty string rather than failing — HA always returns these two for a
    healthy instance, and a missing field is treated as a soft empty value.
    """

    version: str
    time_zone: str


@dataclass(frozen=True, slots=True)
class HaState:
    """One entity state object from HA ``GET /api/states``.

    ``attributes`` is arbitrary HA JSON, so its value type is ``object`` (the
    no-``Any`` escape hatch; collectors narrow per-key as needed). Timestamps
    are HA-provided ISO strings carried verbatim.
    """

    entity_id: str
    state: str
    attributes: dict[str, object]
    last_changed: str
    last_updated: str


@dataclass(frozen=True, slots=True)
class HaErrorLogResult:
    """Plain-text body of HA ``GET /api/error_log``."""

    text: str


@dataclass(frozen=True, slots=True)
class HaServiceResult:
    """Result of HA ``POST /api/services/<domain>/<service>``.

    HA returns a JSON array of changed-state objects; we carry the count and
    the raw list of dicts (value type ``object`` for the same arbitrary-JSON
    reason as :class:`HaState.attributes`).
    """

    changed_states: list[dict[str, object]]


class HomeAssistantRestClient:
    """REST client implementing the :class:`HomeAssistantClient` Protocol."""

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        token_provider: Callable[[], str | None],
    ) -> None:
        """Initialize the client.

        Args:
            base_url: HA base URL (e.g. ``http://192.168.2.148:8123``). Trailing
                slashes are stripped so path concatenation is unambiguous.
            http: the SHARED ``httpx.AsyncClient`` (its configured timeout is reused).
            token_provider: zero-arg callable returning the current long-lived
                bearer token, or ``None`` when no token is configured. Called
                per request; never stored.
        """
        self._base_url: str = base_url.rstrip("/")
        self._http: httpx.AsyncClient = http
        self._token_provider: Callable[[], str | None] = token_provider

    # ---- public API (HomeAssistantClient Protocol) ----

    async def get_config(self) -> HaConfigResult | HaError:
        """GET /api/config -> parsed version + time_zone, or HaError."""
        result = await self._get_json("/api/config")
        if isinstance(result, HaError):
            return result
        if not isinstance(result, dict):
            return HaError(reason="bad_response", message="GET /api/config: body is not an object")
        body = cast("dict[str, object]", result)
        version = body.get("version")
        time_zone = body.get("time_zone")
        return HaConfigResult(
            version=version if isinstance(version, str) else "",
            time_zone=time_zone if isinstance(time_zone, str) else "",
        )

    async def get_states(self) -> list[HaState] | HaError:
        """GET /api/states -> list of HaState, or HaError."""
        result = await self._get_json("/api/states")
        if isinstance(result, HaError):
            return result
        if not isinstance(result, list):
            return HaError(reason="bad_response", message="GET /api/states: body is not a list")
        raw_list = cast("list[object]", result)
        states: list[HaState] = []
        for item in raw_list:
            if not isinstance(item, dict):
                return HaError(
                    reason="bad_response", message="GET /api/states: entry is not an object"
                )
            entry = cast("dict[str, object]", item)
            states.append(_parse_state(entry))
        return states

    async def get_error_log(self) -> HaErrorLogResult | HaError:
        """GET /api/error_log (text/plain) -> text, or HaError."""
        resp = await self._request("GET", "/api/error_log")
        if isinstance(resp, HaError):
            return resp
        status_err = _status_error(resp, "GET", "/api/error_log")
        if status_err is not None:
            return status_err
        return HaErrorLogResult(text=resp.text)

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> HaServiceResult | HaError:
        """POST /api/services/<domain>/<service> with JSON body ``data``."""
        path = f"/api/services/{domain}/{service}"
        resp = await self._request("POST", path, json_body=data if data is not None else {})
        if isinstance(resp, HaError):
            return resp
        status_err = _status_error(resp, "POST", path)
        if status_err is not None:
            return status_err
        try:
            parsed: object = resp.json()
        except ValueError:
            return HaError(reason="bad_response", message=f"POST {path}: response is not JSON")
        if not isinstance(parsed, list):
            # HA returns [] for services that change nothing; a non-list is unexpected.
            return HaError(reason="bad_response", message=f"POST {path}: body is not a list")
        raw_list = cast("list[object]", parsed)
        changed: list[dict[str, object]] = []
        for item in raw_list:
            if isinstance(item, dict):
                changed.append(cast("dict[str, object]", item))
        return HaServiceResult(changed_states=changed)

    async def fire_event(self, event_type: str, data: dict[str, str]) -> None | HaError:
        """POST /api/events/<event_type> to fire an event on HA's event bus.

        Used by the ha_event dispatch channel (STAGE-005-020) to push alert
        firing/resolved events back to Home Assistant for the operator's own
        automations. Returns ``None`` on success (HA replies 200 with a small
        ``{"message": ...}`` body we do not consume) or an HaError on transport
        failure / non-200.
        """
        path = f"/api/events/{event_type}"
        resp = await self._request("POST", path, json_body=cast("dict[str, object]", data))
        if isinstance(resp, HaError):
            return resp
        status_err = _status_error(resp, "POST", path)
        if status_err is not None:
            return status_err
        return None

    # ---- internals ----

    async def _get_json(self, path: str) -> object | HaError:
        """GET ``path``, validate 2xx, parse JSON. Returns parsed object or HaError."""
        resp = await self._request("GET", path)
        if isinstance(resp, HaError):
            return resp
        status_err = _status_error(resp, "GET", path)
        if status_err is not None:
            return status_err
        try:
            return resp.json()
        except ValueError:
            return HaError(reason="bad_response", message=f"GET {path}: response is not JSON")

    async def _request(
        self, method: str, path: str, json_body: dict[str, object] | None = None
    ) -> httpx.Response | HaError:
        """Perform an authenticated request, mapping transport errors to HaError.

        Returns the raw ``httpx.Response`` on a completed exchange (any status),
        or an HaError for: no token (auth, no network call), connect failure
        (unreachable), or timeout (timeout).
        """
        token = self._token_provider()
        if token is None:
            return HaError(reason="auth", message="no token configured")
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            return await self._http.request(method, url, headers=headers, json=json_body)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return HaError(reason="unreachable", message=f"{method} {path}: connection failed")
        except (httpx.ReadTimeout, httpx.TimeoutException):
            return HaError(reason="timeout", message=f"{method} {path}: timed out")


def _parse_state(entry: dict[str, object]) -> HaState:
    """Parse one /api/states entry into an HaState (lenient on missing fields)."""
    entity_id = entry.get("entity_id")
    state = entry.get("state")
    attributes = entry.get("attributes")
    last_changed = entry.get("last_changed")
    last_updated = entry.get("last_updated")
    return HaState(
        entity_id=entity_id if isinstance(entity_id, str) else "",
        state=state if isinstance(state, str) else "",
        attributes=cast("dict[str, object]", attributes) if isinstance(attributes, dict) else {},
        last_changed=last_changed if isinstance(last_changed, str) else "",
        last_updated=last_updated if isinstance(last_updated, str) else "",
    )


def _status_error(resp: httpx.Response, method: str, path: str) -> HaError | None:
    """Return an HaError for a non-2xx status, or None for 2xx."""
    status = resp.status_code
    if _HTTP_OK_FLOOR <= status < _HTTP_OK_CEIL:
        return None
    if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
        return HaError(reason="auth", message=f"{method} {path}: HTTP {status}", status=status)
    return HaError(reason="http_error", message=f"{method} {path}: HTTP {status}", status=status)
