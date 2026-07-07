"""vmalert configuration reload helper (STAGE-010-001).

POSTs ``/-/reload`` to the vmalert-metrics sidecar. Warn-on-failure semantics:
the rule file on disk is the source of truth; if the POST fails, vmalert's
``-configCheckInterval=30s`` picks up the change within ~30s regardless.

Mirrors :class:`homelab_monitor.kernel.alertmanager.render.AlertmanagerReloader`
but manages its own ``httpx.AsyncClient`` lifecycle (the CLI has no shared
long-lived client to inject the way the API app lifespan does).
"""

from __future__ import annotations

from typing import Final

import httpx
import structlog
from structlog.stdlib import BoundLogger

DEFAULT_VMALERT_URL: Final[str] = "http://vmalert-metrics:8880"
"""In-cluster DNS name of the vmalert-metrics sidecar. Container-internal only."""

_HTTP_OK_MIN: Final[int] = 200
_HTTP_OK_MAX: Final[int] = 299


class VmalertReloader:
    """POST ``/-/reload`` to vmalert-metrics; warn-on-failure.

    The file on disk (``deploy/vmalert/metrics/*.yaml``) is authoritative;
    vmalert re-reads the directory every ``-configCheckInterval`` (30s in
    prod), so a failed reload just delays the effective change by ~30s.
    Callers therefore log the failure and continue rather than raising.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_VMALERT_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._log: BoundLogger = structlog.get_logger()  # pyright: ignore[reportAssignmentType]

    async def reload(self) -> bool:
        """POST ``/-/reload``; return True on 2xx, False on any failure.

        Never raises. Exceptions and non-2xx responses are logged as warnings
        with structured ``base_url`` / ``status_code`` / ``error`` fields.
        """
        url = f"{self._base_url}/-/reload"
        timeout = httpx.Timeout(5.0, connect=2.0)
        if self._client is not None:
            return await self._post(self._client, url, timeout)
        async with httpx.AsyncClient() as client:
            return await self._post(client, url, timeout)

    async def _post(
        self,
        client: httpx.AsyncClient,
        url: str,
        timeout: httpx.Timeout,
    ) -> bool:
        try:
            resp = await client.post(url, timeout=timeout)
        except httpx.HTTPError as exc:
            self._log.warning(
                "vmalert.reload.unreachable",
                base_url=self._base_url,
                error=str(exc),
            )
            return False
        if not (_HTTP_OK_MIN <= resp.status_code <= _HTTP_OK_MAX):
            self._log.warning(
                "vmalert.reload.non_200",
                base_url=self._base_url,
                status_code=resp.status_code,
            )
            return False
        self._log.info("vmalert.reload.ok", base_url=self._base_url)
        return True


__all__ = ["DEFAULT_VMALERT_URL", "VmalertReloader"]
