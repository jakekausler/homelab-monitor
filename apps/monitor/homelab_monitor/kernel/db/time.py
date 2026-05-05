"""UTC timestamp helpers for SQLite TEXT columns."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    E.g. ``2026-05-05T13:45:18.123456+00:00``.

    All internal timestamps are stored as ISO-8601 UTC strings per spec §16
    and STAGE-001-004 design decision.
    """
    return datetime.now(tz=UTC).isoformat()
