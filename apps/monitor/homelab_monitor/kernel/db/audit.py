"""Audit-log helper: every state-changing operation routes through this."""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

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
    await repo.execute(
        AUDIT_INSERT,
        {
            "id": uuid7(),
            "who": who,
            "what": what,
            "when": utc_now_iso(),
            "before_json": json.dumps(before) if before is not None else None,
            "after_json": json.dumps(after) if after is not None else None,
            "ip": ip,
        },
    )


AUDIT_INSERT = text(
    'INSERT INTO audit_log (id, who, what, "when", before_json, after_json, ip) '
    "VALUES (:id, :who, :what, :when, :before_json, :after_json, :ip)"
)


async def insert_audit(  # noqa: PLR0913
    conn: AsyncConnection,
    *,
    who: str,
    what: str,
    before: Mapping[str, object] | None = None,
    after: Mapping[str, object] | None = None,
    ip: str | None = None,
    when: str | None = None,
) -> None:
    """Write a row to ``audit_log`` against an EXISTING transaction's connection.

    State-changing kernel operations MUST keep their data write and audit row
    in the SAME transaction for atomicity. This helper accepts an existing
    ``AsyncConnection`` (the one yielded by ``SqliteRepository.transaction()``)
    and issues the audit INSERT against it — NOT a fresh connection.

    Args:
        conn: The async connection from an active ``repo.transaction()`` block.
        who: Actor identifier ("scheduler" for system, "operator" for manual,
            or a username when STAGE-001-010 supplies authenticated users).
        what: Event name in ``<entity>.<verb>_<state>`` form
            (e.g., ``"collector.quarantine_entered"``).
        before: Snapshot of state before the change (or ``None``).
        after: Snapshot of state after the change (or ``None``).
        ip: Originating IP (``None`` for system-initiated).
        when: ISO-8601 UTC timestamp (defaults to ``utc_now_iso()``).
    """
    await conn.execute(
        AUDIT_INSERT,
        {
            "id": uuid7(),
            "who": who,
            "what": what,
            "when": when if when is not None else utc_now_iso(),
            "before_json": json.dumps(before) if before is not None else None,
            "after_json": json.dumps(after) if after is not None else None,
            "ip": ip,
        },
    )


__all__ = ["AUDIT_INSERT", "audit_write", "insert_audit"]
