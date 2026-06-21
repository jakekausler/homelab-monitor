"""Unifi REST client (STAGE-007-001).

Kernel infrastructure mirroring :mod:`homelab_monitor.kernel.ha.client`. A single
read-only API key (header ``X-API-KEY``) authenticates BOTH the official v1
Integrations API and the classic reverse-engineered API on this UDM firmware.

Construction (lifespan, once at startup)::

    UnifiRestClient(
        base_url=unifi_config.base_url,                 # https://192.168.2.1
        http=unifi_http_client,                          # dedicated verify=False client
        key_provider=lambda: ttl_resolver.current().get("unifi_api_key"),
        site_id=unifi_config.site_id,                    # classic site NAME, "default"
    )

    Two site identifiers: classic ``site_name`` ("default", for /api/s/{name}/)
    and v1 ``v1_site_id`` (UUID, resolved from v1/sites, for /sites/{uuid}/).

The client BORROWS a DEDICATED ``httpx.AsyncClient`` created with ``verify=False``
in lifespan (the UDM uses a self-signed cert ``CN=unifi.local``). Lifespan owns
that client's construction + teardown; this client never closes it.

The ``key_provider`` is invoked per request (NOT stored) so a rotated /
post-startup-added ``unifi_api_key`` is picked up without a restart, and the key
value is never held in a client field.

READ-ONLY / OBSERVE-ONLY: only GET helpers exist; there are no write methods.

SECURITY: the API key never appears in any returned ``UnifiError.message`` nor in
any log line. Error messages are built from method + endpoint label + status only.

SCAFFOLDING: the typed GET helpers below establish the full surface for Wave-B/C
collectors (STAGE-007-005..014); not all are consumed in this stage.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

import httpx

from homelab_monitor.kernel.unifi.errors import UnifiError

_HTTP_UNAUTHORIZED: Final[int] = 401
_HTTP_FORBIDDEN: Final[int] = 403
_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
_HTTP_OK_FLOOR: Final[int] = 200
_HTTP_OK_CEIL: Final[int] = 300  # exclusive upper bound for the 2xx success band

_V1_PREFIX: Final[str] = "/proxy/network/integrations/v1"
_V2_PREFIX: Final[str] = "/proxy/network/v2/api/site"


@dataclass(frozen=True, slots=True)
class UnifiResponse:
    """A successful Unifi GET result.

    ``payload`` is the parsed JSON body (an object or array). Its static type is
    ``object`` — the no-``Any`` escape hatch — so callers narrow per-key with
    ``isinstance`` guards (collectors do this in Wave B/C). ``took_seconds`` is the
    monotonic wall-clock duration of the underlying httpx call (read by the
    API-latency collector, STAGE-007-013). ``endpoint`` is a stable label
    (e.g. ``"v1/sites"`` or ``"stat/sysinfo"``) used for the
    ``homelab_unifi_api_took_seconds{endpoint}`` metric.
    """

    payload: object
    took_seconds: float
    endpoint: str


class UnifiRestClient:
    """Read-only Unifi REST client implementing the :class:`UnifiClient` Protocol."""

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        key_provider: Callable[[], str | None],
        site_id: str = "default",
    ) -> None:
        """Initialize the client.

        Args:
            base_url: UDM base URL (e.g. ``https://192.168.2.1``). Trailing slashes
                are stripped so path concatenation is unambiguous.
            http: the DEDICATED ``verify=False`` ``httpx.AsyncClient`` (borrowed;
                lifespan owns its lifecycle).
            key_provider: zero-arg callable returning the current read-only API key,
                or ``None`` when no key is configured. Called per request; never stored.
            site_id: the controller site id (default ``"default"``). Re-cached by
                :meth:`resolve_site_id` from ``v1/sites`` at startup.
        """
        self._base_url: str = base_url.rstrip("/")
        self._http: httpx.AsyncClient = http
        self._key_provider: Callable[[], str | None] = key_provider
        # Classic API (/api/s/{name}/...) needs the SHORT site NAME (e.g. "default").
        # v1 Integrations site-scoped paths (/sites/{uuid}/...) need the v1 site UUID.
        # These are DIFFERENT identifiers; conflating them 401s the classic API.
        self.site_name: str = site_id
        # Seeded to the name; resolve_site_id() (called ONCE at startup) caches the
        # real v1 UUID here. This client does NOT retry resolution — if startup
        # resolution fails, v1_site_id stays "default" until process restart, so a
        # Wave-B/C collector that needs it must re-invoke resolve_site_id() itself.
        self.v1_site_id: str = site_id

    # ---- public v1 helpers (UnifiClient Protocol) ----

    async def v1_sites(self) -> UnifiResponse | UnifiError:
        """GET v1 /sites — controller sites (used to resolve the site id)."""
        return await self._get_v1("/sites", "v1/sites")

    async def v1_devices(self) -> UnifiResponse | UnifiError:
        """GET v1 /sites/{site_id}/devices — device inventory for the cached site."""
        return await self._get_v1(f"/sites/{self.v1_site_id}/devices", "v1/devices")

    async def v1_device(self, device_id: str) -> UnifiResponse | UnifiError:
        """GET v1 /devices/{device_id} — a single device's detail."""
        return await self._get_v1(f"/devices/{device_id}", "v1/device")

    async def v1_device_stats(self, device_id: str) -> UnifiResponse | UnifiError:
        """GET v1 /devices/{device_id}/statistics/latest — latest device stats."""
        return await self._get_v1(f"/devices/{device_id}/statistics/latest", "v1/device_stats")

    async def v1_clients(self) -> UnifiResponse | UnifiError:
        """GET v1 /sites/{site_id}/clients — coarse client list for the cached site."""
        return await self._get_v1(f"/sites/{self.v1_site_id}/clients", "v1/clients")

    # ---- public classic helpers (UnifiClient Protocol) ----

    async def stat_device(self) -> UnifiResponse | UnifiError:
        """GET classic stat/device — fat per-device records (ports/PoE/radios/PDU/temp)."""
        return await self._get_classic("stat/device")

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        """GET classic stat/sta — active-client identity + per-client stats."""
        return await self._get_classic("stat/sta")

    async def stat_alluser(self) -> UnifiResponse | UnifiError:
        """GET classic stat/alluser — all known clients (active + historical)."""
        return await self._get_classic("stat/alluser")

    async def stat_health(self) -> UnifiResponse | UnifiError:
        """GET classic stat/health — subsystem health incl. the www/WAN block."""
        return await self._get_classic("stat/health")

    async def stat_stadpi(self) -> UnifiResponse | UnifiError:
        """GET classic stat/stadpi — per-client per-app DPI byte counters."""
        return await self._get_classic("stat/stadpi")

    async def rest_networkconf(self) -> UnifiResponse | UnifiError:
        """GET classic rest/networkconf — DHCP ranges, dhcpd_dns_*, reservations."""
        return await self._get_classic("rest/networkconf")

    async def rest_alarm(self) -> UnifiResponse | UnifiError:
        """GET classic rest/alarm?archived=false — active IDS/IPS alarms/threats."""
        return await self._get_classic("rest/alarm?archived=false")

    async def stat_sysinfo(self) -> UnifiResponse | UnifiError:
        """GET classic stat/sysinfo — controller version + system info."""
        return await self._get_classic("stat/sysinfo")

    async def v2_traffic(self, start_ms: int, end_ms: int) -> UnifiResponse | UnifiError:
        """GET v2 traffic — per-client per-app usage for the [start_ms, end_ms] window.

        ``start_ms``/``end_ms`` are epoch-MILLISECONDS (epoch-seconds silently returns an
        empty 200 on this firmware). The payload is a bare object with ``client_usage_by_app``
        and ``total_usage_by_app`` arrays (no ``{meta,data}`` envelope).
        """
        params = {"start": str(start_ms), "end": str(end_ms)}
        return await self._get_v2("traffic", "v2/traffic", params)

    # ---- eager, non-fatal site-id resolution ----

    async def resolve_site_id(self) -> UnifiError | None:  # noqa: PLR0911 -- centralized site-id shape validation; each branch is a distinct malformed-response case
        """Resolve + cache the controller site id from ``v1/sites``.

        Caches the v1 site UUID into ``self.v1_site_id`` (used by v1 site-scoped
        paths). The classic ``self.site_name`` (used by ``/api/s/{name}/...``) is a
        DIFFERENT identifier and is left UNCHANGED — the classic API rejects the v1
        UUID with 401.

        Called once at startup (lifespan). NON-FATAL: on any failure the cached
        ``v1_site_id`` is left unchanged (default ``"default"``) and the UnifiError is
        returned for the caller to LOG and continue — a startup UDM/key failure must
        NOT crash the app. Returns ``None`` on success.

        The v1 Integrations ``/sites`` response is a JSON object with a ``data`` array
        of site objects each carrying an ``id``. We defensively extract the first
        site's ``id``; any shape mismatch maps to a ``bad_response`` UnifiError.
        """
        result = await self.v1_sites()
        if isinstance(result, UnifiError):
            return result
        payload = result.payload
        if not isinstance(payload, dict):
            return UnifiError(reason="bad_response", message="v1/sites: body is not an object")
        body = cast("dict[str, object]", payload)
        data = body.get("data")
        if not isinstance(data, list):
            return UnifiError(reason="bad_response", message="v1/sites: 'data' is not a list")
        data_list = cast("list[object]", data)
        if not data_list:
            return UnifiError(reason="bad_response", message="v1/sites: 'data' is empty")
        first = data_list[0]
        if not isinstance(first, dict):
            return UnifiError(
                reason="bad_response", message="v1/sites: site entry is not an object"
            )
        entry = cast("dict[str, object]", first)
        site_id = entry.get("id")
        if not isinstance(site_id, str):
            return UnifiError(reason="bad_response", message="v1/sites: site 'id' is not a string")
        self.v1_site_id = site_id
        return None

    # ---- internals ----

    async def _get_v1(self, path: str, endpoint: str) -> UnifiResponse | UnifiError:
        """Build a v1 Integrations URL and perform the GET.

        ``path`` is appended to ``{base_url}/proxy/network/integrations/v1`` — so
        ``_get_v1("/sites", ...)`` hits ``.../integrations/v1/sites``.
        """
        url = f"{self._base_url}{_V1_PREFIX}{path}"
        return await self._request("GET", url, endpoint)

    async def _get_classic(self, ep: str) -> UnifiResponse | UnifiError:
        """Build a classic API URL and perform the GET.

        ``ep`` is appended to ``{base_url}/proxy/network/api/s/{site_name}/`` — so
        ``_get_classic("stat/sysinfo")`` hits ``.../api/s/default/stat/sysinfo``.
        The classic API needs the SHORT site NAME, never the v1 site UUID.
        """
        url = f"{self._base_url}/proxy/network/api/s/{self.site_name}/{ep}"
        return await self._request("GET", url, ep)

    async def _get_v2(
        self, ep: str, endpoint: str, params: dict[str, str]
    ) -> UnifiResponse | UnifiError:
        """Build a v2 site-scoped API URL (with query params) and perform the GET.

        ``ep`` is appended to ``{base_url}/proxy/network/v2/api/site/{site_name}/`` — so
        ``_get_v2("traffic", "v2/traffic", {...})`` hits
        ``.../v2/api/site/default/traffic?start=...&end=...``. The v2 API returns a BARE
        JSON object (NO ``{meta,data}`` envelope); ``UnifiResponse.payload`` is that object.
        The v2 API uses the classic SHORT site NAME (``self.site_name``), not the v1 UUID.
        """
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self._base_url}{_V2_PREFIX}/{self.site_name}/{ep}?{query}"
        return await self._request("GET", url, endpoint)

    async def _request(self, method: str, url: str, endpoint: str) -> UnifiResponse | UnifiError:
        """Perform an authenticated GET, timing it and mapping every failure to UnifiError.

        Centralizes ALL branching: key check (None -> auth, no network call),
        X-API-KEY header attachment, monotonic timing, transport-error mapping,
        status mapping, and JSON parsing. Keeping the branches here keeps the
        public helpers branchless and the coverage surface in one place.

        GET-only by contract: every caller passes ``"GET"``. The ``method`` param
        is retained for signature parity with the HA exemplar — do NOT add write
        helpers; the Unifi integration is observe-only.
        """
        key = self._key_provider()
        if key is None:
            return UnifiError(reason="auth", message=f"{endpoint}: no API key configured")
        headers = {"X-API-KEY": key}
        start = time.monotonic()
        try:
            resp = await self._http.request(method, url, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return UnifiError(
                reason="unreachable", message=f"{method} {endpoint}: connection failed"
            )
        except (httpx.ReadTimeout, httpx.TimeoutException):
            return UnifiError(reason="timeout", message=f"{method} {endpoint}: timed out")
        took_seconds = time.monotonic() - start
        status_err = _status_error(resp, method, endpoint)
        if status_err is not None:
            return status_err
        try:
            payload: object = resp.json()
        except ValueError:
            return UnifiError(
                reason="bad_response", message=f"{method} {endpoint}: response is not JSON"
            )
        return UnifiResponse(payload=payload, took_seconds=took_seconds, endpoint=endpoint)


def _status_error(resp: httpx.Response, method: str, endpoint: str) -> UnifiError | None:
    """Return a UnifiError for a non-2xx status, or None for 2xx.

    The ``message`` is built from method + endpoint label + status ONLY — never the
    API key or any raw header value.
    """
    status = resp.status_code
    if _HTTP_OK_FLOOR <= status < _HTTP_OK_CEIL:
        return None
    if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
        return UnifiError(
            reason="auth", message=f"{method} {endpoint}: HTTP {status}", status=status
        )
    if status == _HTTP_TOO_MANY_REQUESTS:
        return UnifiError(
            reason="rate_limited", message=f"{method} {endpoint}: HTTP {status}", status=status
        )
    return UnifiError(
        reason="http_error", message=f"{method} {endpoint}: HTTP {status}", status=status
    )
