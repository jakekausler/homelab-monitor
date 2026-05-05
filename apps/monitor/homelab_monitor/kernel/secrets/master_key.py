"""Master key bootstrap: env var → file → fail.

Both inputs are base64-encoded (single decode path; one error mode). The
decoded key MUST be exactly 32 bytes. ``master_key_fingerprint`` produces an
HMAC-based identifier used for rotation detection and operator display
without leaking the key bytes.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
from hashlib import sha256
from pathlib import Path

from homelab_monitor.kernel.secrets.errors import MasterKeyError

ENV_VAR = "HOMELAB_MONITOR_MASTER_KEY"
"""Env var consulted first."""

DEFAULT_KEY_FILE = "/run/secrets/master-key"
"""Fallback file consulted second. Both env and file are base64-encoded."""

EXPECTED_KEY_LEN = 32
"""Decoded master key length: 32 bytes for AES-256."""

FINGERPRINT_INFO = b"homelab-monitor/master-key/fingerprint"
"""Domain separator for the fingerprint HMAC."""


def _decode_b64(data: str, *, source: str) -> bytes:
    """Strict base64 decode; raise :class:`MasterKeyError` on any failure.

    ``source`` is "env" or "file" — included in the error message for clarity.
    """
    try:
        decoded = base64.b64decode(data.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MasterKeyError(f"master key from {source} is not valid base64") from exc
    if len(decoded) != EXPECTED_KEY_LEN:
        raise MasterKeyError(
            f"master key from {source} has length {len(decoded)}; expected {EXPECTED_KEY_LEN}"
        )
    return decoded


def load_master_key(*, file_path: str | None = None) -> bytes:
    """Load the master key. Env first, then file. Refuses to start without one.

    ``file_path`` overrides :data:`DEFAULT_KEY_FILE` (used by tests).

    Raises :class:`MasterKeyError` if neither source is available, the source
    is not valid base64, or the decoded key is not exactly 32 bytes.
    """
    raw_env = os.environ.get(ENV_VAR)
    if raw_env is not None and raw_env.strip() != "":
        return _decode_b64(raw_env, source="env")

    path = Path(file_path) if file_path is not None else Path(DEFAULT_KEY_FILE)
    if path.exists():
        contents = path.read_text(encoding="utf-8")
        return _decode_b64(contents, source="file")

    raise MasterKeyError(
        f"no master key: set {ENV_VAR} or place a base64-encoded 32-byte key at {path}"
    )


def master_key_fingerprint(key: bytes) -> str:
    """Return an HMAC-SHA256 fingerprint of the master key.

    Stable across calls for the same key; different for different keys; reveals
    nothing about the key bytes. Returns a lowercase hex string of the full
    32-byte HMAC digest.
    """
    if len(key) != EXPECTED_KEY_LEN:
        raise MasterKeyError(f"fingerprint requires a {EXPECTED_KEY_LEN}-byte key; got {len(key)}")
    return hmac.new(key, FINGERPRINT_INFO, sha256).hexdigest()
