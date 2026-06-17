"""``hm ssh-probe`` subcommand: keygen + capture-hostkey (STAGE-017-004).

``keygen <target> [--rotate]`` generates a per-target ed25519 keypair, stores the
PEM private key in secrets as ``ssh_probe_key_<target>``, and prints ONLY the bare
public key line. The private key PEM is NEVER printed or logged.

``capture-hostkey <target> [--host H] [--port P]`` connects read-only and captures
the server's host key via an in-process asyncssh ``validate_host_public_key``
callback (fires PRE-AUTH, so no client key is needed — solves the chicken-and-egg).
It prints the bare host-key line + ``SHA256:`` fingerprint + a TOFU warning + a
paste instruction. It writes NOTHING (no secret, no config edit).

This module mirrors the two-level argparse dispatch of ``cli/secrets.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

import asyncssh

from homelab_monitor.cli._support import build_secrets_repo
from homelab_monitor.kernel.secrets.errors import MasterKeyError, SecretNotFoundError
from homelab_monitor.kernel.ssh.config import load_ssh_targets

_TARGET_RE = re.compile(r"[A-Za-z0-9._-]+")
_MIN_PORT = 1
_MAX_PORT = 65535
_CAPTURE_CONNECT_TIMEOUT = 5
_CAPTURE_LOGIN_TIMEOUT = 5


# argparse exposes no public type for sub-parsers; using the private alias is the
# pyright-recommended workaround. https://github.com/python/typeshed/issues/2569
def add_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Wire ``hm ssh-probe`` and its sub-subcommands."""
    ssh_probe = subparsers.add_parser(
        "ssh-probe", help="Per-target SSH probe key + host-key tooling"
    )
    sub = ssh_probe.add_subparsers(dest="ssh_probe_cmd")

    p_keygen = sub.add_parser(
        "keygen",
        help="Generate + store a per-target ed25519 private key; print the public key",
    )
    p_keygen.add_argument("target", help="Target id (charset [A-Za-z0-9._-]+)")
    p_keygen.add_argument(
        "--rotate",
        action="store_true",
        help="Replace an existing key (BREAKS the probe until the new public key is reinstalled)",
    )

    p_capture = sub.add_parser(
        "capture-hostkey",
        help="Connect read-only and print the target's host key + fingerprint (TOFU)",
    )
    p_capture.add_argument("target", help="Target id (charset [A-Za-z0-9._-]+)")
    p_capture.add_argument(
        "--host", default=None, help="Override host (skips ssh_targets config lookup)"
    )
    p_capture.add_argument(
        "--port", type=int, default=None, help="Override port (1-65535; default 22)"
    )

    ssh_probe.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    """Dispatch ``hm ssh-probe <cmd>``."""
    sub = getattr(args, "ssh_probe_cmd", None)
    if sub == "keygen":
        return asyncio.run(_cmd_keygen(args.target, rotate=bool(args.rotate)))
    if sub == "capture-hostkey":
        return asyncio.run(
            _cmd_capture_hostkey(args.target, host_override=args.host, port_override=args.port)
        )
    print("usage: hm ssh-probe {keygen,capture-hostkey}", file=sys.stderr)
    return 2


def _valid_target(target: str) -> bool:
    """Return True iff ``target`` is non-empty and matches the id charset."""
    return _TARGET_RE.fullmatch(target) is not None


async def _cmd_keygen(target: str, *, rotate: bool) -> int:
    """``hm ssh-probe keygen TARGET [--rotate]``."""
    if not _valid_target(target):
        print(
            f"error: invalid target id {target!r}; allowed charset is [A-Za-z0-9._-]",
            file=sys.stderr,
        )
        return 1

    secret_name = f"ssh_probe_key_{target}"

    try:
        repo = await build_secrets_repo()
    except MasterKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    existing = await repo.get(secret_name)
    if existing is not None and not rotate:
        print(
            f"error: secret {secret_name!r} already exists; pass --rotate to replace it "
            "(this BREAKS the probe until the new public key is installed on the target)",
            file=sys.stderr,
        )
        return 1

    key = asyncssh.generate_private_key("ssh-ed25519")  # pyright: ignore[reportUnknownMemberType]
    pem = key.export_private_key().decode()  # pyright: ignore[reportUnknownMemberType]
    pub = key.export_public_key().decode().strip()  # pyright: ignore[reportUnknownMemberType]

    if rotate:
        try:
            await repo.rotate(secret_name, pem, who="hm ssh-probe keygen")
        except SecretNotFoundError:
            print(
                f"error: no secret named {secret_name!r}; omit --rotate to create it",
                file=sys.stderr,
            )
            return 1
    else:
        await repo.set(secret_name, pem, who="hm ssh-probe keygen")

    print(pub)
    print(
        "# install this public key on the target per `hm ssh-probe install-instructions` "
        "(STAGE-017-005)"
    )
    return 0


