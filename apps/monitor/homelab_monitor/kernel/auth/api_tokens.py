"""API token generation, hashing, and verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

DEFAULT_ENV_PREFIX = "prod"
TOKEN_PREFIX_ENV = "HOMELAB_MONITOR_TOKEN_PREFIX"
TOKEN_BYTE_LEN = 30  # 30 raw bytes -> 40 base64url chars


def _current_env_prefix() -> str:
    """Resolve the env-prefix segment of generated tokens."""
    return os.environ.get(TOKEN_PREFIX_ENV, DEFAULT_ENV_PREFIX)


def make_api_token(prefix: str | None = None) -> tuple[str, str]:
    """Generate a fresh API token and its SHA-256 hex hash.

    Returns:
        (plaintext_token, sha256_hex). The plaintext is shown to the operator
        ONCE (CLI output) and never persisted; only the hash hits the DB.
    """
    env_prefix = prefix if prefix is not None else _current_env_prefix()
    raw = secrets.token_bytes(TOKEN_BYTE_LEN)
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    plaintext = f"homelab_{env_prefix}_{body}"
    sha = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return plaintext, sha


def hash_token(plaintext: str) -> str:
    """SHA-256 hex of a token plaintext (used at lookup time)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def verify_api_token(plaintext: str, stored_hash: str) -> bool:
    """Constant-time check that hash(plaintext) == stored_hash."""
    return hmac.compare_digest(hash_token(plaintext), stored_hash)


def parse_token_prefix(plaintext: str) -> str | None:
    """Extract the env-prefix from a token. Returns None on malformed input.

    Reserved for cross-environment misconfiguration detection (e.g., logging a
    warning when a `dev`-prefixed token appears in a `prod`-prefixed deployment).
    NOT currently called from the request hot path — `_resolve_token` only
    consults the SHA-256 hash. The function exists so future middleware
    additions (e.g., a per-token environment-affinity check) can plug in
    without re-deriving the parsing logic.
    """
    if not plaintext.startswith("homelab_"):
        return None
    rest = plaintext[len("homelab_") :]
    sep = rest.find("_")
    if sep <= 0:
        return None
    return rest[:sep]
