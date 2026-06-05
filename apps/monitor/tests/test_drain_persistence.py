"""Tests for SqlitePersistence + _BufferingHandler (STAGE-004-025).

DB: tempfile-backed SQLite + alembic head via the `repo` fixture (conftest).
"""

from __future__ import annotations

import json

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.drain_persistence import (
    SqlitePersistence,
    StoredModel,
    _BufferingHandler,
    _decode_first_seen,  # pyright: ignore[reportPrivateUsage]
)


def test_buffering_handler_load_returns_preloaded_bytes() -> None:
    handler = _BufferingHandler(loaded=b"preloaded")
    assert handler.load_state() == b"preloaded"
    assert handler.pending is None


def test_buffering_handler_save_buffers_into_pending() -> None:
    handler = _BufferingHandler(loaded=None)
    assert handler.load_state() is None
    handler.save_state(b"new-state")
    assert handler.pending == b"new-state"
    # load_state still returns the ORIGINAL loaded value (None), not pending.
    assert handler.load_state() is None


async def test_load_state_for_missing_returns_empty(repo: SqliteRepository) -> None:
    persistence = SqlitePersistence(repo)
    stored = await persistence.load_state_for("nope")
    assert stored == StoredModel(snapshot=None, first_seen_map={})


async def test_persist_then_load_round_trip(repo: SqliteRepository) -> None:
    persistence = SqlitePersistence(repo)
    fsm = {"abc": 1000, "def": 2000}
    await persistence.persist(
        model_key="svc-x",
        snapshot=b"\x00\x01\x02blob",
        line_count=5,
        template_count=2,
        last_processed_ts=12345,
        first_seen_map_json=json.dumps(fsm),
        updated_at=99999,
    )
    stored = await persistence.load_state_for("svc-x")
    assert stored.snapshot == b"\x00\x01\x02blob"
    assert stored.first_seen_map == fsm


async def test_persist_upsert_overwrites(repo: SqliteRepository) -> None:
    persistence = SqlitePersistence(repo)
    await persistence.persist(
        model_key="svc-y",
        snapshot=b"first",
        line_count=1,
        template_count=1,
        last_processed_ts=1,
        first_seen_map_json=json.dumps({"h1": 1}),
        updated_at=1,
    )
    await persistence.persist(
        model_key="svc-y",
        snapshot=b"second",
        line_count=9,
        template_count=3,
        last_processed_ts=2,
        first_seen_map_json=json.dumps({"h1": 1, "h2": 2}),
        updated_at=2,
    )
    stored = await persistence.load_state_for("svc-y")
    assert stored.snapshot == b"second"
    assert stored.first_seen_map == {"h1": 1, "h2": 2}


async def test_persist_null_last_processed_ts(repo: SqliteRepository) -> None:
    persistence = SqlitePersistence(repo)
    await persistence.persist(
        model_key="svc-z",
        snapshot=b"z",
        line_count=0,
        template_count=0,
        last_processed_ts=None,
        first_seen_map_json="{}",
        updated_at=7,
    )
    stored = await persistence.load_state_for("svc-z")
    assert stored.snapshot == b"z"
    assert stored.first_seen_map == {}


async def test_load_state_for_decodes_malformed_first_seen_as_empty(
    repo: SqliteRepository,
) -> None:
    """A non-JSON / non-object first_seen_map column decodes to {} (defensive)."""
    persistence = SqlitePersistence(repo)
    # Persist a deliberately malformed first_seen_map by writing raw via persist's JSON arg.
    await persistence.persist(
        model_key="svc-bad",
        snapshot=b"b",
        line_count=0,
        template_count=0,
        last_processed_ts=None,
        first_seen_map_json="not-json",
        updated_at=1,
    )
    stored = await persistence.load_state_for("svc-bad")
    assert stored.first_seen_map == {}


async def test_load_state_for_decodes_non_object_first_seen_as_empty(
    repo: SqliteRepository,
) -> None:
    persistence = SqlitePersistence(repo)
    await persistence.persist(
        model_key="svc-arr",
        snapshot=b"b",
        line_count=0,
        template_count=0,
        last_processed_ts=None,
        first_seen_map_json="[1,2,3]",
        updated_at=1,
    )
    stored = await persistence.load_state_for("svc-arr")
    assert stored.first_seen_map == {}


async def test_load_state_for_skips_non_int_first_seen_values(
    repo: SqliteRepository,
) -> None:
    """A first_seen_map value that is not int-coercible is dropped, others kept."""
    persistence = SqlitePersistence(repo)
    await persistence.persist(
        model_key="svc-mix",
        snapshot=b"b",
        line_count=0,
        template_count=0,
        last_processed_ts=None,
        first_seen_map_json='{"good": 5, "bad": "x"}',
        updated_at=1,
    )
    stored = await persistence.load_state_for("svc-mix")
    assert stored.first_seen_map == {"good": 5}


def test_decode_first_seen_empty_string() -> None:
    assert _decode_first_seen("") == {}
    assert _decode_first_seen("   ") == {}