async def _cmd_capture_hostkey(
    target: str, *, host_override: str | None, port_override: int | None
) -> int:
    """``hm ssh-probe capture-hostkey TARGET [--host H] [--port P]``."""
    if not _valid_target(target):
        print(
            f"error: invalid target id {target!r}; allowed charset is [A-Za-z0-9._-]",
            file=sys.stderr,
        )
        return 1

    if port_override is not None and not (_MIN_PORT <= port_override <= _MAX_PORT):
        print(
            f"error: --port must be between {_MIN_PORT} and {_MAX_PORT}; got {port_override}",
            file=sys.stderr,
        )
        return 1

    if host_override is not None:
        host = host_override
        port = port_override if port_override is not None else 22
    else:
        targets = load_ssh_targets()
        params = targets.get(target)
        if params is None:
            print(
                f"error: target {target!r} not found in ssh_targets config; "
                "declare it (host + port) or pass --host/--port",
                file=sys.stderr,
            )
            return 1
        host = params.host
        port = port_override if port_override is not None else params.port

    captured = await _capture(host, port)
    if captured is None:
        print(
            f"error: could not capture a host key from {host}:{port}",
            file=sys.stderr,
        )
        return 1

    bare = captured.export_public_key().decode().strip()  # pyright: ignore[reportUnknownMemberType]
    fingerprint = asyncssh.import_public_key(bare).get_fingerprint()  # pyright: ignore[reportUnknownMemberType]

    print(f"# host key for target {target!r} at {host}:{port}")
    print(bare)
    print(f"fingerprint: {fingerprint}")
    print(
        "# WARNING (TOFU): this key was captured on FIRST contact and is NOT yet trusted. "
        "Verify the fingerprint above OUT-OF-BAND before pinning."
    )
    print(f"# To pin: set ssh_targets[{target!r}].host_key to the bare line above.")
    return 0


async def _capture(host: str, port: int) -> asyncssh.SSHKey | None:
    """Connect read-only and return the server's host key, or ``None`` on failure.

    Uses an in-process ``validate_host_public_key`` callback that fires PRE-AUTH
    (before any client key is needed). ``known_hosts`` must be set to
    ``asyncssh.import_known_hosts("")`` — a truthy-but-empty KnownHosts object.
    Omitting it, passing ``None``, ``b''``, ``[]``, or any other falsy value all
    cause asyncssh to auto-load ``~/.ssh/known_hosts`` (internal check:
    ``if not self._known_hosts:``), which suppresses the callback for any key
    already trusted there. The ``asyncssh.PermissionDenied`` raised after capture
    (auth fails — we sent no client key) is EXPECTED and swallowed.
    Connection-level failures (refused / timeout / DNS) return ``None``.
    """
    captured: dict[str, asyncssh.SSHKey] = {}

    class _CaptureClient(asyncssh.SSHClient):
        def validate_host_public_key(
            self, host: str, addr: str, port: int, key: asyncssh.SSHKey
        ) -> bool:
            captured["key"] = key
            return True

    try:
        conn = await asyncssh.connect(  # pyright: ignore[reportUnknownMemberType]
            host,
            port=port,
            username="hostkey-capture",
            client_factory=_CaptureClient,
            client_keys=None,
            password=None,
            agent_path=None,
            known_hosts=asyncssh.import_known_hosts(""),
            preferred_auth="none",
            connect_timeout=_CAPTURE_CONNECT_TIMEOUT,
            login_timeout=_CAPTURE_LOGIN_TIMEOUT,
        )
        conn.close()
        await conn.wait_closed()
    except asyncssh.PermissionDenied:
        # Expected: auth fails because we sent no client key. Host key is already
        # captured by the callback during key exchange (pre-auth).
        pass
    except (asyncssh.Error, OSError) as exc:
        # Connection-level failure (refused / timeout / DNS / protocol). Report
        # via the caller's "could not capture" path.
        print(f"error: ssh connect to {host}:{port} failed: {exc}", file=sys.stderr)
        return None

    return captured.get("key")
