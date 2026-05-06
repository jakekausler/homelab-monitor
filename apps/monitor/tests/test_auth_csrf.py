"""Tests for kernel/auth/csrf.py — CSRF token generation and verification."""

from __future__ import annotations

from homelab_monitor.kernel.auth.csrf import make_csrf_token, verify_csrf_token


def test_make_csrf_token_returns_32_char_hex() -> None:
    """make_csrf_token returns 32-character hex string."""
    token = make_csrf_token()
    assert len(token) == 32  # noqa: PLR2004
    assert all(c in "0123456789abcdef" for c in token)


def test_make_csrf_token_distinct_calls() -> None:
    """Two consecutive make_csrf_token calls return different values."""
    token1 = make_csrf_token()
    token2 = make_csrf_token()
    assert token1 != token2


def test_verify_csrf_token_matches_identical() -> None:
    """verify_csrf_token returns True for identical tokens."""
    token = make_csrf_token()
    assert verify_csrf_token(token, token)


def test_verify_csrf_token_rejects_mismatched() -> None:
    """verify_csrf_token returns False for mismatched tokens."""
    token1 = make_csrf_token()
    token2 = make_csrf_token()
    assert not verify_csrf_token(token1, token2)


def test_verify_csrf_token_empty_provided() -> None:
    """verify_csrf_token returns False for empty provided token."""
    token = make_csrf_token()
    assert not verify_csrf_token("", token)


def test_verify_csrf_token_empty_stored() -> None:
    """verify_csrf_token returns False for empty stored token."""
    token = make_csrf_token()
    assert not verify_csrf_token(token, "")


def test_verify_csrf_token_both_empty() -> None:
    """verify_csrf_token returns False when both empty (defensive: empty is never valid)."""
    assert not verify_csrf_token("", "")
