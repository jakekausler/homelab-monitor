"""Tests for ``kernel.db.ids.uuid7``."""

from __future__ import annotations

import uuid

from homelab_monitor.kernel.db.ids import uuid7


def test_uuid7_returns_valid_uuid_string() -> None:
    """Result parses as a UUID."""
    out = uuid7()
    assert isinstance(out, str)
    parsed = uuid.UUID(out)
    assert parsed.version == 7  # noqa: PLR2004


def test_uuid7_two_calls_yield_distinct_strings() -> None:
    """Each call returns a fresh UUID (random tail bits make collision negligible)."""
    assert uuid7() != uuid7()
