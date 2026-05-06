"""Tests for kernel/auth/passwords.py — bcrypt hash/verify."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.auth.passwords import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    verify_password,
)


def test_hash_verify_round_trip() -> None:
    """hash_password and verify_password round-trip successfully."""
    plaintext = "mysecretpassword123"
    hashed = hash_password(plaintext, cost=4)
    assert verify_password(plaintext, hashed)


def test_verify_wrong_password() -> None:
    """verify_password returns False for wrong plaintext."""
    plaintext = "mysecretpassword123"
    hashed = hash_password(plaintext, cost=4)
    assert not verify_password("wrongpassword", hashed)


def test_min_length_accepted() -> None:
    """Passwords of exactly MIN_PASSWORD_LENGTH are accepted."""
    plaintext = "a" * MIN_PASSWORD_LENGTH
    hashed = hash_password(plaintext, cost=4)
    assert verify_password(plaintext, hashed)


def test_below_min_length_rejected() -> None:
    """Passwords shorter than MIN_PASSWORD_LENGTH raise ValueError."""
    short = "a" * (MIN_PASSWORD_LENGTH - 1)
    with pytest.raises(ValueError):
        hash_password(short, cost=4)


def test_empty_password_rejected() -> None:
    """Empty password raises ValueError."""
    with pytest.raises(ValueError):
        hash_password("", cost=4)


def test_malformed_hash_returns_false() -> None:
    """verify_password returns False for malformed stored hash (no raise)."""
    malformed = "not-a-valid-bcrypt-hash"
    result = verify_password("password", malformed)
    assert result is False


def test_cost_4_vs_cost_12_different_hashes() -> None:
    """Different cost factors produce different hash formats."""
    plaintext = "testpassword123"
    hash4 = hash_password(plaintext, cost=4)
    hash12 = hash_password(plaintext, cost=12)
    # Different cost means different hash (salt is random anyway, but cost is in format)
    assert hash4 != hash12
    # Both should verify correctly
    assert verify_password(plaintext, hash4)
    assert verify_password(plaintext, hash12)


def test_same_plaintext_different_hashes() -> None:
    """Two hashes of the same plaintext are different (random salt)."""
    plaintext = "testpassword123"
    hash1 = hash_password(plaintext, cost=4)
    hash2 = hash_password(plaintext, cost=4)
    assert hash1 != hash2
    # Both should still verify
    assert verify_password(plaintext, hash1)
    assert verify_password(plaintext, hash2)
