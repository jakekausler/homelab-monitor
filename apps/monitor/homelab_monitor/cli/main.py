"""Main CLI entry point — argparse dispatcher for the ``hm`` script."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from homelab_monitor import __version__
from homelab_monitor.cli import migrate as migrate_cli
from homelab_monitor.cli import secrets as secrets_cli


def _build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser."""
    parser = argparse.ArgumentParser(prog="hm", description="homelab-monitor CLI")
    parser.add_argument(
        "--version",
        action="version",
        version=f"homelab-monitor {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    migrate_cli.add_subparser(subparsers)
    secrets_cli.add_subparser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the process exit code.

    With no subcommand, prints the version (preserves STAGE-001-001 behaviour).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        print(f"homelab-monitor {__version__}")
        return 0
    func = getattr(args, "func", None)
    if func is None:  # pragma: no cover
        parser.print_help()
        return 2
    rc = func(args)
    return int(rc) if rc is not None else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
