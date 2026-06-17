"""Shared CLI bootstrap helpers."""

from __future__ import annotations

from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.secrets.master_key import load_master_key
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository


async def build_secrets_repo() -> AsyncSecretsRepository:
    """Construct an :class:`AsyncSecretsRepository` from env config.

    Mirrors the original ``cli/secrets.py._build_repo`` body verbatim. Raises
    :class:`~homelab_monitor.kernel.secrets.errors.MasterKeyError` if no master
    key is configured (callers map this to ``error: ...`` + exit 1).
    """
    master = load_master_key()
    engine = get_engine()
    return AsyncSecretsRepository(SqliteRepository(engine), master)
