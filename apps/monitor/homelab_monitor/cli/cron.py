"""``hm cron`` subcommand. Currently only `hm cron discover`."""

from __future__ import annotations

import argparse
import asyncio
import sys

from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.plugins.discoverers.cron_discoverer import CronDiscoverer


def add_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    cron = subparsers.add_parser("cron", help="Cron registry maintenance commands")
    sub = cron.add_subparsers(dest="cron_cmd")
    p_discover = sub.add_parser("discover", help="Trigger a one-shot cron discovery scan")
    p_discover.set_defaults(func=_handle)
    cron.set_defaults(func=_handle)


class _StderrLog:
    def warning(self, event: str, **fields: object) -> None:
        print(f"WARN {event}: {fields}", file=sys.stderr)

    def info(self, event: str, **fields: object) -> None:
        print(f"INFO {event}: {fields}", file=sys.stderr)


def _handle(args: argparse.Namespace) -> int:
    sub = getattr(args, "cron_cmd", None)
    if sub == "discover":
        return asyncio.run(_cmd_discover())
    print("usage: hm cron {discover}", file=sys.stderr)
    return 2


async def _cmd_discover() -> int:
    engine = get_engine()
    repo = SqliteRepository(engine)
    cron_repo = CronRepo(repo)
    discoverer = CronDiscoverer()
    result = await discoverer.scan(cron_repo, log=_StderrLog())
    print(
        f"discovered: found={len(result.found_fingerprints)} "
        f"inserted={result.inserted_count} updated={result.updated_count} "
        f"bump_only={result.bump_only_count} partial={result.partial} "
        f"errors={len(result.errors)}"
    )
    for err in result.errors:
        print(f"  ERROR {err.host_source_path}: {err.error}", file=sys.stderr)
    return 0 if not result.partial else 1
