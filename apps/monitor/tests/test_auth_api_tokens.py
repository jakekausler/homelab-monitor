"""Tests for kernel/auth/api_tokens.py — API token generation, hashing, parsing."""

from __future__ import annotations

import hashlib

import pytest

from homelab_monitor.kernel.auth.api_tokens import (
    make_api_token,
    parse_token_prefix,
    verify_api_token,
)


def test_make_api_token_default_prefix() -> None:
    """make_api_token with default prefix starts with 'homelab_prod_'."""
    plaintext, hash_val = make_api_token()
    assert plaintext.startswith("homelab_prod_")
    # Hash should be 64-char hex (SHA-256)
    assert len(hash_val) == 64  # noqa: PLR2004
    assert all(c in "0123456789abcdef" for c in hash_val)


def test_make_api_token_custom_prefix_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """make_api_token with HOMELAB_MONITOR_TOKEN_PREFIX env starts with custom prefix."""
    monkeypatch.setenv("HOMELAB_MONITOR_TOKEN_PREFIX", "dev")
    plaintext, _ = make_api_token()
    assert plaintext.startswith("homelab_dev_")


def test_make_api_token_hash_matches_sha256() -> None:
    """Returned hash matches sha256 of plaintext."""
    plaintext, hash_val = make_api_token()
    expected_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    assert hash_val == expected_hash


def test_make_api_token_distinct_calls() -> None:
    """Two consecutive make_api_token calls produce different plaintexts."""
    plaintext1, _ = make_api_token()
    plaintext2, _ = make_api_token()
    assert plaintext1 != plaintext2


def test_verify_api_token_matches() -> None:
    """verify_api_token returns True for matching plaintext/hash."""
    plaintext, hash_val = make_api_token()
    assert verify_api_token(plaintext, hash_val)


def test_verify_api_token_mismatched() -> None:
    """verify_api_token returns False for mismatched plaintext/hash."""
    plaintext1, _ = make_api_token()
    _, hash_val2 = make_api_token()
    assert not verify_api_token(plaintext1, hash_val2)


def test_parse_token_prefix_extraction() -> None:
    """parse_token_prefix extracts the env prefix from a token."""
    plaintext = "homelab_prod_abc123def456"
    prefix = parse_token_prefix(plaintext)
    assert prefix == "prod"


def test_parse_token_prefix_malformed() -> None:
    """parse_token_prefix returns None for malformed token (no underscore)."""
    malformed = "nounderscore123"
    result = parse_token_prefix(malformed)
    assert result is None


def test_parse_token_prefix_empty() -> None:
    """parse_token_prefix returns None for empty string."""
    result = parse_token_prefix("")
    assert result is None
