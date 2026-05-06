"""Password hashing and verification using bcrypt."""

from __future__ import annotations

import os

import bcrypt

MIN_PASSWORD_LENGTH = 12
# Default bcrypt cost factor. Configurable via HOMELAB_MONITOR_BCRYPT_COST;
# tests set this to 4 for speed. Production should remain at >= 12 (NIST
# guidance for 2026: cost 12-14 for interactive auth).
_BCRYPT_COST_ENV = "HOMELAB_MONITOR_BCRYPT_COST"


def _resolve_bcrypt_cost() -> int:
    raw = os.environ.get(_BCRYPT_COST_ENV)
    if raw is None or not raw.strip():
        return 12
    try:
        return max(4, min(20, int(raw)))
    except ValueError:
        return 12


DEFAULT_BCRYPT_COST = _resolve_bcrypt_cost()


def hash_password(plaintext: str, *, cost: int | None = None) -> str:
    """Bcrypt-hash a password; returns the printable hash string.

    Length validation (>= MIN_PASSWORD_LENGTH) is the ONLY content rule
    enforced server-side per locked stage decision (no complexity rules in v1).
    Raises ValueError if plaintext is shorter than the minimum.

    Cost defaults to DEFAULT_BCRYPT_COST (configured at module load from env).
    """
    if cost is None:
        cost = DEFAULT_BCRYPT_COST
    if len(plaintext) < MIN_PASSWORD_LENGTH:
        msg = f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        raise ValueError(msg)
    salt = bcrypt.gensalt(rounds=cost)
    return bcrypt.hashpw(plaintext.encode("utf-8"), salt).decode("utf-8")


def verify_password(plaintext: str, hash_str: str) -> bool:
    """Constant-time bcrypt verify. Returns False on any failure (no leak of cause)."""
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), hash_str.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def validate_password_length(plaintext: str) -> None:
    """Raise ValueError if plaintext is shorter than MIN_PASSWORD_LENGTH.

    Used by the CLI (`hm user create`, `hm user passwd`) to validate input
    BEFORE prompting for confirmation, so the user is told the rule up
    front. `hash_password` re-checks at hash time so an internal caller
    that bypasses the CLI cannot persist a too-short password.
    """
    if len(plaintext) < MIN_PASSWORD_LENGTH:
        msg = f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        raise ValueError(msg)
