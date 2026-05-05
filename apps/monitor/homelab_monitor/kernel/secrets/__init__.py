"""Encrypted secrets store: AES-GCM AEAD with HKDF per-row key derivation."""

from __future__ import annotations

from homelab_monitor.kernel.secrets.crypto import decrypt, derive_key, encrypt
from homelab_monitor.kernel.secrets.errors import (
    MasterKeyError,
    SecretIntegrityError,
    SecretNotFoundError,
)
from homelab_monitor.kernel.secrets.master_key import (
    load_master_key,
    master_key_fingerprint,
)
from homelab_monitor.kernel.secrets.repository import (
    AsyncSecretsRepository,
    SecretMeta,
)
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver

__all__ = [
    "AsyncSecretsRepository",
    "MasterKeyError",
    "SecretIntegrityError",
    "SecretMeta",
    "SecretNotFoundError",
    "SyncSecretsResolver",
    "decrypt",
    "derive_key",
    "encrypt",
    "load_master_key",
    "master_key_fingerprint",
]
