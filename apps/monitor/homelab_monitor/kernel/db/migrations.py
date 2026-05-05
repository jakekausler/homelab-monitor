"""Async wrapper around Alembic for boot-time migrations.

Locked decisions (STAGE-001-004 design):

- ``HOMELAB_MONITOR_AUTO_MIGRATE`` env var (default ``"true"``).
  - On boot: if Alembic head == DB current revision, proceed.
  - If pending and env truthy, run ``upgrade head`` automatically.
  - If pending and env ``"false"``, raise :class:`MigrationsPendingError`.
- Programmatic Alembic config — ``script_location`` resolved relative to the
  package, so tests don't depend on cwd.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command

# Path to ``apps/monitor/alembic`` regardless of cwd. ``__file__`` points to
# ``apps/monitor/homelab_monitor/kernel/db/migrations.py``; four ``.parent`` hops
# land at ``apps/monitor`` and one more segment gives us the alembic dir.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
ALEMBIC_DIR = _PACKAGE_ROOT / "alembic"


class MigrationsPendingError(RuntimeError):
    """Raised when pending migrations exist and auto-migrate is disabled."""


def _build_config(database_url: str) -> Config:
    """Build a programmatic :class:`alembic.config.Config`."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _auto_migrate_enabled() -> bool:
    """Return ``True`` if ``HOMELAB_MONITOR_AUTO_MIGRATE`` is truthy (default true)."""
    raw = os.environ.get("HOMELAB_MONITOR_AUTO_MIGRATE", "true")
    return raw.strip().lower() in {"true", "1", "yes", "on"}


async def check_pending_migrations(engine: AsyncEngine) -> list[str]:
    """Return the list of pending revision IDs (``[]`` when at head).

    A fresh DB returns every revision in the script directory.
    """
    cfg = _build_config(str(engine.url))
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()

    def _read_current(sync_conn: Any) -> str | None:  # noqa: ANN401
        ctx = MigrationContext.configure(sync_conn)
        return ctx.get_current_revision()

    async with engine.connect() as conn:
        current = await conn.run_sync(_read_current)

    if current == head:
        return []
    if current is None:
        # Empty DB: every revision in the script directory is pending.
        return [rev.revision for rev in script.walk_revisions(base="base", head="head")]
    pending: list[str] = []
    for rev in script.walk_revisions(base="base", head="head"):
        if rev.revision == current:
            break
        pending.append(rev.revision)
    return pending


async def run_migrations(engine: AsyncEngine) -> None:
    """Apply pending migrations. Honours the auto-migrate env var.

    - No pending → no-op.
    - Pending and env truthy → run ``alembic upgrade head``.
    - Pending and env falsey → raise :class:`MigrationsPendingError`.

    Concurrency: assumes single-writer (the homelab-monitor service runs as a
    single container per host; deployment is not HA). SQLite has no advisory
    locks, but ``PRAGMA busy_timeout=5000`` (set by the engine factory) gives
    Alembic 5s of retry if a manual ``hm migrate`` runs concurrently with boot.
    Two simultaneous ``upgrade head`` calls on a fresh DB will race; the loser
    will see ``OperationalError: table alembic_version already exists``.
    """
    pending = await check_pending_migrations(engine)
    if not pending:
        return
    if not _auto_migrate_enabled():
        raise MigrationsPendingError(
            f"{len(pending)} pending migration(s); set HOMELAB_MONITOR_AUTO_MIGRATE=true "
            f"or run `hm migrate` manually."
        )
    cfg = _build_config(str(engine.url))
    # ``command.upgrade`` is sync; Alembic spawns its own connections via the URL.
    command.upgrade(cfg, "head")


def alembic_upgrade_head(database_url: str) -> None:
    """CLI helper: run ``alembic upgrade head`` synchronously."""
    cfg = _build_config(database_url)
    command.upgrade(cfg, "head")


def alembic_current_revision(database_url: str) -> str | None:
    """Return the current revision recorded in the DB, or ``None`` if empty."""
    # ``alembic_current_revision`` is a sync helper used by the CLI; convert the
    # async URL prefix to its sync counterpart. Non-aiosqlite URLs pass through
    # untouched.
    sync_url = (
        database_url.replace("sqlite+aiosqlite://", "sqlite://", 1)
        if database_url.startswith("sqlite+aiosqlite://")
        else database_url
    )
    sync_engine = create_engine(sync_url, future=True)
    try:
        with sync_engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            return ctx.get_current_revision()
    finally:
        sync_engine.dispose()


def alembic_head_revision(database_url: str) -> str | None:
    """Return the head revision in the script directory."""
    cfg = _build_config(database_url)
    script = ScriptDirectory.from_config(cfg)
    return script.get_current_head()


def alembic_history(database_url: str) -> list[str]:
    """Return revisions newest -> oldest as ``"<rev> -> <down_rev>: <doc>"`` strings."""
    cfg = _build_config(database_url)
    script = ScriptDirectory.from_config(cfg)
    out: list[str] = []
    for rev in script.walk_revisions(base="base", head="head"):
        down = rev.down_revision if rev.down_revision is not None else "<base>"
        doc = rev.doc or ""
        out.append(f"{rev.revision} -> {down}: {doc}")
    return out
