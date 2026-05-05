"""Pure crypto primitives for the secrets store.

AES-GCM AEAD with HKDF-SHA256 per-row key derivation. All functions are pure;
state lives in the repository layer.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from homelab_monitor.kernel.secrets.errors import SecretIntegrityError

DERIVED_KEY_LEN = 32
"""HKDF output length: 32 bytes for AES-256-GCM."""

SALT_LEN = 16
"""HKDF salt length per row."""

NONCE_LEN = 12
"""AES-GCM nonce length per encryption (must be unique per (key, message))."""

HKDF_INFO_PREFIX = b"homelab-monitor/secrets/v1/"
"""Domain-separation prefix; concatenated with the row id for the HKDF info parameter."""


def _hkdf_info(row_id: str) -> bytes:
    """Build the HKDF info parameter from the row's UUIDv7 id.

    Using the row id (stable across renames) rather than the secret name keeps
    HKDF stable when a future feature renames a secret without re-encrypting.
    """
    return HKDF_INFO_PREFIX + row_id.encode("utf-8")


def derive_key(master: bytes, salt: bytes, row_id: str) -> bytes:
    """Derive a 32-byte AES-256 key from the master + per-row salt + row id.

    HKDF-SHA256 with project-specific info. Pure function — same inputs, same
    output.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=DERIVED_KEY_LEN,
        salt=salt,
        info=_hkdf_info(row_id),
    )
    return hkdf.derive(master)


def encrypt(master: bytes, plaintext: bytes, row_id: str) -> tuple[bytes, bytes]:
    """Encrypt ``plaintext`` for storage; return ``(salt, nonce_plus_ct)``.

    - Generates a fresh 16-byte salt via ``os.urandom`` (HKDF salt).
    - Generates a fresh 12-byte nonce via ``os.urandom`` (AES-GCM IV).
    - Derives the per-row key via :func:`derive_key`.
    - Encrypts under AES-GCM-256; tag is appended to the ciphertext by
      ``cryptography``'s API.

    Returns a tuple of ``(salt, nonce || ciphertext_with_tag)``. The caller is
    expected to base64-encode the second element for SQLite TEXT storage and
    persist the salt as a BLOB.
    """
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = derive_key(master, salt, row_id)
    aead = AESGCM(key)
    ct = aead.encrypt(nonce, plaintext, associated_data=None)
    return salt, nonce + ct


def decrypt(master: bytes, salt: bytes, nonce_plus_ct: bytes, row_id: str) -> bytes:
    """Decrypt the blob produced by :func:`encrypt`.

    Splits ``nonce_plus_ct`` into the leading 12-byte nonce + remaining
    ciphertext-with-tag, derives the per-row key, and verifies+decrypts under
    AES-GCM. Raises :class:`SecretIntegrityError` on any failure (tag mismatch,
    truncated input, wrong master key) — the same error class for every cause
    so callers can't distinguish them.
    """
    if len(nonce_plus_ct) < NONCE_LEN:
        raise SecretIntegrityError("ciphertext too short")
    nonce = nonce_plus_ct[:NONCE_LEN]
    ct = nonce_plus_ct[NONCE_LEN:]
    key = derive_key(master, salt, row_id)
    aead = AESGCM(key)
    try:
        return aead.decrypt(nonce, ct, associated_data=None)
    except InvalidTag as exc:
        raise SecretIntegrityError("AES-GCM tag verification failed") from exc
