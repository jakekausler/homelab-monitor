"""Kernel DB layer: async SQLite engine, repository facade, migrations, audit."""

from __future__ import annotations

from homelab_monitor.kernel.db.audit import audit_write
from homelab_monitor.kernel.db.engine import (
    dispose_engine,
    get_database_url,
    get_engine,
)
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.migrations import (
    MigrationsPendingError,
    check_pending_migrations,
    run_migrations,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso

__all__ = [
    "MigrationsPendingError",
    "SqliteRepository",
    "audit_write",
    "check_pending_migrations",
    "dispose_engine",
    "get_database_url",
    "get_engine",
    "run_migrations",
    "utc_now_iso",
    "uuid7",
]
