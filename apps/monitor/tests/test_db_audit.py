"""Tests for ``kernel.db.audit.audit_write``."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text

from homelab_monitor.kernel.db.audit import audit_write
from homelab_monitor.kernel.db.repository import SqliteRepository


async def test_audit_write_inserts_row_with_minimal_args(
    repo: SqliteRepository,
) -> None:
    """Required-only arguments produce a row with NULL JSON columns and NULL ip."""
    await audit_write(repo, who="system", what="boot")
    row = await repo.fetch_one(
        text('SELECT id, who, what, "when", before_json, after_json, ip FROM audit_log')
    )
    assert row is not None
    assert row.who == "system"
    assert row.what == "boot"
    assert row.before_json is None
    assert row.after_json is None
    assert row.ip is None
    parsed = datetime.fromisoformat(row.when)
    assert parsed.tzinfo is not None


async def test_audit_write_serialises_before_and_after(repo: SqliteRepository) -> None:
    """Dict ``before``/``after`` are JSON-encoded; ip is stored verbatim."""
    await audit_write(
        repo,
        who="jakekausler",
        what="rotate-secret",
        before={"hash": "old"},
        after={"hash": "new"},
        ip="10.0.0.5",
    )
    row = await repo.fetch_one(
        text("SELECT before_json, after_json, ip FROM audit_log WHERE who = :w"),
        {"w": "jakekausler"},
    )
    assert row is not None
    assert json.loads(row.before_json) == {"hash": "old"}
    assert json.loads(row.after_json) == {"hash": "new"}
    assert row.ip == "10.0.0.5"


async def test_audit_write_ids_are_unique(repo: SqliteRepository) -> None:
    """Two calls produce distinct UUIDv7 ids."""
    await audit_write(repo, who="system", what="event-a")
    await audit_write(repo, who="system", what="event-b")
    rows = await repo.fetch_all(text("SELECT id FROM audit_log"))
    ids = {row.id for row in rows}
    assert len(ids) == 2  # noqa: PLR2004
