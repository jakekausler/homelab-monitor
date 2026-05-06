"""Session cookie generation and verification using HMAC-SHA256."""

from __future__ import annotations

import hmac
import uuid
from hashlib import sha256

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SESSION_HMAC_INFO = b"homelab.session.v1.hmac"
# 16 bytes (128 bits) is sufficient against existential forgery for the
# session-cookie threat model: an attacker who could compute or guess a
# valid HMAC on a chosen session_id has already broken HMAC-SHA256, which
# we treat as out of scope. Truncation is permitted by RFC 2104 and FIPS
# 198. The 32-hex-char suffix keeps cookie length manageable for browsers.
# Increase to 32 bytes only if the threat model expands to include
# offline cryptanalysis budgets larger than 2^64 operations.
SESSION_HMAC_LEN_BYTES = 16  # 16 bytes -> 32 hex chars
SESSION_ID_LEN = 32  # uuid4().hex
COOKIE_VALUE_LEN = SESSION_ID_LEN + 1 + (SESSION_HMAC_LEN_BYTES * 2)  # 65


def _derive_hmac_key(master_key: bytes) -> bytes:
    """HKDF-SHA256 derive a 32-byte HMAC subkey from the master key."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=SESSION_HMAC_INFO,
    )
    return hkdf.derive(master_key)


def make_session_id() -> str:
    """Generate a fresh session id (uuid4 hex, 32 chars)."""
    return uuid.uuid4().hex


def make_session_cookie_value(session_id: str, master_key: bytes) -> str:
    """Build the cookie value `<session_id>.<hmac_hex_truncated_16_bytes>`."""
    key = _derive_hmac_key(master_key)
    full = hmac.new(key, session_id.encode("ascii"), sha256).digest()
    truncated = full[:SESSION_HMAC_LEN_BYTES].hex()
    return f"{session_id}.{truncated}"


def verify_session_cookie_value(value: str, master_key: bytes) -> str | None:
    """Validate cookie HMAC and return session_id, or None on tamper / malform.

    NEVER raises. All malformed inputs (wrong length, wrong shape, bad HMAC)
    return None — the caller treats this as "no session" without leaking
    detail in logs.
    """
    if not isinstance(value, str) or len(value) != COOKIE_VALUE_LEN:  # pyright: ignore[reportUnnecessaryIsInstance]
        return None
    sep = value.find(".")
    if sep != SESSION_ID_LEN:
        return None
    session_id = value[:sep]
    provided_hmac_hex = value[sep + 1 :]
    if len(provided_hmac_hex) != SESSION_HMAC_LEN_BYTES * 2:
        return None
    expected = make_session_cookie_value(session_id, master_key)
    if not hmac.compare_digest(value, expected):
        return None
    return session_id
