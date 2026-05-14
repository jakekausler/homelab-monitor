"""``hm collector`` subcommand. Collector operations like unquarantine."""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logging import configure_logging
from homelab_monitor.kernel.scheduler.failure_budget import FailureBudget


def add_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    collector = subparsers.add_parser("collector", help="Collector operations")
    sub = collector.add_subparsers(dest="collector_cmd")
    p_unq = sub.add_parser(
        "unquarantine",
        help="Clear quarantine for one or all collectors",
    )
    p_unq.add_argument(
        "name",
        nargs="?",
        help="Collector name (omit to clear all)",
    )
    p_unq.set_defaults(func=_handle)
    collector.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    sub = getattr(args, "collector_cmd", None)
    if sub == "unquarantine":
        name = getattr(args, "name", None)
        return asyncio.run(_cmd_unquarantine(name))
    print("usage: hm collector {unquarantine}", file=sys.stderr)
    return 2


async def _cmd_unquarantine(name: str | None) -> int:
    configure_logging()
    log: BoundLogger = structlog.get_logger()  # pyright: ignore[reportAssignmentType]

    engine = get_engine()
    repo = SqliteRepository(engine)
    failure_budget = FailureBudget(repo, log)

    if name is None:
        # Clear all quarantine
        cleared = await failure_budget.clear_all_quarantine(by="cli")
        if cleared == 0:
            print("No quarantined collectors found.")
        else:
            print(f"Cleared quarantine for {cleared} collector(s).")
        return 0
    else:
        # Clear specific collector
        await failure_budget.load_state()
        if failure_budget.is_quarantined(name):
            await failure_budget.clear_quarantine(name, by="cli")
            print(f"Cleared quarantine for collector: {name}")
            return 0
        else:
            print(f"Collector not quarantined: {name}", file=sys.stderr)
            return 1
