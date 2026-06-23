"""Pi-hole v6 REST client (STAGE-006-001).

Kernel infrastructure mirroring :mod:`homelab_monitor.kernel.unifi.client` and
:mod:`homelab_monitor.kernel.ha.client`. Pi-hole v6 uses SESSION auth, not a
static header: ``POST /api/auth`` with the app password returns a session id
(SID); every subsequent request carries it in the ``X-FTL-SID`` header. One
session is reused across requests; on a 401 the client re-authenticates ONCE and
retries the request.

Construction (lifespan, once at startup)::

    PiholeRestClient(
        base_url=pihole_config.base_url,                  # http://192.168.2.148:8080 (host LAN IP)
        http=http_client,                                 # reuse the shared pool (plain HTTP)
        password_provider=lambda: ttl_resolver.current().get("pihole_api_password_ro"),
    )

The client reuses the SHARED ``httpx.AsyncClient`` (Pi-hole is plain HTTP on the
LAN — no TLS, no dedicated verify=False client needed, unlike Unifi).

The ``password_provider`` is invoked only inside ``_login`` (NOT stored) so a
rotated / post-startup-added password is picked up without a restart, and the
password value is never held in a client field.

READ-mostly: the typed GET helpers below establish the full surface for Wave-B/C
collectors; ``aclose`` issues a best-effort logout (DELETE /api/auth).

SECURITY: the app password NEVER appears in any returned ``PiholeError.message``
nor in any log line. The login body's ``password`` and the ``X-FTL-SID`` header /
SID value are never logged. Error messages are built from method + path + status
(plus the Pi-hole-provided ``session.message`` for auth failures, which never
contains the password).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

import httpx

from homelab_monitor.kernel.pihole.errors import PiholeError

_HTTP_UNAUTHORIZED: Final[int] = 401
_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
_HTTP_OK_FLOOR: Final[int] = 200
_HTTP_OK_CEIL: Final[int] = 300  # exclusive upper bound for the 2xx success band


@dataclass(frozen=True, slots=True)
class PiholeSession:
    """Parsed ``session`` object from Pi-hole ``POST /api/auth``.

    Lenient on missing fields. ``sid`` is None when login was rejected. ``message``
    is the Pi-hole-provided human reason (e.g. ``"password incorrect"``) — it never
    contains the password, so it is safe to surface in a PiholeError.
    """

    valid: bool
    totp: bool
    sid: str | None
    validity: int
    message: str | None
    csrf: str | None = None


@dataclass(frozen=True, slots=True)
class PiholeResponse:
    """A successful Pi-hole GET result.

    ``payload`` is the parsed JSON body (an object or array). Its static type is
    ``object`` — the no-``Any`` escape hatch — so callers narrow per-key with
    ``isinstance`` guards (collectors do this in Wave B/C). ``took_seconds`` is the
    Pi-hole-reported ``took`` field (FTL's own measured query time, in seconds),
    read by the API-latency collector (Wave B). ``endpoint`` is a stable label
    (e.g. ``"info/version"``) used for the latency metric.
    """

    payload: object
    took_seconds: float
    endpoint: str


class PiholeRestClient:
    """Pi-hole v6 REST client implementing the :class:`PiholeClient` Protocol."""

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        password_provider: Callable[[], str | None],
    ) -> None:
        """Initialize the client.

        Args:
            base_url: Pi-hole base URL (e.g. ``http://192.168.2.148:8080``). Trailing
                slashes are assumed already stripped by the config loader.
            http: the SHARED ``httpx.AsyncClient`` (its configured timeout is reused).
            password_provider: zero-arg callable returning the current app password,
                or ``None`` when no password is configured. Called only inside
                ``_login``; never stored.
        """
        self._base_url: str = base_url.rstrip("/")
        self._http: httpx.AsyncClient = http
        self._password_provider: Callable[[], str | None] = password_provider
        # Reused session id. None until the first successful login.
        self._sid: str | None = None
        # Serializes login so concurrent first-callers don't stampede /api/auth.
        self._auth_lock: asyncio.Lock = asyncio.Lock()

    # ---- public API (PiholeClient Protocol) ----

    async def info_version(self) -> PiholeResponse | PiholeError:
        """GET /api/info/version — Pi-hole / FTL version info (EXERCISED THIS STAGE)."""
        return await self._get("/api/info/version", "info/version")

    async def info_ftl(self) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        return await self._get("/api/info/ftl", "info/ftl")

    async def info_database(self) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        return await self._get("/api/info/database", "info/database")

    async def info_messages(self) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        return await self._get("/api/info/messages", "info/messages")

    async def info_system(self) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        return await self._get("/api/info/system", "info/system")

    async def stats_summary(self) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        return await self._get("/api/stats/summary", "stats/summary")

    async def stats_upstreams(self) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        return await self._get("/api/stats/upstreams", "stats/upstreams")

    async def stats_query_types(self) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        return await self._get("/api/stats/query_types", "stats/query_types")

    async def stats_top_clients(
        self, *, blocked: bool = False, count: int | None = None
    ) -> PiholeResponse | PiholeError:
        params: dict[str, str] = {}
        if blocked:
            params["blocked"] = "true"
        if count is not None:
            params["count"] = str(count)
        endpoint = "stats/top_clients_blocked" if blocked else "stats/top_clients"
        return await self._get("/api/stats/top_clients", endpoint, params=params or None)

    async def stats_top_domains(
        self, *, blocked: bool = False, count: int | None = None
    ) -> PiholeResponse | PiholeError:
        params: dict[str, str] = {}
        if blocked:
            params["blocked"] = "true"
        if count is not None:
            params["count"] = str(count)
        endpoint = "stats/top_domains_blocked" if blocked else "stats/top_domains"
        return await self._get("/api/stats/top_domains", endpoint, params=params or None)

    async def stats_recent_blocked(self) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        return await self._get("/api/stats/recent_blocked", "stats/recent_blocked")

    async def dns_blocking(self) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        return await self._get("/api/dns/blocking", "dns/blocking")

    async def lists(self) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        return await self._get("/api/lists", "lists")

    async def network_devices(self) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        # path: best-effort, confirm against v6 docs in consuming stage
        return await self._get("/api/network/devices", "network/devices")

    async def queries(self, params: dict[str, str]) -> PiholeResponse | PiholeError:
        # SCAFFOLDING: consumed in Wave B/C (STAGE-006-005..015)
        # path: best-effort, confirm against v6 docs in consuming stage
        return await self._get("/api/queries", "queries", params=params)

    async def aclose(self) -> None:
        """Best-effort logout: DELETE /api/auth with the current SID, suppress all errors.

        A dead Pi-hole at shutdown must NEVER block teardown, so EVERY exception is
        swallowed. No-op when there is no active session. Clears the SID afterward.
        """
        if not self._sid:
            return
        sid = self._sid
        try:
            await self._http.request(
                "DELETE",
                f"{self._base_url}/api/auth",
                headers={"X-FTL-SID": sid},
            )
        except Exception:  # shutdown must not raise; password not in scope here
            pass
        finally:
            self._sid = None

    # ---- internals ----

    async def _login(self) -> PiholeError | None:  # noqa: PLR0911 -- return-not-raise: each branch is a distinct auth-failure / response-shape case
        """POST /api/auth with the app password. On success store the SID; else PiholeError.

        Returns ``None`` on success (``self._sid`` set), or a PiholeError. The
        password is read here (never stored) and never placed in any error message.
        """
        password = self._password_provider()
        if password is None:
            return PiholeError(reason="auth", message="no pihole password configured")
        url = f"{self._base_url}/api/auth"
        try:
            resp = await self._http.request("POST", url, json={"password": password})
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return PiholeError(reason="unreachable", message="POST /api/auth: connection failed")
        except (httpx.ReadTimeout, httpx.TimeoutException):
            return PiholeError(reason="timeout", message="POST /api/auth: timed out")
        status = resp.status_code
        if status == _HTTP_TOO_MANY_REQUESTS:
            return PiholeError(
                reason="rate_limited", message="POST /api/auth: HTTP 429", status=status
            )
        if not (_HTTP_OK_FLOOR <= status < _HTTP_OK_CEIL):
            return PiholeError(
                reason="auth", message=f"POST /api/auth: HTTP {status}", status=status
            )
        try:
            body: object = resp.json()
        except ValueError:
            return PiholeError(
                reason="bad_response", message="POST /api/auth: response is not JSON"
            )
        if not isinstance(body, dict):
            return PiholeError(
                reason="bad_response", message="POST /api/auth: body is not an object"
            )
        session = _parse_session(cast("dict[str, object]", body))
        if not session.valid or not session.sid:
            reason = session.message if session.message else "login rejected"
            return PiholeError(reason="auth", message=f"POST /api/auth: {reason}")
        self._sid = session.sid
        return None

    async def _ensure_session(self, failed_sid: str | None) -> PiholeError | None:
        """Single-flight (re)login. Returns None when a usable SID exists, else PiholeError.

        Acquires the auth lock. If another caller already rotated the SID away from
        ``failed_sid`` while we waited, return immediately (the current SID is fresh —
        no re-login). Otherwise perform a login.
        """
        async with self._auth_lock:
            if self._sid is not None and self._sid != failed_sid:
                return None
            return await self._login()

    async def _get(  # noqa: PLR0911 -- return-not-raise: one branch per httpx error + HTTP status (timeout/unreachable/401/429/5xx/bad-json)
        self, path: str, endpoint: str, params: dict[str, str] | None = None
    ) -> PiholeResponse | PiholeError:
        """Perform an authenticated GET, mapping every failure to a PiholeError.

        Centralizes ALL branching: session establishment, X-FTL-SID attachment,
        transport-error mapping, status mapping (401 -> single re-auth+retry, 429,
        5xx/other), JSON parse, and ``took`` extraction. Keeping the branches here
        keeps the public helpers branchless and the coverage surface in one place.
        """
        if self._sid is None:
            login_err = await self._ensure_session(None)
            if login_err is not None:
                return login_err
        resp = await self._do_get(path, params)
        if isinstance(resp, PiholeError):
            return resp
        if resp.status_code == _HTTP_UNAUTHORIZED:
            reauth_err = await self._ensure_session(self._sid)
            if reauth_err is not None:
                return reauth_err
            resp = await self._do_get(path, params)
            if isinstance(resp, PiholeError):
                return resp
            if resp.status_code == _HTTP_UNAUTHORIZED:
                return PiholeError(
                    reason="auth",
                    message=f"GET {path}: HTTP 401 after re-auth",
                    status=_HTTP_UNAUTHORIZED,
                )
        status = resp.status_code
        if status == _HTTP_TOO_MANY_REQUESTS:
            retry_after = resp.headers.get("Retry-After")
            suffix = f" (Retry-After: {retry_after})" if retry_after else ""
            return PiholeError(
                reason="rate_limited", message=f"GET {path}: HTTP 429{suffix}", status=status
            )
        if not (_HTTP_OK_FLOOR <= status < _HTTP_OK_CEIL):
            return PiholeError(
                reason="http_error", message=f"GET {path}: HTTP {status}", status=status
            )
        try:
            payload: object = resp.json()
        except ValueError:
            return PiholeError(reason="bad_response", message=f"GET {path}: response is not JSON")
        took = _extract_took(payload)
        return PiholeResponse(payload=payload, took_seconds=took, endpoint=endpoint)

    async def _do_get(
        self, path: str, params: dict[str, str] | None
    ) -> httpx.Response | PiholeError:
        """Issue one GET with the current SID header, mapping transport errors to PiholeError.

        Returns the raw ``httpx.Response`` (any status) or a transport PiholeError.
        Status mapping happens in ``_get``.
        """
        url = f"{self._base_url}{path}"
        headers = {"X-FTL-SID": self._sid} if self._sid else {}
        try:
            return await self._http.request("GET", url, headers=headers, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return PiholeError(reason="unreachable", message=f"GET {path}: connection failed")
        except (httpx.ReadTimeout, httpx.TimeoutException):
            return PiholeError(reason="timeout", message=f"GET {path}: timed out")


def _parse_session(body: dict[str, object]) -> PiholeSession:
    """Parse the ``session`` object out of a /api/auth body (lenient on missing fields)."""
    raw = body.get("session")
    session = cast("dict[str, object]", raw) if isinstance(raw, dict) else {}
    valid = session.get("valid")
    totp = session.get("totp")
    sid = session.get("sid")
    validity = session.get("validity")
    message = session.get("message")
    csrf = session.get("csrf")
    return PiholeSession(
        valid=valid if isinstance(valid, bool) else False,
        totp=totp if isinstance(totp, bool) else False,
        sid=sid if isinstance(sid, str) else None,
        validity=validity if isinstance(validity, int) else 0,
        message=message if isinstance(message, str) else None,
        csrf=csrf if isinstance(csrf, str) else None,
    )


def _extract_took(payload: object) -> float:
    """Read FTL's ``took`` field from a parsed payload; 0.0 when absent/wrong type."""
    if isinstance(payload, dict):
        took = cast("dict[str, object]", payload).get("took")
        if isinstance(took, (int, float)) and not isinstance(took, bool):
            return float(took)
    return 0.0
