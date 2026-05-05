"""Exception types for the secrets subsystem."""

from __future__ import annotations


class SecretIntegrityError(Exception):
    """Raised when AES-GCM tag verification fails.

    Catches both tampered ciphertext and decryption with the wrong master key
    or wrong salt — same exception by design, to avoid leaking which of the
    three failed.
    """


class SecretNotFoundError(KeyError):
    """Raised when a secret name is not present in the store."""


class MasterKeyError(RuntimeError):
    """Raised when the master key cannot be loaded or is malformed.

    Reasons: env var and file both missing, base64 decode failed, decoded key
    is not exactly 32 bytes.
    """
