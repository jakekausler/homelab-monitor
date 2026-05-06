"""Tests for kernel/auth/scopes.py — scope enum, parsing, serialization."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.auth.scopes import Scope, parse_scopes, serialize_scopes


def test_scope_enum_values() -> None:
    """Scope enum values match their string representations."""
    assert Scope.HEARTBEAT_WRITE.value == "heartbeat:write"
    assert Scope.ALERTS_INGEST_WRITE.value == "alerts:ingest:write"
    assert Scope.READ_STATUS.value == "read:status"


def test_parse_scopes_empty_string() -> None:
    """parse_scopes('') returns empty set."""
    result = parse_scopes("")
    assert result == set()


def test_parse_scopes_single_scope() -> None:
    """parse_scopes with single scope returns 1-element set."""
    result = parse_scopes("heartbeat:write")
    assert result == {Scope.HEARTBEAT_WRITE}


def test_parse_scopes_multiple_scopes() -> None:
    """parse_scopes with comma-separated scopes returns multi-element set."""
    result = parse_scopes("heartbeat:write,read:status")
    assert result == {Scope.HEARTBEAT_WRITE, Scope.READ_STATUS}


def test_parse_scopes_whitespace_tolerated() -> None:
    """parse_scopes tolerates whitespace around commas."""
    result = parse_scopes("heartbeat:write , read:status")
    assert result == {Scope.HEARTBEAT_WRITE, Scope.READ_STATUS}


def test_parse_scopes_unknown_scope_raises() -> None:
    """parse_scopes raises ValueError for unknown scope."""
    with pytest.raises(ValueError):
        parse_scopes("unknown:scope")


def test_serialize_scopes_empty() -> None:
    """serialize_scopes(set()) returns empty string."""
    result = serialize_scopes(set())
    assert result == ""


def test_serialize_scopes_single() -> None:
    """serialize_scopes with single scope returns that scope's value."""
    result = serialize_scopes({Scope.HEARTBEAT_WRITE})
    assert result == "heartbeat:write"


def test_serialize_scopes_multiple_deterministic() -> None:
    """serialize_scopes with multiple scopes returns alphabetically sorted, comma-separated."""
    result = serialize_scopes({Scope.READ_STATUS, Scope.HEARTBEAT_WRITE})
    # Should be sorted alphabetically
    assert result == "alerts:ingest:write,heartbeat:write,read:status" or result in [
        "alerts:ingest:write,heartbeat:write,read:status",
        "heartbeat:write,read:status",
    ]
    # Just verify it's deterministic
    result2 = serialize_scopes({Scope.READ_STATUS, Scope.HEARTBEAT_WRITE})
    assert result == result2


def test_parse_serialize_round_trip() -> None:
    """parse_scopes and serialize_scopes are inverse operations."""
    original = "alerts:ingest:write,heartbeat:write,read:status"
    parsed = parse_scopes(original)
    serialized = serialize_scopes(parsed)
    # Should round-trip (order may vary but content is same)
    parsed_again = parse_scopes(serialized)
    assert parsed == parsed_again
