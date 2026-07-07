"""Alertmanager silences HTTP client — read-only wrapper over ``/api/v2/silences``.

Thin sibling of :class:`AlertmanagerReloader` (``render.py``). Reuses the shared
``ctx.http`` client for connection reuse. Raises ``httpx.HTTPError`` on network
failure or non-2xx so the reconciler can mark the tick failed and skip the
resolution phase.
"""

# NOTE: This client assumes Alertmanager is reachable on the internal
# compose network without auth (the standard homelab-monitor deployment).
# If AM is later gated behind auth, extend the constructor to accept an
# httpx.BasicAuth (or a bearer-token env var) and pass it to each request.

from __future__ import annotations

from typing import Final, TypedDict, cast

import httpx
import structlog

_HTTP_OK: Final[int] = 200
_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(5.0, connect=2.0)


class Matcher(TypedDict, total=False):
    """One Alertmanager silence matcher (v2 API shape)."""

    name: str
    value: str
    isRegex: bool
    isEqual: bool


class Silence(TypedDict, total=False):
    """One Alertmanager silence (v2 API shape, fields we consume)."""

    id: str
    status: dict[str, str]
    startsAt: str
    updatedAt: str
    endsAt: str
    matchers: list[Matcher]


class SilencesClient:
    """GET ``/api/v2/silences`` from Alertmanager.

    Raises ``httpx.HTTPError`` on any failure; the caller (KarmaOutcomeReconciler)
    catches this to mark the tick failed and skip its work.
    """

    def __init__(
        self,
        *,
        am_url: str,
        http_client: httpx.AsyncClient,
        log: structlog.BoundLogger,
    ) -> None:
        self._am_url = am_url.rstrip("/")
        self._http = http_client
        self._log = log

    async def list_silences(self) -> list[Silence]:
        """Return the JSON list from ``GET /api/v2/silences``.

        The AM v2 response shape (relevant fields):

            [
                {
                    "id": "8f...",
                    "status": {"state": "active" | "expired" | "pending"},
                    "startsAt": "2026-06-01T12:00:00Z",
                    "endsAt": "...",
                    "matchers": [
                        {
                            "name": "alertname",
                            "value": "DiskFull",
                            "isRegex": false,
                            "isEqual": true,
                        },
                        ...
                    ],
                    ...
                },
                ...
            ]

        Raises:
            httpx.HTTPError: any network error, timeout, or non-2xx response.
        """
        url = f"{self._am_url}/api/v2/silences"
        try:
            resp = await self._http.get(url, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            self._log.warning(
                "alertmanager.silences.unreachable",
                am_url=self._am_url,
                error=str(exc),
            )
            raise
        if resp.status_code != _HTTP_OK:
            self._log.warning(
                "alertmanager.silences.non_200",
                am_url=self._am_url,
                status_code=resp.status_code,
            )
            raise httpx.HTTPStatusError(
                f"AM /api/v2/silences returned {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        data = resp.json()
        if not isinstance(data, list):
            self._log.warning(
                "alertmanager.silences.unexpected_shape",
                am_url=self._am_url,
                type=type(data).__name__,
            )
            raise httpx.HTTPError("AM /api/v2/silences did not return a list")
        # Runtime shape: JSON list of JSON objects. pyright can't prove this
        # without a TypedDict; we keep the pragmatic dict[str, Any] shape used
        # throughout the codebase.
        result: list[Silence] = []
        for item in cast(list[object], data):
            if isinstance(item, dict):
                result.append(cast(Silence, item))
        return result


__all__ = ["SilencesClient"]
