"""Audit-log helper: every state-changing operation routes through this."""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy import text

from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


async def audit_write(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    who: str,
    what: str,
    before: Mapping[str, object] | None = None,
    after: Mapping[str, object] | None = None,
    ip: str | None = None,
) -> None:
    """Insert a row into ``audit_log``.

    ``who`` is required (no default) — callers must pass a stable identifier
    such as ``"system"`` for kernel-initiated writes or the username for
    user-initiated writes. Keyword-only to prevent positional-arg mistakes.

    ``before`` / ``after`` are arbitrary JSON-serialisable mappings; ``Mapping``
    keeps callers flexible while still type-narrowing in pyright strict mode.
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
