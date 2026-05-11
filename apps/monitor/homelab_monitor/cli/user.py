"""``hm user`` subcommand: create / list / passwd / delete operator users."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from homelab_monitor.kernel.auth.passwords import MIN_PASSWORD_LENGTH, hash_password
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.repository import SqliteRepository


# argparse exposes no public type for sub-parsers; using the private alias is the
# pyright-recommended workaround. https://github.com/python/typeshed/issues/2569
def add_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Wire ``hm user`` and its sub-subcommands onto an argparse subparsers object."""
    user = subparsers.add_parser("user", help="Manage operator users")
    sub = user.add_subparsers(dest="user_cmd")

    p_create = sub.add_parser("create", help="Create a new user (prompts for password twice)")
    p_create.add_argument("username")

    sub.add_parser("list", help="List all users (no hashes)")

    p_passwd = sub.add_parser("passwd", help="Change a user's password (admin override)")
    p_passwd.add_argument("username")

    p_delete = sub.add_parser("delete", help="Delete a user")
    p_delete.add_argument("username")

    user.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    """Dispatch ``hm user`` / ``hm user <cmd>``."""
    sub = getattr(args, "user_cmd", None)
    if sub is None:
        print("usage: hm user {create,list,passwd,delete}", file=sys.stderr)
        return 2
    if sub == "create":
        return asyncio.run(_cmd_create(args.username))
    if sub == "list":
        return asyncio.run(_cmd_list())
    if sub == "passwd":
        return asyncio.run(_cmd_passwd(args.username))
    if sub == "delete":
        return asyncio.run(_cmd_delete(args.username))
    print(f"unknown subcommand: {sub}", file=sys.stderr)  # pragma: no cover
    return 2  # pragma: no cover


async def _build_repo() -> AuthRepository:
    """Construct an :class:`AuthRepository` from env config."""
    engine = get_engine()
    return AuthRepository(SqliteRepository(engine))


def _read_password_line(prompt: str) -> str:
    """Read one password line: try getpass first; fall back to stdin readline.

    getpass opens /dev/tty directly, which fails in docker bootstrap when there
    is no controlling terminal. Catching GetPassWarning + the underlying
    GetPassError/IOError lets piped ``printf 'pw\\npw\\n' | hm user create``
    work while keeping the normal interactive path (and test mocks) intact.
    """
    try:
        return getpass.getpass(prompt)
    except (getpass.GetPassWarning, EOFError, OSError):
        # No tty available (non-interactive docker bootstrap); read from stdin.
        return sys.stdin.readline().rstrip("\n")


def _prompt_password_twice() -> str | None:
    """Prompt for password TWICE; return plaintext or None on mismatch / validation error."""
    pw1 = _read_password_line("Password: ")
    pw2 = _read_password_line("Confirm password: ")
    if pw1 != pw2:
        print("error: passwords do not match", file=sys.stderr)
        return None
    if len(pw1) < MIN_PASSWORD_LENGTH:
        print(
            f"error: password must be at least {MIN_PASSWORD_LENGTH} characters",
            file=sys.stderr,
        )
        return None
    return pw1


async def _cmd_create(username: str) -> int:
    """``hm user create USERNAME``: create user with password prompt."""
    pw = _prompt_password_twice()
    if pw is None:
        return 1
    repo = await _build_repo()
    existing = await repo.get_user_by_username(username)
    if existing is not None:
        print(f"error: user already exists: {username}", file=sys.stderr)
        return 1
    pw_hash = hash_password(pw)
    user = await repo.create_user(username, pw_hash, who=username)
    print(f"created user: id={user.id} username={user.username}")
    return 0


async def _cmd_list() -> int:
    """``hm user list``: print one line per user, id + username + created_at."""
    repo = await _build_repo()
    users = await repo.list_users()
    if not users:
        print("(no users)")
        return 0
    for u in users:
        print(f"{u.id}\t{u.username}\tcreated={u.created_at}")
    return 0


async def _cmd_passwd(username: str) -> int:
    """``hm user passwd USERNAME``: change password (admin override)."""
    pw = _prompt_password_twice()
    if pw is None:
        return 1
    repo = await _build_repo()
    user = await repo.get_user_by_username(username)
    if user is None:
        print(f"error: user not found: {username}", file=sys.stderr)
        return 1
    pw_hash = hash_password(pw)
    await repo.change_password(user.id, pw_hash, who="operator")
    await repo.delete_all_user_sessions(user.id)
    print(f"password changed for: {username} (all sessions invalidated)")
    return 0


async def _cmd_delete(username: str) -> int:
    """``hm user delete USERNAME``: delete a user."""
    repo = await _build_repo()
    user = await repo.get_user_by_username(username)
    if user is None:
        print(f"error: user not found: {username}", file=sys.stderr)
        return 1
    await repo.delete_user(user.id, who="operator")
    print(f"deleted user: {username}")
    return 0
