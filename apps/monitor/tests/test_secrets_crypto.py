"""Tests for the AES-GCM + HKDF crypto primitives."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.secrets.crypto import (
    NONCE_LEN,
    SALT_LEN,
    decrypt,
    derive_key,
    encrypt,
)
from homelab_monitor.kernel.secrets.errors import SecretIntegrityError

ROW_ID = "01952f00-0000-7000-8000-000000000001"
OTHER_ROW_ID = "01952f00-0000-7000-8000-000000000002"


def test_encrypt_decrypt_round_trip() -> None:
    """A value encrypted under master+row_id decrypts back to itself."""
    master = bytes(range(32))
    plaintext = b"hl-test-secret-roundtrip"
    salt, blob = encrypt(master, plaintext, ROW_ID)
    recovered = decrypt(master, salt, blob, ROW_ID)
    assert recovered == plaintext
    assert len(salt) == SALT_LEN
    assert len(blob) >= NONCE_LEN + 16  # nonce + 16-byte AES-GCM tag minimum


def test_tampered_ciphertext_raises() -> None:
    """Flipping a single byte in the AEAD payload raises SecretIntegrityError."""
    master = bytes(range(32))
    plaintext = b"important-token"
    salt, blob = encrypt(master, plaintext, ROW_ID)
    tampered = bytearray(blob)
    tampered[-1] ^= 0x01  # flip last byte (inside the tag)
    with pytest.raises(SecretIntegrityError):
        decrypt(master, salt, bytes(tampered), ROW_ID)


def test_wrong_master_raises_same_error() -> None:
    """Decrypting with the wrong master raises SecretIntegrityError (no info leak)."""
    master_a = bytes(range(32))
    master_b = bytes(range(1, 33))
    plaintext = b"important-token"
    salt, blob = encrypt(master_a, plaintext, ROW_ID)
    with pytest.raises(SecretIntegrityError):
        decrypt(master_b, salt, blob, ROW_ID)


def test_wrong_row_id_raises() -> None:
    """Decrypting under a different row_id (different HKDF info) fails."""
    master = bytes(range(32))
    plaintext = b"important-token"
    salt, blob = encrypt(master, plaintext, ROW_ID)
    with pytest.raises(SecretIntegrityError):
        decrypt(master, salt, blob, OTHER_ROW_ID)


def test_truncated_ciphertext_raises() -> None:
    """A blob shorter than the nonce length raises SecretIntegrityError."""
    master = bytes(range(32))
    salt = b"\x00" * SALT_LEN
    with pytest.raises(SecretIntegrityError):
        decrypt(master, salt, b"\x00" * (NONCE_LEN - 1), ROW_ID)


def test_nonce_uniqueness_across_two_encrypts() -> None:
    """Encrypting the same plaintext twice produces different blobs (fresh nonce + salt)."""
    master = bytes(range(32))
    pt = b"same-input"
    salt1, blob1 = encrypt(master, pt, ROW_ID)
    salt2, blob2 = encrypt(master, pt, ROW_ID)
    assert salt1 != salt2
    assert blob1 != blob2
    # Both still decrypt correctly to the original plaintext.
    assert decrypt(master, salt1, blob1, ROW_ID) == pt
    assert decrypt(master, salt2, blob2, ROW_ID) == pt


def test_derive_key_different_for_different_salts() -> None:
    """HKDF with different salts produces different keys."""
    master = bytes(range(32))
    k1 = derive_key(master, b"\x00" * SALT_LEN, ROW_ID)
    k2 = derive_key(master, b"\x01" * SALT_LEN, ROW_ID)
    assert k1 != k2
    assert len(k1) == 32  # noqa: PLR2004
    assert len(k2) == 32  # noqa: PLR2004


def test_derive_key_different_for_different_row_ids() -> None:
    """HKDF info parameter (row id) drives unique derivations."""
    master = bytes(range(32))
    salt = b"\x42" * SALT_LEN
    k1 = derive_key(master, salt, ROW_ID)
    k2 = derive_key(master, salt, OTHER_ROW_ID)
    assert k1 != k2
