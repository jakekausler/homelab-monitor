"""Tests for kernel/auth/sessions.py — session ID generation, HMAC signing/verification."""

from __future__ import annotations

from homelab_monitor.kernel.auth.sessions import (
    make_session_cookie_value,
    make_session_id,
    verify_session_cookie_value,
)


def test_make_session_id_returns_32_char_hex() -> None:
    """make_session_id returns 32-character hex string."""
    sid = make_session_id()
    assert len(sid) == 32  # noqa: PLR2004
    assert all(c in "0123456789abcdef" for c in sid)


def test_make_session_id_distinct_calls() -> None:
    """Two consecutive make_session_id calls return different values."""
    sid1 = make_session_id()
    sid2 = make_session_id()
    assert sid1 != sid2


def test_make_session_cookie_value_shape() -> None:
    """make_session_cookie_value produces <32hex>.<32hex> format."""
    session_id = make_session_id()
    master_key = bytes(range(32))
    cookie_val = make_session_cookie_value(session_id, master_key)
    # Should be 32-char hex + dot + 32-char hex = 65 chars
    assert len(cookie_val) == 65  # noqa: PLR2004
    parts = cookie_val.split(".")
    assert len(parts) == 2  # noqa: PLR2004
    assert len(parts[0]) == 32  # noqa: PLR2004
    assert len(parts[1]) == 32  # noqa: PLR2004
    assert all(c in "0123456789abcdef" for c in parts[0])
    assert all(c in "0123456789abcdef" for c in parts[1])


def test_verify_session_cookie_value_round_trip() -> None:
    """Verify returns the original session_id on match."""
    session_id = make_session_id()
    master_key = bytes(range(32))
    cookie_val = make_session_cookie_value(session_id, master_key)
    result = verify_session_cookie_value(cookie_val, master_key)
    assert result == session_id


def test_verify_session_cookie_value_wrong_key() -> None:
    """Wrong key returns None."""
    session_id = make_session_id()
    master_key1 = bytes(range(32))
    master_key2 = bytes(reversed(range(32)))
    cookie_val = make_session_cookie_value(session_id, master_key1)
    result = verify_session_cookie_value(cookie_val, master_key2)
    assert result is None


def test_verify_session_cookie_value_tampered_hmac() -> None:
    """Tampered HMAC suffix returns None."""
    session_id = make_session_id()
    master_key = bytes(range(32))
    cookie_val = make_session_cookie_value(session_id, master_key)
    # Flip one character in the HMAC part (after the dot)
    parts = cookie_val.split(".")
    # Flip to a guaranteed-different hex char so the tamper is never a no-op.
    tampered_hmac = ("b" if parts[1][0] == "a" else "a") + parts[1][1:]
    tampered_val = f"{parts[0]}.{tampered_hmac}"
    result = verify_session_cookie_value(tampered_val, master_key)
    assert result is None


def test_verify_session_cookie_value_tampered_session_id() -> None:
    """Tampered session_id returns None."""
    session_id = make_session_id()
    master_key = bytes(range(32))
    cookie_val = make_session_cookie_value(session_id, master_key)
    # Flip one character in the session_id part (before the dot)
    parts = cookie_val.split(".")
    # Flip to a guaranteed-different hex char so the tamper is never a no-op.
    tampered_sid = ("b" if parts[0][0] == "a" else "a") + parts[0][1:]
    tampered_val = f"{tampered_sid}.{parts[1]}"
    result = verify_session_cookie_value(tampered_val, master_key)
    assert result is None


def test_verify_session_cookie_value_wrong_total_length() -> None:
    """Wrong total length returns None."""
    master_key = bytes(range(32))
    result = verify_session_cookie_value("tooshort", master_key)
    assert result is None


def test_verify_session_cookie_value_missing_dot() -> None:
    """Missing dot separator returns None."""
    master_key = bytes(range(32))
    no_dot = "a" * 65  # Valid length but no dot
    result = verify_session_cookie_value(no_dot, master_key)
    assert result is None


def test_verify_session_cookie_value_bytes_input() -> None:
    """Bytes input (not str) returns None."""
    master_key = bytes(range(32))
    result = verify_session_cookie_value(b"bytes_input", master_key)  # type: ignore[arg-type]
    assert result is None


def test_hkdf_determinism() -> None:
    """Same session_id + key produce same cookie value (deterministic HKDF)."""
    session_id = "a" * 32
    master_key = bytes(range(32))
    cookie1 = make_session_cookie_value(session_id, master_key)
    cookie2 = make_session_cookie_value(session_id, master_key)
    assert cookie1 == cookie2
