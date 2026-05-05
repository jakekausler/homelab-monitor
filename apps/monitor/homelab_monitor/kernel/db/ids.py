"""UUIDv7 helper for time-sortable primary keys."""

from __future__ import annotations

import uuid_utils


def uuid7() -> str:
    """Return a UUIDv7 as a lowercase canonical string.

    UUIDv7 carries millisecond-precision creation time in its high bits, which
    gives us natural creation-time ordering on primary keys (helpful for the
    audit log and runbook runs). Wrapped here so the rest of the kernel only
    depends on a stable string-returning function.
    """
    return str(uuid_utils.uuid7())
