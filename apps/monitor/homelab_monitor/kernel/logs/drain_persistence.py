"""Persistence layer for the drain3 log-signature engine (STAGE-004-025).

drain3's PersistenceHandler contract is synchronous (save_state/load_state take/return
bytes with no I/O hooks for async). We satisfy that contract with an in-memory
`_BufferingHandler` per model, and keep all real (async) DB I/O on `SqlitePersistence`.

The engine's flow per model:
  1. `await persistence.load_state_for(key)` -> the stored snapshot bytes (or None).
  2. Build a `_BufferingHandler(loaded=<bytes>)`; drain3 reads it via load_state() in __init__.
  3. On `snapshot()`, the engine calls `miner.save_state("manual")`, which writes current
     bytes into the handler's `.pending`; the engine reads `.pending` and calls
     `await persistence.persist(...)`.

`snapshot` bytes are the base64+zlib-compressed jsonpickle blob drain3 produces
(snapshot_compress_state=True). first_seen_map is a JSON object {template_hash: first_seen_ms}.
All *_ts / updated_at values are unix-ms INTEGER.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from drain3.persistence_handler import (
    PersistenceHandler,  # pyright: ignore[reportMissingTypeStubs]  -- drain3 has no type stubs
)
from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository


@dataclass(frozen=True, slots=True)
class StoredModel:
    """A drain_models row as loaded for engine bootstrap.

    `snapshot` is the raw drain3 state blob (base64+zlib bytes), or None when the
    model row does not yet exist. `first_seen_map` is decoded from the row's JSON.
    """

    snapshot: bytes | None
    first_seen_map: dict[str, int]


class _BufferingHandler(PersistenceHandler):
    """In-memory drain3 PersistenceHandler.

    `loaded` is returned verbatim by load_state (drain3 reads it once in __init__).
    `pending` captures the most recent save_state bytes; the engine reads it during
    snapshot(). Both methods are pure memory ops — no I/O — so drain3's synchronous
    contract is honoured while the real async DB I/O lives on SqlitePersistence.

    Note: drain3 calls save_state synchronously on every cluster/template change
    during add_log_message; this handler absorbs those into `.pending` as cheap
    memory writes (no I/O). `.pending` being non-None does NOT mean "manual snapshot
    pending" — it means "current serialized state is buffered." Real DB persistence
    happens only when DrainEngine.snapshot() reads `.pending` and writes it via
    SqliteRepository.
    """

    def __init__(self, loaded: bytes | None = None) -> None:
        self.loaded: bytes | None = loaded
        self.pending: bytes | None = None

    def save_state(self, state: bytes) -> None:
        self.pending = state

    def load_state(self) -> bytes | None:  # pyright: ignore[reportIncompatibleMethodOverride]  -- drain3 ABC incorrectly annotates return as None; actual contract returns saved bytes
        return self.loaded


class DrainPersistence(Protocol):
    """Persistence contract the DrainEngine depends on (testable seam)."""

    async def load_state_for(self, model_key: str) -> StoredModel:
        """Return the stored snapshot + first_seen_map for `model_key`.

        Returns StoredModel(snapshot=None, first_seen_map={}) when no row exists.
        """
        ...

    async def persist(  # noqa: PLR0913 -- params mirror the drain_models row
        self,
        *,
        model_key: str,
        snapshot: bytes,
        line_count: int,
        template_count: int,
        last_processed_ts: int | None,
        first_seen_map_json: str,
        updated_at: int,
    ) -> None:
        """Upsert the full drain_models row for `model_key`."""
        ...

    async def get_max_cursor(self) -> int | None:
        """Return MAX(last_processed_ts) across all drain_models rows, or None.

        None when the table is empty OR every row has a NULL last_processed_ts.
        Used by the DrainConsumer cold-start seed to RESUME from the furthest-
        advanced per-model cursor rather than replaying history.
        """
        ...


class SqlitePersistence:
    """`DrainPersistence` backed by the drain_models table."""

    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    async def load_state_for(self, model_key: str) -> StoredModel:
        rows = await self._repo.fetch_all(
            text("SELECT snapshot, first_seen_map FROM drain_models WHERE model_key = :key"),
            {"key": model_key},
        )
        if not rows:
            return StoredModel(snapshot=None, first_seen_map={})
        row = rows[0]
        snapshot_raw: Any = row.snapshot  # pyright: ignore[reportAny]  -- SQLite Row
        first_seen_raw: Any = row.first_seen_map  # pyright: ignore[reportAny]  -- SQLite Row
        snapshot = bytes(snapshot_raw) if snapshot_raw is not None else None
        first_seen_map = _decode_first_seen(str(first_seen_raw))
        return StoredModel(snapshot=snapshot, first_seen_map=first_seen_map)

    async def persist(  # noqa: PLR0913 -- params mirror the drain_models row
        self,
        *,
        model_key: str,
        snapshot: bytes,
        line_count: int,
        template_count: int,
        last_processed_ts: int | None,
        first_seen_map_json: str,
        updated_at: int,
    ) -> None:
        async with self._repo.transaction() as conn:
            await conn.execute(
                text(
                    "INSERT INTO drain_models "
                    "  (model_key, snapshot, line_count, template_count, "
                    "   last_processed_ts, first_seen_map, updated_at) "
                    "VALUES "
                    "  (:key, :snap, :lc, :tc, :lpts, :fsm, :ua) "
                    "ON CONFLICT(model_key) DO UPDATE SET "
                    "  snapshot = excluded.snapshot, "
                    "  line_count = excluded.line_count, "
                    "  template_count = excluded.template_count, "
                    "  last_processed_ts = excluded.last_processed_ts, "
                    "  first_seen_map = excluded.first_seen_map, "
                    "  updated_at = excluded.updated_at"
                ),
                {
                    "key": model_key,
                    "snap": snapshot,
                    "lc": line_count,
                    "tc": template_count,
                    "lpts": last_processed_ts,
                    "fsm": first_seen_map_json,
                    "ua": updated_at,
                },
            )

    async def get_max_cursor(self) -> int | None:
        rows = await self._repo.fetch_all(
            text("SELECT MAX(last_processed_ts) AS max_cursor FROM drain_models"),
            {},
        )
        if not rows:  # pragma: no cover  -- aggregate always returns one row
            return None
        raw: Any = rows[0].max_cursor  # pyright: ignore[reportAny]  -- SQLite Row
        if raw is None:
            return None
        return int(raw)


def _decode_first_seen(raw: str) -> dict[str, int]:
    """Decode the first_seen_map JSON column into {template_hash: first_seen_ms}.

    Tolerant: an empty/whitespace string or a non-object JSON value yields {}.
    """
    if not raw.strip():
        return {}
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in parsed.items():  # pyright: ignore[reportUnknownVariableType]
        try:
            out[str(k)] = int(v)  # pyright: ignore[reportUnknownArgumentType]  -- JSON value coerced; non-int raises, caught below
        except (TypeError, ValueError):
            continue
    return out


__all__ = [
    "DrainPersistence",
    "SqlitePersistence",
    "StoredModel",
    "_BufferingHandler",
]
