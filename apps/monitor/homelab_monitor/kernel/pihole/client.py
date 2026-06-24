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
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal, cast

import httpx

from homelab_monitor.kernel.pihole.errors import PiholeError

_HTTP_UNAUTHORIZED: Final[int] = 401
_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
_HTTP_OK_FLOOR: Final[int] = 200
_HTTP_OK_CEIL: Final[int] = 300  # exclusive upper bound for the 2xx success band

# Pi-hole returns 200 with an {"error": {"key": ..., "message": ...}} envelope for
# some auth/validation failures instead of an HTTP status. We classify those
# envelopes so an in-body "unauthorized" key shares the SAME re-auth-retry guard as
# a real HTTP 401 (Cross-stage deliverable 2). The literal key Pi-hole uses for an
# expired/invalid session is "unauthorized".
_PIHOLE_UNAUTHORIZED_KEY: Final[str] = "unauthorized"

_GRAVITY_TIMEOUT_SECONDS: Final[float] = 120.0  # gravity rebuilds can run a minute+
_GRAVITY_LOG_TAIL: Final[int] = 20  # last N non-empty lines retained for the audit/UI


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

    async def config(self) -> PiholeResponse | PiholeError:
        """GET /api/config — full Pi-hole config (consumed by pihole_config collector)."""
        return await self._get("/api/config", "config")

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

    async def set_blocking(
        self, *, blocking: bool, timer: int | None
    ) -> PiholeResponse | PiholeError:
        """POST /api/dns/blocking — set blocking on/off (RW). Body {"blocking", "timer"}.

        Returns a PiholeResponse whose payload is Pi-hole's echoed new state (it
        returns the same shape as GET /api/dns/blocking: {"blocking": "<state>",
        "timer": <float|null>, "took": ...}).
        """
        resp = await self._request(
            "POST", "/api/dns/blocking", json_body={"blocking": blocking, "timer": timer}
        )
        if isinstance(resp, PiholeError):
            return resp
        try:
            payload: object = resp.json()
        except ValueError:
            return PiholeError(
                reason="bad_response", message="POST /api/dns/blocking: response is not JSON"
            )
        took = _extract_took(payload)
        return PiholeResponse(payload=payload, took_seconds=took, endpoint="dns/blocking")

    async def gravity_update(self) -> PiholeResponse | PiholeError:  # noqa: PLR0911 -- return-not-raise: each branch is a distinct auth/stream/status error path
        """POST /api/action/gravity — rebuild gravity (RW, STREAMING text/plain).

        Drains the chunked text stream to completion, then tail-parses the last
        lines for a success/failure marker. Uses a generous timeout (gravity can run
        a minute+). Returns PiholeResponse(payload={"success": bool, "log_tail":
        [last N non-empty lines]}, took_seconds=<wall time>, endpoint="action/gravity").

        SUCCESS HEURISTIC (defensive — tune in Refinement 3b once live markers are
        confirmed): a stream that completes WITHOUT an explicit failure/error marker
        in its tail is treated as SUCCESS. The exact Pi-hole marker lines are not yet
        confirmed, so this is a single well-commented function the Refinement pass can
        adjust. A 401 on stream-open triggers ONE re-auth + re-open (mirrors _request).
        """
        if self._sid is None:
            login_err = await self._ensure_session(None)
            if login_err is not None:
                return login_err
        result = await self._gravity_stream_once()
        if isinstance(result, PiholeError):
            return result
        status, lines, elapsed = result
        if status == _HTTP_UNAUTHORIZED:
            reauth_err = await self._ensure_session(self._sid)
            if reauth_err is not None:
                return reauth_err
            result = await self._gravity_stream_once()
            if isinstance(result, PiholeError):
                return result
            status, lines, elapsed = result
            if status == _HTTP_UNAUTHORIZED:
                return PiholeError(
                    reason="auth",
                    message="POST /api/action/gravity: unauthorized after re-auth",
                    status=_HTTP_UNAUTHORIZED,
                )
        if status == _HTTP_TOO_MANY_REQUESTS:
            return PiholeError(
                reason="rate_limited", message="POST /api/action/gravity: HTTP 429", status=status
            )
        if not (_HTTP_OK_FLOOR <= status < _HTTP_OK_CEIL):
            return PiholeError(
                reason="http_error",
                message=f"POST /api/action/gravity: HTTP {status}",
                status=status,
            )
        tail = lines[-_GRAVITY_LOG_TAIL:]
        success = _gravity_succeeded(tail)
        return PiholeResponse(
            payload={"success": success, "log_tail": tail},
            took_seconds=elapsed,
            endpoint="action/gravity",
        )

    async def _gravity_stream_once(
        self,
    ) -> tuple[int, list[str], float] | PiholeError:
        """Open the gravity stream once, drain it, return (status, non_empty_lines, elapsed).

        Returns a transport PiholeError on connect/timeout. Does NOT classify status
        (caller maps 401/429/5xx). Lines are stripped; empty lines are dropped.
        """
        url = f"{self._base_url}/api/action/gravity"
        headers = {"X-FTL-SID": self._sid} if self._sid else {}
        start = time.monotonic()
        lines: list[str] = []
        try:
            async with self._http.stream(
                "POST", url, headers=headers, timeout=_GRAVITY_TIMEOUT_SECONDS
            ) as resp:
                status = resp.status_code
                async for chunk in resp.aiter_lines():
                    stripped = chunk.strip()
                    if stripped:
                        lines.append(stripped)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return PiholeError(
                reason="unreachable", message="POST /api/action/gravity: connection failed"
            )
        except (httpx.ReadTimeout, httpx.TimeoutException):
            return PiholeError(reason="timeout", message="POST /api/action/gravity: timed out")
        return (status, lines, time.monotonic() - start)

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

    async def _get(
        self, path: str, endpoint: str, params: dict[str, str] | None = None
    ) -> PiholeResponse | PiholeError:
        """Perform an authenticated GET, mapping every failure to a PiholeError.

        Uses ``_classify_response`` so an HTTP 401 AND a 200-response carrying an
        ``{"error": {"key": "unauthorized"}}`` envelope share the SAME single
        re-auth-retry guard (Cross-stage deliverable 2). After one re-auth, a still-
        unauthorized result (either signal) becomes ``PiholeError(reason="auth")``.
        """
        resp = await self._request("GET", path, params=params)
        if isinstance(resp, PiholeError):
            return resp
        # resp is a verified-ok httpx.Response (2xx, no error envelope).
        try:
            payload: object = resp.json()
        except ValueError:
            return PiholeError(reason="bad_response", message=f"GET {path}: response is not JSON")
        took = _extract_took(payload)
        return PiholeResponse(payload=payload, took_seconds=took, endpoint=endpoint)

    async def _request(  # noqa: PLR0911 -- return-not-raise: one branch per transport/auth/classify outcome on each of two attempts
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: object | None = None,
    ) -> httpx.Response | PiholeError:
        """Authenticated request with single re-auth-retry; returns a verified-ok Response.

        Establishes a session, issues the request, classifies the response. On
        ``needs_reauth`` (HTTP 401 OR 200 ``unauthorized`` envelope) it re-auths ONCE
        and retries; a second ``needs_reauth`` becomes ``PiholeError(reason="auth")``.
        On ``ok`` it returns the raw ``httpx.Response`` for the caller to parse
        (the caller owns body parsing: JSON for reads, stream-drain for gravity).
        """
        if self._sid is None:
            login_err = await self._ensure_session(None)
            if login_err is not None:
                return login_err
        resp = await self._do_request(method, path, params=params, json_body=json_body)
        if isinstance(resp, PiholeError):
            return resp
        outcome, err = _classify_response(resp, f"{method} {path}")
        if outcome == "error":
            assert err is not None
            return err
        if outcome == "ok":
            return resp
        # outcome == "needs_reauth": re-auth ONCE and retry.
        reauth_err = await self._ensure_session(self._sid)
        if reauth_err is not None:
            return reauth_err
        resp = await self._do_request(method, path, params=params, json_body=json_body)
        if isinstance(resp, PiholeError):
            return resp
        outcome, err = _classify_response(resp, f"{method} {path}")
        if outcome == "error":
            assert err is not None
            return err
        if outcome == "needs_reauth":
            return PiholeError(
                reason="auth",
                message=f"{method} {path}: unauthorized after re-auth",
                status=_HTTP_UNAUTHORIZED,
            )
        return resp

    async def _do_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: object | None = None,
    ) -> httpx.Response | PiholeError:
        """Issue one request with the current SID header; map transport errors to PiholeError.

        Returns the raw ``httpx.Response`` (any status) or a transport PiholeError.
        Status / envelope classification happens in ``_request`` / ``_get``.
        """
        url = f"{self._base_url}{path}"
        headers = {"X-FTL-SID": self._sid} if self._sid else {}
        try:
            return await self._http.request(
                method, url, headers=headers, params=params, json=json_body
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return PiholeError(reason="unreachable", message=f"{method} {path}: connection failed")
        except (httpx.ReadTimeout, httpx.TimeoutException):
            return PiholeError(reason="timeout", message=f"{method} {path}: timed out")


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


def _classify_response(  # noqa: PLR0911 -- classifier: each branch maps a distinct HTTP/body-envelope outcome to a typed tuple
    resp: httpx.Response, path: str
) -> tuple[Literal["ok", "needs_reauth", "error"], PiholeError | None]:
    """Classify a raw Pi-hole response into ok / needs_reauth / error.

    Unifies HTTP-status and in-body error-envelope handling so a 200-response
    carrying ``{"error": {"key": "unauthorized", ...}}`` is treated IDENTICALLY to
    an HTTP 401 (both -> needs_reauth, one re-auth + retry). Any other error key
    becomes a ``bad_response`` PiholeError.

    Returns ``(outcome, err)``:
      - ("ok", None)            -> 2xx status with NO error envelope; caller parses.
      - ("needs_reauth", None)  -> HTTP 401, OR 200 with error key "unauthorized".
      - ("error", PiholeError)  -> any mapped failure (rate_limited / http_error /
                                   bad_response / 200-with-other-error-key).

    The 401-after-reauth -> auth decision stays in ``_get`` / the write helpers
    (this function does not know whether it is the first or the retried attempt).
    """
    status = resp.status_code
    if status == _HTTP_UNAUTHORIZED:
        return ("needs_reauth", None)
    if status == _HTTP_TOO_MANY_REQUESTS:
        retry_after = resp.headers.get("Retry-After")
        suffix = f" (Retry-After: {retry_after})" if retry_after else ""
        return (
            "error",
            PiholeError(reason="rate_limited", message=f"{path}: HTTP 429{suffix}", status=status),
        )
    if not (_HTTP_OK_FLOOR <= status < _HTTP_OK_CEIL):
        return (
            "error",
            PiholeError(reason="http_error", message=f"{path}: HTTP {status}", status=status),
        )
    # 2xx: inspect for an in-body error envelope. A parse failure here is NOT fatal
    # (some endpoints stream text, not JSON); only an explicit {"error": {...}}
    # object is classified. JSON-parse failures are left to the caller's own parse.
    try:
        body: object = resp.json()
    except ValueError:
        return ("ok", None)
    if isinstance(body, dict):
        error_obj = cast("dict[str, object]", body).get("error")
        if isinstance(error_obj, dict):
            err = cast("dict[str, object]", error_obj)
            key_obj = err.get("key")
            key = key_obj if isinstance(key_obj, str) else ""
            msg_obj = err.get("message")
            msg = msg_obj if isinstance(msg_obj, str) else ""
            if key == _PIHOLE_UNAUTHORIZED_KEY:
                return ("needs_reauth", None)
            return (
                "error",
                PiholeError(
                    reason="bad_response",
                    message=f"{path}: error key={key or '?'} message={msg}",
                    status=status,
                ),
            )
    return ("ok", None)


def _gravity_succeeded(tail: list[str]) -> bool:
    """Heuristic success check on the tail of the gravity stream.

    POSITIVE check (Refinement 3b confirmed the live `pihole -g` output ends with a
    "[✓] Done" completion marker): a tail containing that marker is a success. This
    avoids false-negatives from benign lines that merely contain "error" (e.g.
    "0 errors", an adlist URL with "error" in it). If no success marker is present,
    fall back to the failure-marker scan: an explicit failure marker → failed; an
    otherwise-clean completed stream → success (defensive). Matched case-insensitively.
    """
    success_marker = "[✓] done"
    failure_markers = ("error", "failed", "failure", "fatal", "abort")
    for line in tail:
        if success_marker in line.lower():
            return True
    for line in tail:
        lower = line.lower()
        if any(marker in lower for marker in failure_markers):
            return False
    return True
