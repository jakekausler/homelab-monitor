"""``hm api-token`` subcommand: create / list / revoke API tokens."""

from __future__ import annotations

import argparse
import asyncio
import sys

from homelab_monitor.kernel.auth.api_tokens import make_api_token
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.repository import SqliteRepository


# argparse exposes no public type for sub-parsers; using the private alias is the
# pyright-recommended workaround. https://github.com/python/typeshed/issues/2569
def add_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Wire ``hm api-token`` and its sub-subcommands onto an argparse subparsers object."""
    at = subparsers.add_parser("api-token", help="Manage API tokens")
    sub = at.add_subparsers(dest="api_token_cmd")

    p_create = sub.add_parser("create", help="Create a new API token (printed once)")
    p_create.add_argument(
        "--scope",
        action="append",
        required=True,
        help=(
            "Scope to grant (repeatable); allowed: heartbeat:write,alerts:ingest:write,read:status"
        ),
    )
    p_create.add_argument("--name", required=True, help="Human-readable token name (UNIQUE)")

    sub.add_parser("list", help="List API tokens (metadata only)")

    p_revoke = sub.add_parser("revoke", help="Revoke an API token by id")
    p_revoke.add_argument("token_id")

    at.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    """Dispatch ``hm api-token`` / ``hm api-token <cmd>``."""
    sub = getattr(args, "api_token_cmd", None)
    if sub is None:
        print("usage: hm api-token {create,list,revoke}", file=sys.stderr)
        return 2
    if sub == "create":
        return asyncio.run(_cmd_create(args.scope, args.name))
    if sub == "list":
        return asyncio.run(_cmd_list())
    if sub == "revoke":
        return asyncio.run(_cmd_revoke(args.token_id))
    print(f"unknown subcommand: {sub}", file=sys.stderr)  # pragma: no cover
    return 2  # pragma: no cover


async def _build_repo() -> AuthRepository:
    """Construct an :class:`AuthRepository` from env config."""
    engine = get_engine()
    return AuthRepository(SqliteRepository(engine))


async def _cmd_create(scopes_raw: list[str], name: str) -> int:
    """``hm api-token create --scope ... --name NAME``: create and print plaintext token."""
    scopes: set[Scope] = set()
    for raw in scopes_raw:
        try:
            scopes.add(Scope(raw))
        except ValueError:
            print(f"error: unknown scope: {raw}", file=sys.stderr)
            return 1
    plaintext, _sha = make_api_token()
    repo = await _build_repo()
    token = await repo.create_api_token(name, scopes, plaintext, who="operator")
    print(f"created api token: id={token.id} name={token.name} scopes={token.scopes}")
    print("=" * 60)
    print("TOKEN (copy now; this is the ONLY time it will be shown):")
    print(plaintext)
    print("=" * 60)
    return 0


async def _cmd_list() -> int:
    """``hm api-token list``: print one line per token, metadata only (no hash)."""
    repo = await _build_repo()
    tokens = await repo.list_api_tokens()
    if not tokens:
        print("(no api tokens)")
        return 0
    for t in tokens:
        last = t.last_used_at if t.last_used_at is not None else "-"
        print(f"{t.id}\t{t.name}\tscopes={t.scopes}\tcreated={t.created_at}\tlast_used={last}")
    return 0


async def _cmd_revoke(token_id: str) -> int:
    """``hm api-token revoke TOKEN_ID``: revoke (delete) an API token."""
    repo = await _build_repo()
    try:
        await repo.revoke_api_token(token_id, who="operator")
    except LookupError:
        print(f"error: api token not found: {token_id}", file=sys.stderr)
        return 1
    print(f"revoked api token: {token_id}")
    return 0
