"""``hm cron`` subcommand. Discover, install-wrapper, get-wrapper-template."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from importlib.resources import files
from pathlib import Path

from structlog import get_logger

from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.cron.install import (
    WrapperInstallError,
    build_install_kit,
    build_uninstall_kit,
    install_wrapper_local,
    uninstall_wrapper_local,
)
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.secrets.master_key import load_master_key
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository
from homelab_monitor.plugins.discoverers.cron_discoverer import CronDiscoverer, resolve_hostname


def add_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    cron = subparsers.add_parser("cron", help="Cron registry maintenance commands")
    sub = cron.add_subparsers(dest="cron_cmd")
    p_discover = sub.add_parser("discover", help="Trigger a one-shot cron discovery scan")
    p_discover.set_defaults(func=_handle)

    p_install = sub.add_parser(
        "install-wrapper", help="Install the heartbeat wrapper for a local cron"
    )
    p_install.add_argument("fingerprint")
    p_install.add_argument(
        "--confirm",
        action="store_true",
        help="Actually modify the crontab (omit for a dry-run preview)",
    )
    p_install.set_defaults(func=_handle)

    p_uninstall = sub.add_parser(
        "uninstall-wrapper", help="Remove the heartbeat wrapper from a local cron"
    )
    p_uninstall.add_argument("fingerprint")
    p_uninstall.add_argument(
        "--confirm",
        action="store_true",
        help="Actually modify the crontab (omit for a dry-run preview)",
    )
    p_uninstall.set_defaults(func=_handle)

    p_tmpl = sub.add_parser(
        "get-wrapper-template", help="Print the wrapper script template to stdout"
    )
    p_tmpl.set_defaults(func=_handle)

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
    if sub == "install-wrapper":
        return asyncio.run(_cmd_install_wrapper(args.fingerprint, confirm=args.confirm))
    if sub == "uninstall-wrapper":
        return asyncio.run(_cmd_uninstall_wrapper(args.fingerprint, confirm=args.confirm))
    if sub == "get-wrapper-template":
        return _cmd_get_wrapper_template()
    print(
        "usage: hm cron {discover,install-wrapper,uninstall-wrapper,get-wrapper-template}",
        file=sys.stderr,
    )
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


def _cmd_get_wrapper_template() -> int:
    """Print the wrapper script template to stdout."""
    template_text = (
        files("homelab_monitor")
        .joinpath("data", "cron-with-heartbeat.sh.tmpl")
        .read_text(encoding="utf-8")
    )
    print(template_text)
    return 0


async def _cmd_install_wrapper(fingerprint: str, confirm: bool) -> int:  # noqa: PLR0911 -- CLI command: each validation failure is its own early-return error path
    """Install the heartbeat wrapper for a local cron (or dry-run preview)."""
    try:
        # Build engine and repos
        engine = get_engine()
        repo = SqliteRepository(engine)
        cron_repo = CronRepo(repo)
        auth_repo = AuthRepository(repo)

        # Load secrets repo
        try:
            master_key = load_master_key()
        except Exception as exc:
            print(f"ERROR: failed to load master key: {exc}", file=sys.stderr)
            return 1
        secrets_repo = AsyncSecretsRepository(repo, master_key)

        # Resolve environment variables
        host_root = Path(os.environ.get("HM_CRON_HOST_ROOT", "/host"))
        public_url = os.environ.get("HOMELAB_MONITOR_PUBLIC_URL", "")
        if not public_url:
            print("ERROR: HOMELAB_MONITOR_PUBLIC_URL not set", file=sys.stderr)
            return 1

        local_hostname = resolve_hostname()

        # Get the cron
        cron = await cron_repo.get_cron(fingerprint, include_hidden=True)
        if cron is None:
            print(f"ERROR: cron not found: {fingerprint}", file=sys.stderr)
            return 1

        # Check it's local
        if cron.host != local_hostname:
            print(
                f"ERROR: cron is on host {cron.host!r}, not local {local_hostname!r}",
                file=sys.stderr,
            )
            return 1

        # Dry-run: build install kit and print preview
        if not confirm:
            kit = await build_install_kit(cron, host_root=host_root, public_url=public_url)
            print("=== Wrapper script ===")
            print(kit.wrapper_content)
            print("\n=== Crontab diff ===")
            print(f"File: {kit.crontab_diff.source_path}")
            print(f"- {kit.crontab_diff.old_line}")
            print(f"+ {kit.crontab_diff.new_line}")
            return 0

        # Confirm: actually install
        log = get_logger()
        await install_wrapper_local(
            fingerprint,
            cron_repo=cron_repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=host_root,
            public_url=public_url,
            local_hostname=local_hostname,
            who="cli",
            ip=None,
            log=log,
        )
        print(f"installed wrapper for {fingerprint}")
        return 0

    except WrapperInstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected error: {exc}", file=sys.stderr)
        return 1


async def _cmd_uninstall_wrapper(fingerprint: str, confirm: bool) -> int:
    """Remove the heartbeat wrapper from a local cron (or dry-run preview)."""
    try:
        engine = get_engine()
        repo = SqliteRepository(engine)
        cron_repo = CronRepo(repo)

        host_root = Path(os.environ.get("HM_CRON_HOST_ROOT", "/host"))
        local_hostname = resolve_hostname()

        cron = await cron_repo.get_cron(fingerprint, include_hidden=True)
        if cron is None:
            print(f"ERROR: cron not found: {fingerprint}", file=sys.stderr)
            return 1

        if cron.host != local_hostname:
            print(
                f"ERROR: cron is on host {cron.host!r}, not local {local_hostname!r}",
                file=sys.stderr,
            )
            return 1

        # Dry-run: build uninstall kit and print preview
        if not confirm:
            kit = await build_uninstall_kit(cron, host_root=host_root)
            print("=== Crontab diff ===")
            print(f"File: {kit.crontab_diff.source_path}")
            print(f"- {kit.crontab_diff.old_line}")
            print(f"+ {kit.crontab_diff.new_line}")
            return 0

        # Confirm: actually uninstall
        log = get_logger()
        await uninstall_wrapper_local(
            fingerprint,
            cron_repo=cron_repo,
            host_root=host_root,
            local_hostname=local_hostname,
            who="cli",
            ip=None,
            log=log,
        )
        print(f"removed wrapper for {fingerprint}")
        return 0

    except WrapperInstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected error: {exc}", file=sys.stderr)
        return 1
