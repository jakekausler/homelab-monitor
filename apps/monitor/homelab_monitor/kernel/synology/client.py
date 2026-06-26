"""Synology DSM v7 REST client (STAGE-008-001).

Kernel infrastructure mirroring :mod:`homelab_monitor.kernel.pihole.client`
(session single-flight) and :mod:`homelab_monitor.kernel.unifi.client`
(self-signed ``verify=False`` transport + monotonic latency). DSM v7 uses SESSION
auth: ``SYNO.API.Auth`` version 7 ``method=login`` (``account``/``passwd``/
``format=sid``, **NO ``session=`` param** — sending one returns DSM error 402)
returns a session id (SID); every subsequent ``entry.cgi`` call carries it as the
``_sid`` query param. One session is reused; on DSM body error **119** (session
expired) the client re-authenticates ONCE and retries.

DSM has ONE endpoint — ``{base_url}/webapi/entry.cgi`` — that varies only by the
``api`` / ``version`` / ``method`` query params, so a single private ``_get``
builds every request and the ~20 typed helpers are branchless one-line wrappers.

DSM returns HTTP 200 for LOGICAL errors: the failure is in the body as
``{"success": false, "error": {"code": N}}``. So the success/error decision is
made AFTER JSON parse (HTTP status only gates transport-level failures).

Construction (lifespan, once at startup)::

    SynologyRestClient(
        base_url=synology_config.base_url,               # https://192.168.2.4:5001
        http=synology_http_client,                        # dedicated verify=False client
        account=synology_config.account,                  # "homelab-monitor" (not a secret)
        password_provider=lambda: ttl_resolver.current().get("synology_dsm_password"),
    )

The client BORROWS a DEDICATED ``httpx.AsyncClient`` created with ``verify=False``
in lifespan (the DSM serves a self-signed cert ``CN=synology``). Lifespan owns that
client's construction + teardown; this client never closes it.

The ``password_provider`` is invoked only inside ``_login`` (NOT stored) so a
rotated / post-startup-added password is picked up without a restart, and the
password value is never held in a client field. The ``account`` is not a secret and
is stored.

READ / OBSERVE-ONLY: only GET helpers exist; there are no write methods.
``aclose`` issues a best-effort logout (``method=logout``).

SECURITY: the DSM password NEVER appears in any returned ``SynologyError.message``
nor in any log line; neither does the SID. Error messages are built from the api
name + method + HTTP status / DSM error code only — never the ``passwd`` query
value, the account, or the ``_sid``.

SCAFFOLDING: the typed GET helpers below establish the full surface for Wave-B/C/E
collectors; only ``system_info`` is exercised THIS stage.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

import httpx

from homelab_monitor.kernel.synology.errors import SynologyError

_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
_HTTP_OK_FLOOR: Final[int] = 200
_HTTP_OK_CEIL: Final[int] = 300  # exclusive upper bound for the 2xx success band

# DSM body error codes (carried in {"error": {"code": N}}; HTTP is 200).
_DSM_BAD_CREDENTIALS: Final[int] = 400  # bad account/password -> auth
_DSM_PERMISSION_DENIED: Final[int] = 105  # SID valid, account lacks rights -> api_error
_DSM_SESSION_EXPIRED: Final[int] = 119  # re-auth once + retry
_DSM_SESSION_PARAM_SENT: Final[int] = 402  # we never send session=; should never happen


@dataclass(frozen=True, slots=True)
class SynologySession:
    """Parsed ``data`` object from a DSM ``SYNO.API.Auth`` login.

    Lenient on missing fields. ``sid`` is None when login was rejected / the field
    was absent or the wrong type. ``synotoken`` is the CSRF header for WRITE calls;
    this integration is observe-only so it is parsed-but-ignored (kept for shape
    symmetry; never used).
    """

    sid: str | None
    synotoken: str | None = None


@dataclass(frozen=True, slots=True)
class SynologyResponse:
    """A successful Synology DSM GET result.

    ``payload`` is the DSM ``data`` object when the body carried a ``data`` key,
    otherwise the whole parsed body (some methods return ``{"success": true}`` with
    no ``data``). Its static type is ``object`` — the no-``Any`` escape hatch — so
    callers narrow per-key with ``isinstance`` guards (collectors do this in Wave
    B/C/E). ``took_seconds`` is the monotonic wall-clock duration of the underlying
    httpx call (DSM has no server-reported ``took`` field), read by the API-latency
    collector (Wave B) for ``homelab_synology_api_took_seconds{api}``. ``endpoint``
    is a stable label ``"{api}/{method}"`` (e.g. ``"SYNO.Core.System/info"``).
    """

    payload: object
    took_seconds: float
    endpoint: str


class SynologyRestClient:
    """Synology DSM v7 REST client implementing the :class:`SynologyClient` Protocol."""

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        account: str,
        password_provider: Callable[[], str | None],
    ) -> None:
        """Initialize the client.

        Args:
            base_url: DSM base URL (e.g. ``https://192.168.2.4:5001``). Trailing
                slashes are assumed already stripped by the config loader.
            http: the DEDICATED ``verify=False`` ``httpx.AsyncClient`` (borrowed;
                lifespan owns its lifecycle).
            account: the DSM service-account name (not a secret; from config).
            password_provider: zero-arg callable returning the current DSM password,
                or ``None`` when none is configured. Called only inside ``_login``;
                never stored.
        """
        self._base_url: str = base_url.rstrip("/")
        self._http: httpx.AsyncClient = http
        self._account: str = account
        self._password_provider: Callable[[], str | None] = password_provider
        # Reused session id. None until the first successful login.
        self._sid: str | None = None
        # Serializes login so concurrent first-callers don't stampede the auth API.
        self._auth_lock: asyncio.Lock = asyncio.Lock()

    # ---- public API (SynologyClient Protocol) ----

    async def system_info(self) -> SynologyResponse | SynologyError:
        # Recon (EPIC-008 "Verified deployment reality"): SYNO.Core.System v3
        # method=info -> model/serial/firmware/uptime/sys_temp. EXERCISED THIS STAGE.
        return await self._get("SYNO.Core.System", "3", "info")

    async def system_utilization(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-008
        return await self._get("SYNO.Core.System.Utilization", "1", "get")

    async def system_health(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-009
        return await self._get("SYNO.Core.System.SystemHealth", "1", "get")

    async def hardware_fanspeed(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-007
        return await self._get("SYNO.Core.Hardware.FanSpeed", "1", "get")

    async def need_reboot(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-007
        return await self._get("SYNO.Core.Hardware.NeedReboot", "1", "get")

    async def storage_load_info(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-005 / STAGE-008-006
        return await self._get("SYNO.Storage.CGI.Storage", "1", "load_info")

    async def ups_get(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-009
        return await self._get("SYNO.Core.ExternalDevice.UPS", "1", "get")

    async def upgrade_check(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-012
        return await self._get("SYNO.Core.Upgrade.Server", "4", "check")

    async def package_list(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-012
        return await self._get("SYNO.Core.Package", "1", "list")

    async def package_server_list(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-012
        return await self._get("SYNO.Core.Package.Server", "2", "list")

    async def backup_task_list(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-010
        return await self._get("SYNO.Backup.Task", "1", "list")

    async def backup_repository_list(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-010
        return await self._get("SYNO.Backup.Repository", "1", "list")

    async def share_snapshot_list(self, name: str) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-011
        return await self._get("SYNO.Core.Share.Snapshot", "2", "list", params={"name": name})

    async def share_list(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-011
        return await self._get("SYNO.Core.Share", "1", "list")

    async def replica_core_list(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-011
        return await self._get("SYNO.Btrfs.Replica.Core", "1", "list")

    async def security_scan_status(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-013
        return await self._get("SYNO.Core.SecurityScan.Status", "1", "system_get")

    async def current_connection_list(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-013
        return await self._get("SYNO.Core.CurrentConnection", "1", "list")

    async def ss_info(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-015
        return await self._get("SYNO.SurveillanceStation.Info", "1", "GetInfo")

    async def ss_camera_list(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-015
        return await self._get("SYNO.SurveillanceStation.Camera", "9", "List")

    async def ss_event_count_by_category(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-016
        return await self._get("SYNO.SurveillanceStation.Event", "5", "CountByCategory")

    async def ss_recording_list(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-016
        return await self._get("SYNO.SurveillanceStation.Recording", "6", "List")

    async def ss_license(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-017
        return await self._get("SYNO.SurveillanceStation.License", "1", "Load")

    async def ss_homemode(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-017
        return await self._get("SYNO.SurveillanceStation.HomeMode", "1", "GetInfo")

    async def ss_log_list(self) -> SynologyResponse | SynologyError:
        # SCAFFOLDING: consumed in STAGE-008-015 / STAGE-008-016
        return await self._get("SYNO.SurveillanceStation.Log", "3", "List")

    async def aclose(self) -> None:
        """Best-effort logout (``SYNO.API.Auth method=logout``); suppress all errors.

        A dead DSM at shutdown must NEVER block teardown, so EVERY exception is
        swallowed. No-op when there is no active session. Clears the SID afterward.
        The password is not in scope here; the logout carries only the SID.
        """
        if self._sid is None:
            return
        sid = self._sid
        url = f"{self._base_url}/webapi/entry.cgi"
        params = {
            "api": "SYNO.API.Auth",
            "version": "7",
            "method": "logout",
            "_sid": sid,
        }
        try:
            await self._http.request("GET", url, params=params)
        except Exception:  # shutdown must not raise; password not in scope here
            pass
        finally:
            self._sid = None

    # ---- internals ----

    async def _login(self) -> SynologyError | None:  # noqa: PLR0911 -- return-not-raise: each branch is a distinct auth-failure / response-shape case
        """Login via ``SYNO.API.Auth`` v7; on success store the SID, else SynologyError.

        Returns ``None`` on success (``self._sid`` set), or a SynologyError. The
        password is read here (never stored) and never placed in any error message.
        NO ``session=`` param is sent (DSM error 402 if present).
        """
        password = self._password_provider()
        if password is None:
            return SynologyError(reason="auth", message="no synology password configured")
        url = f"{self._base_url}/webapi/entry.cgi"
        params = {
            "api": "SYNO.API.Auth",
            "version": "7",
            "method": "login",
            "account": self._account,
            "passwd": password,
            "format": "sid",
        }
        try:
            resp = await self._http.request("GET", url, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return SynologyError(reason="unreachable", message="auth login: connection failed")
        except (httpx.ReadTimeout, httpx.TimeoutException):
            return SynologyError(reason="timeout", message="auth login: timed out")
        status = resp.status_code
        if status == _HTTP_TOO_MANY_REQUESTS:
            return SynologyError(
                reason="rate_limited", message="auth login: HTTP 429", status=status
            )
        if not (_HTTP_OK_FLOOR <= status < _HTTP_OK_CEIL):
            return SynologyError(reason="auth", message=f"auth login: HTTP {status}", status=status)
        try:
            body: object = resp.json()
        except ValueError:
            return SynologyError(reason="bad_response", message="auth login: response is not JSON")
        if not isinstance(body, dict):
            return SynologyError(reason="bad_response", message="auth login: body is not an object")
        body_dict = cast("dict[str, object]", body)
        if body_dict.get("success") is not True:
            return SynologyError(reason="auth", message="auth login: rejected")
        session = _parse_session(body_dict)
        if session.sid is None:
            return SynologyError(reason="auth", message="auth login: no sid in response")
        self._sid = session.sid
        return None

    async def _ensure_session(self, failed_sid: str | None) -> SynologyError | None:
        """Single-flight (re)login. Returns None when a usable SID exists, else SynologyError.

        Acquires the auth lock. If another caller already rotated the SID away from
        ``failed_sid`` while we waited, return immediately (the current SID is fresh —
        no re-login). Otherwise perform a login.
        """
        async with self._auth_lock:
            if self._sid is not None and self._sid != failed_sid:
                return None
            return await self._login()

    async def _get(  # noqa: PLR0911 -- return-not-raise: one branch per transport error + HTTP status + DSM error code
        self, api: str, version: str, method: str, params: dict[str, str] | None = None
    ) -> SynologyResponse | SynologyError:
        """Perform an authenticated DSM GET, mapping every failure to a SynologyError.

        Centralizes ALL branching: session establishment, ``_sid`` injection,
        transport-error mapping, HTTP-status mapping, JSON parse, and the DSM
        body-level ``success``/``error.code`` handling (119 -> single re-auth+retry;
        400 -> auth; 105/402/other -> api_error). Keeping the branches here keeps the
        public helpers branchless and the coverage surface in one place.
        """
        endpoint = f"{api}/{method}"
        if self._sid is None:
            login_err = await self._ensure_session(None)
            if login_err is not None:
                return login_err
        result = await self._do_get(api, version, method, endpoint, params)
        if isinstance(result, SynologyError):
            return result
        response, dsm_code = result
        if dsm_code == _DSM_SESSION_EXPIRED:
            reauth_err = await self._ensure_session(self._sid)
            if reauth_err is not None:
                return reauth_err
            retry = await self._do_get(api, version, method, endpoint, params)
            if isinstance(retry, SynologyError):
                return retry
            response, dsm_code = retry
            if dsm_code == _DSM_SESSION_EXPIRED:
                return SynologyError(
                    reason="auth",
                    message=f"{endpoint}: DSM error 119 after re-auth",
                    status=_DSM_SESSION_EXPIRED,
                )
        if dsm_code is not None:
            return _dsm_error(endpoint, dsm_code)
        return response

    async def _do_get(  # noqa: PLR0911 -- return-not-raise: transport + HTTP status + JSON-shape branches
        self,
        api: str,
        version: str,
        method: str,
        endpoint: str,
        params: dict[str, str] | None,
    ) -> tuple[SynologyResponse, int | None] | SynologyError:
        """Issue ONE DSM GET, time it, parse the body, return (response, dsm_code).

        Returns a ``SynologyError`` for transport / HTTP / parse failures. Otherwise
        returns ``(SynologyResponse, dsm_code)`` where ``dsm_code`` is the DSM
        ``error.code`` when the body had ``success:false`` (the caller decides
        re-auth vs api_error), or ``None`` for a clean ``success:true`` body. The
        monotonic timer brackets ONLY this single httpx call, so the 119-retry path
        reports the SECOND call's timing (re-auth round-trips are not timed).
        """
        url = f"{self._base_url}/webapi/entry.cgi"
        query: dict[str, str] = {"api": api, "version": version, "method": method}
        if self._sid is not None:
            query["_sid"] = self._sid
        if params is not None:
            query.update(params)
        start = time.monotonic()
        try:
            resp = await self._http.request("GET", url, params=query)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return SynologyError(reason="unreachable", message=f"{endpoint}: connection failed")
        except (httpx.ReadTimeout, httpx.TimeoutException):
            return SynologyError(reason="timeout", message=f"{endpoint}: timed out")
        took_seconds = time.monotonic() - start
        status = resp.status_code
        if status == _HTTP_TOO_MANY_REQUESTS:
            return SynologyError(
                reason="rate_limited", message=f"{endpoint}: HTTP 429", status=status
            )
        if not (_HTTP_OK_FLOOR <= status < _HTTP_OK_CEIL):
            return SynologyError(
                reason="http_error", message=f"{endpoint}: HTTP {status}", status=status
            )
        try:
            body: object = resp.json()
        except ValueError:
            return SynologyError(reason="bad_response", message=f"{endpoint}: response is not JSON")
        if not isinstance(body, dict):
            return SynologyError(
                reason="bad_response", message=f"{endpoint}: body is not an object"
            )
        body_dict = cast("dict[str, object]", body)
        success = body_dict.get("success")
        if success is True:
            data = body_dict.get("data")
            payload: object = data if "data" in body_dict else body_dict
            return (
                SynologyResponse(payload=payload, took_seconds=took_seconds, endpoint=endpoint),
                None,
            )
        if success is False:
            code = _extract_error_code(body_dict)
            # On success=false this response is discarded by _get;
            # the returned dsm_code drives the error.
            empty = SynologyResponse(
                payload=body_dict, took_seconds=took_seconds, endpoint=endpoint
            )
            return (empty, code)
        return SynologyError(
            reason="bad_response", message=f"{endpoint}: body missing 'success' boolean"
        )


def _parse_session(body: dict[str, object]) -> SynologySession:
    """Parse the ``data`` object out of a login body (lenient on missing fields)."""
    raw = body.get("data")
    data = cast("dict[str, object]", raw) if isinstance(raw, dict) else {}
    sid = data.get("sid")
    synotoken = data.get("synotoken")
    return SynologySession(
        sid=sid if isinstance(sid, str) and sid else None,
        synotoken=synotoken if isinstance(synotoken, str) else None,
    )


def _extract_error_code(body: dict[str, object]) -> int:
    """Read the DSM ``error.code`` from a ``success:false`` body; 0 when absent/wrong type."""
    raw = body.get("error")
    error = cast("dict[str, object]", raw) if isinstance(raw, dict) else {}
    code = error.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return 0


def _dsm_error(endpoint: str, code: int) -> SynologyError:
    """Map a DSM body error code (NOT 119) to a typed SynologyError.

    119 is handled inline in ``_get`` (re-auth path) and never reaches here. The
    ``status`` field carries the DSM error code, not an HTTP status.
    """
    if code == _DSM_BAD_CREDENTIALS:
        return SynologyError(
            reason="auth", message=f"{endpoint}: DSM error 400 (bad credentials)", status=code
        )
    if code == _DSM_PERMISSION_DENIED:
        # SID valid, account lacks rights — do NOT re-auth.
        return SynologyError(
            reason="api_error",
            message=f"{endpoint}: DSM error 105 (permission denied)",
            status=code,
        )
    if code == _DSM_SESSION_PARAM_SENT:
        # should never happen — we never send session=
        return SynologyError(reason="api_error", message=f"{endpoint}: DSM error 402", status=code)
    return SynologyError(reason="api_error", message=f"{endpoint}: DSM error {code}", status=code)
