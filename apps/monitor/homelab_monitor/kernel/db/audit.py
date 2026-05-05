"""Audit-log helper: every state-changing operation routes through this."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


async def audit_write(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    who: str,
    what: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Insert a row into ``audit_log``.

    ``before`` / ``after`` are JSON-serialised. ``when`` is set to the current
    UTC instant (ISO-8601). ``id`` is a fresh UUIDv7. The column name ``when``
    must be quoted because ``WHEN`` is a reserved SQL word.
    """
    before_json = json.dumps(before) if before is not None else None
    after_json = json.dumps(after) if after is not None else None
    stmt = text(
        'INSERT INTO audit_log (id, who, what, "when", before_json, after_json, ip) '
        "VALUES (:id, :who, :what, :when, :before_json, :after_json, :ip)"
    )
    await repo.execute(
        stmt,
        {
            "id": uuid7(),
            "who": who,
            "what": what,
            "when": utc_now_iso(),
            "before_json": before_json,
            "after_json": after_json,
            "ip": ip,
        },
    )
