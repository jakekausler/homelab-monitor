"""API token scopes and scope serialization."""

from __future__ import annotations

from enum import StrEnum


class Scope(StrEnum):
    """Built-in API token scopes. Adding new scopes is an additive change."""

    HEARTBEAT_WRITE = "heartbeat:write"
    ALERTS_INGEST_WRITE = "alerts:ingest:write"
    READ_STATUS = "read:status"
    ADMIN_BACKUP_WRITE = "admin:backup:write"


def parse_scopes(stored: str) -> set[Scope]:
    """Parse a comma-separated stored scopes string into a set of Scope values.

    Empty string → empty set. Unknown scope strings raise ValueError. Whitespace
    around individual entries is stripped.
    """
    if not stored.strip():
        return set()
    out: set[Scope] = set()
    for raw in stored.split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            out.add(Scope(token))
        except ValueError as exc:
            msg = f"unknown scope: {token!r}"
            raise ValueError(msg) from exc
    return out


def serialize_scopes(scopes: set[Scope]) -> str:
    """Serialize a set of Scope values to a deterministic comma-separated string."""
    return ",".join(sorted(s.value for s in scopes))
