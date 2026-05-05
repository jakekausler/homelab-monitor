"""``hm migrate`` subcommand: apply / inspect Alembic migrations."""

from __future__ import annotations

import argparse

from homelab_monitor.kernel.db.engine import get_database_url
from homelab_monitor.kernel.db.migrations import (
    alembic_current_revision,
    alembic_head_revision,
    alembic_history,
    alembic_upgrade_head,
)


# argparse exposes no public type for sub-parsers; using the private alias is the
# pyright-recommended workaround. https://github.com/python/typeshed/issues/2569
def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    """Wire ``migrate`` and its sub-subcommands onto an argparse subparsers object."""
    migrate = subparsers.add_parser("migrate", help="Apply or inspect Alembic migrations")
    migrate_sub = migrate.add_subparsers(dest="migrate_cmd")
    migrate_sub.add_parser("status", help="Show current and head revisions")
    migrate_sub.add_parser("history", help="List all known revisions")
    migrate.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    """Dispatch ``hm migrate`` / ``hm migrate status`` / ``hm migrate history``."""
    url = get_database_url()
    sub = getattr(args, "migrate_cmd", None)
    if sub == "status":
        return _cmd_status(url)
    if sub == "history":
        return _cmd_history(url)
    return _cmd_upgrade(url)


def _cmd_upgrade(url: str) -> int:
    """``hm migrate``: run ``alembic upgrade head``."""
    alembic_upgrade_head(url)
    print("Migrations applied: at head.")
    return 0


def _cmd_status(url: str) -> int:
    """``hm migrate status``: print current vs head revision."""
    current = alembic_current_revision(url)
    head = alembic_head_revision(url)
    print(f"current: {current or '<empty>'}")
    print(f"head:    {head or '<empty>'}")
    if current == head:
        print("status:  up to date")
    else:
        print("status:  pending migrations")
    return 0


def _cmd_history(url: str) -> int:
    """``hm migrate history``: print revisions newest -> oldest."""
    for line in alembic_history(url):
        print(line)
    return 0
