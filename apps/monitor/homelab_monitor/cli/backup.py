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

from homelab_monitor.kernel.backup.service import BackupService


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
        return _cmd_retention(int(args.keep))
    print(f"unknown subcommand: {sub}", file=sys.stderr)  # pragma: no cover
    return 2  # pragma: no cover


def _build_service_sync() -> BackupService:
    """Construct a BackupService from env vars (no app.state)."""
    db_url = os.environ.get(
        "HOMELAB_MONITOR_DB_URL", "sqlite+aiosqlite:////data/homelab-monitor.db"
    )
    prefix = "sqlite+aiosqlite:///"
    db_path = Path(db_url[len(prefix) :] if db_url.startswith(prefix) else db_url)
    vm_url = os.environ.get("HOMELAB_MONITOR_VM_URL", "http://victoriametrics:8428")
    vm_data_dir = Path(os.environ.get("HOMELAB_MONITOR_VM_DATA_DIR", "/var/vm-data"))
    backup_root = Path(
        os.environ.get("HOMELAB_MONITOR_BACKUP_ROOT", "/storage/backup/homelab-monitor")
    )
    # CLI gets its own short-lived AsyncClient; not the app.state one.
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    return BackupService(
        db_path=db_path,
        vm_url=vm_url,
        vm_data_dir=vm_data_dir,
        backup_root=backup_root,
        http_client=client,
    )


async def _cmd_run() -> int:
    """``hm backup run``: execute a full backup, print summary."""
    service = _build_service_sync()
    try:
        result = await service.run_backup()
    finally:
        # Close the per-CLI httpx client we created above.
        await service._http.aclose()  # pyright: ignore[reportPrivateUsage]
    print(json.dumps(asdict(result), indent=2))
    return 1 if result.errors else 0


def _cmd_list() -> int:
    """``hm backup list``: list existing backups as JSON."""
    service = _build_service_sync()
    listing = service.list_backups()
    print(json.dumps(listing, indent=2))
    return 0


def _cmd_retention(keep: int) -> int:
    """``hm backup retention --keep N``: apply retention to both components."""
    service = _build_service_sync()
    deleted = service.apply_retention(keep=keep)
    print(json.dumps(deleted, indent=2))
    return 0
