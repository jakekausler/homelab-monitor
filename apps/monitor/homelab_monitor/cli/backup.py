"""``hm backup`` subcommand: run / list / retention."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from homelab_monitor.kernel.backup.service import BackupService
from homelab_monitor.kernel.db.repository import SqliteRepository


# argparse exposes no public type for sub-parsers; using the private alias is the
# pyright-recommended workaround. https://github.com/python/typeshed/issues/2569
def add_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Wire ``hm backup`` and its sub-subcommands."""
    backup = subparsers.add_parser("backup", help="Run / list / prune SQLite + VM backups")
    sub = backup.add_subparsers(dest="backup_cmd")

    sub.add_parser("run", help="Run one full backup")
    sub.add_parser("list", help="List existing backups (JSON)")

    p_ret = sub.add_parser("retention", help="Apply retention: keep the N newest")
    p_ret.add_argument("--keep", type=int, required=True, help="Number of newest to retain")

    backup.set_defaults(func=_handle)


async def _close_service_and_engine(service: BackupService, engine: AsyncEngine) -> None:
    """Helper for sync _cmd_list cleanup (since it doesn't await elsewhere)."""
    await service.aclose()
    await engine.dispose()


def _handle(args: argparse.Namespace) -> int:
    """Dispatch ``hm backup <cmd>``."""
    sub = getattr(args, "backup_cmd", None)
    if sub is None:
        print("usage: hm backup {run,list,retention}", file=sys.stderr)
        return 2
    if sub == "run":
        return asyncio.run(_cmd_run())
    if sub == "list":
        return _cmd_list()
    if sub == "retention":
        return asyncio.run(_cmd_retention(int(args.keep)))
    print(f"unknown subcommand: {sub}", file=sys.stderr)  # pragma: no cover
    return 2  # pragma: no cover


def _build_service_sync() -> tuple[BackupService, AsyncEngine]:
    """Construct a BackupService from env vars (no app.state)."""
    db_url = os.environ.get(
        "HOMELAB_MONITOR_DB_URL", "sqlite+aiosqlite:////data/homelab-monitor.db"
    )
    prefix = "sqlite+aiosqlite:///"
    db_path_str = db_url[len(prefix) :] if db_url.startswith(prefix) else db_url
    db_path = Path(db_path_str)
    vm_url = os.environ.get("HOMELAB_MONITOR_VM_URL", "http://victoriametrics:8428")
    vm_data_dir = Path(os.environ.get("HOMELAB_MONITOR_VM_DATA_DIR", "/var/vm-data"))
    backup_root = Path(
        os.environ.get("HOMELAB_MONITOR_BACKUP_ROOT", "/storage/backup/homelab-monitor")
    )
    # CLI gets its own short-lived AsyncClient; not the app.state one.
    vm_timeout_s = float(os.environ.get("HOMELAB_MONITOR_VM_TIMEOUT_S", "30.0"))
    client = httpx.AsyncClient(timeout=httpx.Timeout(vm_timeout_s, connect=5.0))

    engine = create_async_engine(db_url)
    repo = SqliteRepository(engine)

    service = BackupService(
        db=repo,
        db_path=db_path,
        vm_url=vm_url,
        vm_data_dir=vm_data_dir,
        backup_root=backup_root,
        http_client=client,
    )
    return service, engine


async def _cmd_run() -> int:
    """``hm backup run``: execute a full backup, print summary."""
    service, engine = _build_service_sync()
    try:
        result = await service.run_backup(who=f"cli:{os.geteuid()}", ip=None)
    finally:
        await service.aclose()
        await engine.dispose()
    print(json.dumps(asdict(result), indent=2))
    return 1 if result.errors else 0


def _cmd_list() -> int:
    """``hm backup list``: list existing backups as JSON."""
    service, engine = _build_service_sync()
    try:
        listing = service.list_backups()
    finally:
        asyncio.run(_close_service_and_engine(service, engine))
    print(json.dumps(listing, indent=2))
    return 0


async def _cmd_retention(keep: int) -> int:
    """``hm backup retention --keep N``: apply retention to both components."""
    service, engine = _build_service_sync()
    try:
        deleted = await service.apply_retention(keep=keep, who=f"cli:{os.geteuid()}", ip=None)
    finally:
        await service.aclose()
        await engine.dispose()
    print(json.dumps(deleted, indent=2))
    return 0
