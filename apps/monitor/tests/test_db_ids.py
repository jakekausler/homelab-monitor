"""Tests for ``kernel.db.ids.uuid7``."""

from __future__ import annotations

import time
import uuid

from homelab_monitor.kernel.db.ids import uuid7


def test_uuid7_returns_valid_uuid_string() -> None:
    """Result parses as a UUID."""
    out = uuid7()
    assert isinstance(out, str)
    parsed = uuid.UUID(out)
    assert parsed.version == 7  # noqa: PLR2004


def test_uuid7_is_time_sortable() -> None:
    """A UUIDv7 generated later sorts strictly after one generated earlier."""
    earlier = uuid7()
    time.sleep(0.005)  # 5ms — UUIDv7 is millisecond-precise
    later = uuid7()
    assert earlier < later
