"""CSRF token generation and verification."""

from __future__ import annotations

import hmac
import uuid


def make_csrf_token() -> str:
    """Generate a fresh CSRF token (uuid4 hex, 32 chars)."""
    return uuid.uuid4().hex


def verify_csrf_token(provided: str, expected: str) -> bool:
    """Constant-time compare of provided header value to the expected session value."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)
