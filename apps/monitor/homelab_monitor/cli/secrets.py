"""``hm secrets`` subcommand: set/get/list/rotate/delete/rotate-master."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from homelab_monitor.cli._support import build_secrets_repo
from homelab_monitor.kernel.secrets.errors import (
    MasterKeyError,
    SecretIntegrityError,
    SecretNotFoundError,
)
from homelab_monitor.kernel.secrets.master_key import (
    decode_master_key_b64,
    master_key_fingerprint,
)
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

REVEAL_ENV = "HOMELAB_MONITOR_REVEAL"


# argparse exposes no public type for sub-parsers; using the private alias is the
# pyright-recommended workaround. https://github.com/python/typeshed/issues/2569
def add_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Wire ``hm secrets`` and its sub-subcommands."""
    secrets = subparsers.add_parser("secrets", help="Manage encrypted secrets")
    sub = secrets.add_subparsers(dest="secrets_cmd")

    p_set = sub.add_parser("set", help="Set or rotate a secret (value from stdin)")
    p_set.add_argument("name")
    p_set.add_argument(
        "--from-stdin",
        action="store_true",
        required=True,
        help="Read the value from stdin (no positional value, no prompt).",
    )

    p_get = sub.add_parser("get", help=f"Print a secret to stdout (requires {REVEAL_ENV}=1)")
    p_get.add_argument("name")

    sub.add_parser("list", help="List secret names + timestamps; never values")

    p_rotate = sub.add_parser("rotate", help="Rotate a secret (value from stdin)")
    p_rotate.add_argument("name")
    p_rotate.add_argument("--from-stdin", action="store_true", required=True)

    p_delete = sub.add_parser("delete", help="Delete a secret")
    p_delete.add_argument("name")

    p_rm = sub.add_parser(
        "rotate-master",
        help="Re-encrypt every secret with a new master key (from stdin)",
    )
    p_rm.add_argument("--from-stdin", action="store_true", required=True)

    secrets.set_defaults(func=_handle)


def _handle(  # noqa: PLR0911
    args: argparse.Namespace,
) -> int:  # one return per subcommand; flat dispatch is clearer than a dict
    """Dispatch ``hm secrets <cmd>``."""
    sub = getattr(args, "secrets_cmd", None)
    if sub is None:
        print(
            "usage: hm secrets {set,get,list,rotate,delete,rotate-master}",
            file=sys.stderr,
        )
        return 2
    if sub == "set":
        return asyncio.run(_cmd_set(args.name))
    if sub == "get":
        return asyncio.run(_cmd_get(args.name))
    if sub == "list":
        return asyncio.run(_cmd_list())
    if sub == "rotate":
        return asyncio.run(_cmd_rotate(args.name))
    if sub == "delete":
        return asyncio.run(_cmd_delete(args.name))
    if sub == "rotate-master":
        return asyncio.run(_cmd_rotate_master())
    print(f"unknown subcommand: {sub}", file=sys.stderr)  # pragma: no cover
    return 2  # pragma: no cover


async def _build_repo() -> AsyncSecretsRepository:
    """Construct an :class:`AsyncSecretsRepository` from env config.

    Delegates to :func:`homelab_monitor.cli._support.build_secrets_repo` (shared
    with ``cli/ssh_probe.py``).
    """
    return await build_secrets_repo()


async def _cmd_set(name: str) -> int:
    """``hm secrets set NAME --from-stdin``: read value from stdin, store."""
    value = sys.stdin.read().rstrip("\r\n")
    try:
        repo = await _build_repo()
        await repo.set(name, value, who="system")
    except MasterKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # ``# pragma: no cover``: defensive handler. ``set`` does not decrypt the
    # existing row (the rotation path overwrites without reading), so this branch
    # is unreachable today. Kept for uniform error-UX contract per code review I4.
    except SecretIntegrityError as exc:  # pragma: no cover
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"set: {name}")
    return 0


async def _cmd_get(name: str) -> int:
    """``hm secrets get NAME``: print plaintext if REVEAL=1, else error."""
    if os.environ.get(REVEAL_ENV) != "1":
        print(f"error: set {REVEAL_ENV}=1 to reveal secret values", file=sys.stderr)
        return 1
    try:
        repo = await _build_repo()
        value = await repo.get(name)
    except MasterKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SecretIntegrityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if value is None:
        print(f"error: no secret named {name!r}", file=sys.stderr)
        return 1
    print(value)
    return 0


async def _cmd_list() -> int:
    """``hm secrets list``: print one line per secret, names + timestamps only."""
    try:
        repo = await _build_repo()
        metas = await repo.list_names()
    except MasterKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not metas:
        print("(no secrets)")
        return 0
    for m in metas:
        rotated = m.rotated_at if m.rotated_at is not None else "-"
        print(f"{m.name}\tcreated={m.created_at}\trotated={rotated}")
    return 0


async def _cmd_rotate(name: str) -> int:
    """``hm secrets rotate NAME --from-stdin``: rotate existing secret."""
    value = sys.stdin.read().rstrip("\r\n")
    try:
        repo = await _build_repo()
        await repo.rotate(name, value, who="system")
    except MasterKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Defensive; rotate overwrites without decrypting the existing row.
    except SecretIntegrityError as exc:  # pragma: no cover
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SecretNotFoundError:
        print(f"error: no secret named {name!r}", file=sys.stderr)
        return 1
    print(f"rotated: {name}")
    return 0


async def _cmd_delete(name: str) -> int:
    """``hm secrets delete NAME``: remove a secret."""
    try:
        repo = await _build_repo()
        await repo.delete(name, who="system")
    except MasterKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Defensive; delete doesn't decrypt the row.
    except SecretIntegrityError as exc:  # pragma: no cover
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SecretNotFoundError:
        print(f"error: no secret named {name!r}", file=sys.stderr)
        return 1
    print(f"deleted: {name}")
    return 0


async def _cmd_rotate_master() -> int:
    """``hm secrets rotate-master --from-stdin``: re-encrypt under new master."""
    raw = sys.stdin.read().strip()
    try:
        new_master = decode_master_key_b64(raw, source="stdin")
    except MasterKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        repo = await _build_repo()
        old_fp = repo.current_fingerprint()
        count = await repo.rotate_master(new_master, who="system")
        repo.set_master_key(new_master)
        new_fp = master_key_fingerprint(new_master)
    except MasterKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SecretIntegrityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"rotated master key: {count} secret(s) re-encrypted")
    print(f"old fingerprint: {old_fp}")
    print(f"new fingerprint: {new_fp}")
    return 0
