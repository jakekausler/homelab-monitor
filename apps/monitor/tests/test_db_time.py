"""Tests for ``kernel.db.time.utc_now_iso``."""

from __future__ import annotations

from datetime import datetime, timedelta

from homelab_monitor.kernel.db.time import utc_now_iso


def test_utc_now_iso_returns_string() -> None:
    """Returned value is a non-empty string."""
    out = utc_now_iso()
    assert isinstance(out, str)
    assert out


def test_utc_now_iso_is_iso_8601_utc() -> None:
    """Value parses as ISO-8601 and carries a UTC offset."""
    out = utc_now_iso()
    parsed = datetime.fromisoformat(out)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
