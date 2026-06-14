"""In-memory Home-Assistant entity-registry cache (STAGE-005-037).

KERNEL-ONLY: MUST NOT import from ``plugins``. Holds the latest
``config/entity_registry/list`` snapshot and answers a fast per-entity exclusion
query for the HA collectors (availability + z-score).

SECURITY / PRIVACY (mirrors enrichment.py): this module NEVER logs ``entity_id``
or any entity name, and NEVER emits ``entity_id`` as a metric label. Self-metrics
are aggregate-only (count, last-fetch ts, ok/error counter).

FAIL-OPEN: until the first successful fetch the snapshot is empty + not populated;
``is_excluded`` returns False so no entity is dropped. A refresh failure KEEPS the
prior snapshot (does not clear it).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from homelab_monitor.kernel.ha.enrichment import (
    RegistryEntry,
    build_registry_index,
    extract_registry,
)
from homelab_monitor.kernel.ha.errors import HaError

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from homelab_monitor.kernel.config import HaRegistryConfig
    from homelab_monitor.kernel.plugins.io import MetricsWriter

_REGISTRY_LIST_COMMAND = "config/entity_registry/list"

M_REGISTRY_ENTRIES = "homelab_ha_entity_registry_entries"
M_REGISTRY_LAST_FETCH_TS = "homelab_ha_entity_registry_last_fetch_timestamp_seconds"
M_REGISTRY_FETCH_TOTAL = "homelab_ha_entity_registry_fetch_total"

# Until the first populated fetch, retry on this short backoff (doubling, capped)
# so a first fetch that races the WS connect/auth handshake recovers in seconds
# rather than waiting the full refresh_seconds. STAGE-005-037 startup-race fix.
_INITIAL_REFRESH_BACKOFF_SECONDS: float = 2.0
_MAX_INITIAL_REFRESH_BACKOFF_SECONDS: float = 30.0

__all__ = [
    "HaEntityRegistryCache",
    "RegistrySnapshot",
]


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """An immutable point-in-time view of the entity registry.

    ``entries`` maps ``entity_id`` -> :class:`RegistryEntry`. ``fetched_at`` is the
    UTC time of the fetch that produced this snapshot, or ``None`` for the empty
    bootstrap snapshot (before any successful fetch).
    """

    entries: dict[str, RegistryEntry] = field(default_factory=lambda: {})
    fetched_at: datetime | None = None

    @property
    def is_populated(self) -> bool:
        """True iff at least one successful fetch has produced this snapshot."""
        return self.fetched_at is not None

    def is_excluded(self, entity_id: str, cfg: HaRegistryConfig) -> bool:
        """Return True iff ``entity_id`` should be DROPPED per ``cfg``.

        FAIL-OPEN: returns False when the snapshot is not populated (no fetch yet)
        or the entity is unknown to the registry. An entity is excluded when, per
        the cfg toggles, it is registry-disabled, registry-hidden, or its
        ``entity_category`` (lowercased) is in ``cfg.exclude_categories``.
        """
        if not self.is_populated:
            return False
        entry = self.entries.get(entity_id)
        if entry is None:
            return False
        if cfg.exclude_disabled and entry.disabled_by is not None:
            return True
        if cfg.exclude_hidden and entry.hidden_by is not None:
            return True
        return entry.entity_category is not None and (
            entry.entity_category.strip().lower() in cfg.exclude_categories
        )


class HaEntityRegistryCache:
    """Owns the registry snapshot + a supervised refresh loop.

    Mirrors :class:`HomeAssistantWebsocketClient`'s task lifecycle exactly
    (``start_task`` idempotent; ``stop_task`` cancel+suppress+await+clear).
    ``refresh`` is independently callable and side-effect-complete for tests.
    """

    def __init__(
        self,
        *,
        ws_client: _RegistryWsClient,
        config: HaRegistryConfig,
        metrics_writer: MetricsWriter,
        log: BoundLogger,
    ) -> None:
        """Initialize the cache (does NOT fetch; call ``refresh`` or ``start_task``).

        Args:
            ws_client: anything with ``async send_command(type_, **fields)`` that
                returns ``list[object] | dict[str, object] | HaError``.
            config: the loaded :class:`HaRegistryConfig`.
            metrics_writer: shared writer for the ``homelab_ha_entity_registry_*``
                self-metrics.
            log: bound structlog logger (caller binds
                ``component="ha_entity_registry"``).
        """
        self._ws = ws_client
        self._cfg = config
        self._metrics = metrics_writer
        self._log = log
        self._snapshot = RegistrySnapshot()
        self._task: asyncio.Task[None] | None = None

    def snapshot(self) -> RegistrySnapshot:
        """Return the current (immutable) snapshot."""
        return self._snapshot

    async def refresh(self) -> None:
        """Fetch the registry once and replace the snapshot on success.

        On ``HaError``: emit the error counter, KEEP the prior snapshot. On a
        non-error result: build the index, replace the snapshot with a populated
        one, and emit the success self-metrics. Never raises.
        """
        result = await self._ws.send_command(_REGISTRY_LIST_COMMAND)
        if isinstance(result, HaError):
            self._metrics.write_counter(M_REGISTRY_FETCH_TOTAL, 1.0, {"result": "error"})
            return
        entries = build_registry_index(extract_registry(result))
        fetched_at = datetime.now(UTC)
        self._snapshot = RegistrySnapshot(entries=entries, fetched_at=fetched_at)
        self._metrics.write_counter(M_REGISTRY_FETCH_TOTAL, 1.0, {"result": "ok"})
        self._metrics.write_gauge(M_REGISTRY_ENTRIES, float(len(entries)), {})
        self._metrics.write_gauge(M_REGISTRY_LAST_FETCH_TS, fetched_at.timestamp(), {})

    def start_task(self) -> None:
        """Launch the supervised refresh loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="ha_entity_registry")

    async def stop_task(self) -> None:
        """Cancel + await the refresh loop; clear the handle."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run_loop(self) -> None:
        """Refresh-first, then sleep, repeat. Fail-open on unexpected errors.

        Until the FIRST successful (populated) fetch, retry on a short backoff
        (2s -> 4s -> ... capped at 30s) so a first fetch that races the WS
        connect/auth handshake recovers in seconds rather than waiting the full
        ``refresh_seconds``. Once populated, settle into the normal cadence.

        ``refresh`` itself never raises, but the loop body is guarded so any
        unexpected exception logs + continues instead of killing the loop.
        Re-raises CancelledError so ``stop_task`` observes cancellation.
        """
        initial_backoff = _INITIAL_REFRESH_BACKOFF_SECONDS
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning("ha_entity_registry.refresh_error", error=str(exc))
            if self._snapshot.is_populated:
                await asyncio.sleep(self._cfg.refresh_seconds)
            else:
                await asyncio.sleep(initial_backoff)
                initial_backoff = min(initial_backoff * 2, _MAX_INITIAL_REFRESH_BACKOFF_SECONDS)


class _RegistryWsClient(Protocol):
    """The minimal ws-client surface the cache needs (one-shot RPC)."""

    async def send_command(
        self, type_: str, **fields: object
    ) -> dict[str, object] | list[object] | HaError:
        """Send a one-shot WS command and await the result (or an HaError)."""
        ...
