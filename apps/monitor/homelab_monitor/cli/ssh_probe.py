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
from homelab_monitor.kernel.ssh.client import AsyncSshClientFactory
from homelab_monitor.kernel.ssh.config import load_ssh_target_configs, load_ssh_targets
from homelab_monitor.kernel.ssh.errors import (
    HostKeyMismatch,
    HostKeyNotPinned,
    SshAuthError,
    SshConnectionRefused,
    SshTimeout,
    SshTransportError,
)
from homelab_monitor.kernel.ssh.params import SshTargetParams

_TARGET_RE = re.compile(r"[A-Za-z0-9._-]+")
_MIN_PORT = 1
_MAX_PORT = 65535
_CAPTURE_CONNECT_TIMEOUT = 5
_CAPTURE_LOGIN_TIMEOUT = 5
_RESTRICTION_EXIT_CODE = 3


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

    p_install = sub.add_parser(
        "install-instructions",
        help="Print the account-mode-aware manual setup recipe (no network; no private key)",
    )
    p_install.add_argument("target", help="Target id (charset [A-Za-z0-9._-]+)")

    p_test = sub.add_parser(
        "test",
        help="Connect + run the forced command + verify an arbitrary command is refused",
    )
    p_test.add_argument("target", help="Target id (charset [A-Za-z0-9._-]+)")

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
    if sub == "install-instructions":
        return asyncio.run(_cmd_install_instructions(args.target))
    if sub == "test":
        return asyncio.run(_cmd_test(args.target))
    print(
        "usage: hm ssh-probe {keygen,capture-hostkey,install-instructions,test}",
        file=sys.stderr,
    )
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


def _render_appliance_instructions(target: str, forced: str, pub: str, key_secret_ref: str) -> str:
    """Render the appliance-mode setup recipe (firmware-persistence warning included)."""
    authkeys_line = (
        f'command="{forced}",no-port-forwarding,no-pty,no-X11-forwarding,'
        f"no-agent-forwarding {pub} hm-probe-{target}"
    )
    return f"""SSH probe setup — target '{target}' (appliance mode)

This framework NEVER writes to the target's auth config. Perform these steps by hand
on the target as the privileged user.

1. Append this EXACT single line to the target's authorized_keys (e.g. /root/.ssh/authorized_keys):

{authkeys_line}

2. Verify the restriction holds:

   hm ssh-probe test {target}

WARNING (UniFi OS firmware persistence): On UniFi OS appliances, /root/.ssh/authorized_keys
lives on an overlayfs layer that is wiped by firmware upgrades. After every UniFi OS update,
re-apply the authorized_keys line above or the probe will fail to connect.

The private key is NEVER printed and stays in the secrets store (secret: {key_secret_ref})."""


def _render_dedicated_user_instructions(
    target: str, user: str, pub: str, key_secret_ref: str
) -> str:
    """Render the dedicated-user-mode 5-step setup recipe (no persistence warning)."""
    authkeys_line = (
        f'command="/home/{user}/hm-probe.sh",no-port-forwarding,no-pty,'
        f"no-X11-forwarding,no-agent-forwarding {pub} hm-probe-{target}"
    )
    return f"""SSH probe setup — target '{target}' (dedicated-user mode)

This framework NEVER writes to the target's auth config. Perform these steps by hand
on the target as an administrator.

1. Create the dedicated low-privilege user '{user}' (no interactive login beyond the forced
   command; do NOT add it to admin/root groups). On a generic Linux target:

   sudo useradd -m -s /bin/sh {user}

2. Install the read-only probe script at /home/{user}/hm-probe.sh (owned by {user}, mode 0755):

   #!/bin/sh
   # hm-probe exemplar — replace body with the real collector (EPIC-008).
   uptime

3. (Only if your script needs privileged read commands) add a narrow NOPASSWD sudoers line.
   The exemplar 'uptime' needs NO sudo — skip this step for the exemplar. When a real probe
   (e.g. EPIC-008) needs privileged reads, add to /etc/sudoers.d/hm-probe-{user} (validate with
   'visudo -cf'):

   {user} ALL=(root) NOPASSWD: <ABSOLUTE_PATHS_OF_READ_ONLY_COMMANDS>

4. Append this EXACT single line to /home/{user}/.ssh/authorized_keys (owned by {user}, mode 0600):

{authkeys_line}

5. Verify the restriction holds:

   hm ssh-probe test {target}

The private key is NEVER printed and stays in the secrets store (secret: {key_secret_ref})."""


async def _cmd_install_instructions(target: str) -> int:  # noqa: PLR0911
    """``hm ssh-probe install-instructions TARGET`` — pure render, no network."""
    if not _valid_target(target):
        print(
            f"error: invalid target id {target!r}; allowed charset is [A-Za-z0-9._-]",
            file=sys.stderr,
        )
        return 1

    try:
        configs = load_ssh_target_configs()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    cfg = configs.get(target)
    if cfg is None:
        print(f"error: ssh target {target!r} not in config", file=sys.stderr)
        return 1

    # key_secret_ref is guaranteed non-None after the model_validator.
    key_secret_ref = cfg.key_secret_ref
    assert key_secret_ref is not None

    try:
        repo = await build_secrets_repo()
    except MasterKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    pem = await repo.get(key_secret_ref)
    if pem is None:
        print(
            f"error: no probe key for {target!r} — run 'hm ssh-probe keygen {target}' first",
            file=sys.stderr,
        )
        return 1

    pub = asyncssh.import_private_key(pem).export_public_key().decode().strip()  # pyright: ignore[reportUnknownMemberType]

    if cfg.account_mode == "appliance":
        if cfg.script_id is not None:
            print(
                "error: appliance mode forces a command, not a script "
                f"(target {target!r} has script_id set)",
                file=sys.stderr,
            )
            return 1
        if cfg.forced_command is not None:
            forced = cfg.forced_command
        else:
            forced = "<CONFIGURE forced_command IN ssh-targets.yaml>"
            print(
                f"NOTE: target {target!r} has no forced_command set; the rendered "
                "authorized_keys line uses a placeholder. Set forced_command in "
                "ssh-targets.yaml before installing.",
                file=sys.stderr,
            )
        print(_render_appliance_instructions(target, forced, pub, key_secret_ref))
    else:  # account_mode == "dedicated-user"
        print(_render_dedicated_user_instructions(target, cfg.user, pub, key_secret_ref))

    return 0


def _build_target_factory(
    target: str, params: SshTargetParams, key_secret_ref: str, pem: str
) -> AsyncSshClientFactory:
    """Build a one-off factory bound to a SINGLE resolved target.

    ``resolve`` returns the projected params only for this target id; ``secrets_for``
    is a sync lambda closed over the ALREADY-FETCHED PEM (the factory reads the
    secret synchronously, so we must resolve it async up-front).
    """
    return AsyncSshClientFactory(
        resolve=lambda tid: params if tid == target else None,
        secrets_for=lambda name: pem if name == key_secret_ref else None,
    )


async def _cmd_test(target: str) -> int:  # noqa: PLR0911
    """``hm ssh-probe test TARGET`` — connect, run an arbitrary command, verify the restriction.

    Exit codes: 0 = restriction holds; 1 = could not test (config/key/connection/
    auth/host-key); 3 = connected but restriction BROKEN.
    """
    if not _valid_target(target):
        print(
            f"error: invalid target id {target!r}; allowed charset is [A-Za-z0-9._-]",
            file=sys.stderr,
        )
        return 1

    try:
        configs = load_ssh_target_configs()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    cfg = configs.get(target)
    if cfg is None:
        print(f"error: ssh target {target!r} not in config", file=sys.stderr)
        return 1

    if not cfg.host_key:
        print(
            f"error: no pinned host key for {target!r} — run "
            f"'hm ssh-probe capture-hostkey {target}' and add host_key to the config first",
            file=sys.stderr,
        )
        return 1

    key_secret_ref = cfg.key_secret_ref
    assert key_secret_ref is not None

    try:
        repo = await build_secrets_repo()
    except MasterKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    pem = await repo.get(key_secret_ref)
    if pem is None:
        print(
            f"error: no probe key for {target!r} — run 'hm ssh-probe keygen {target}' first",
            file=sys.stderr,
        )
        return 1

    # Projected params for host/port/user/pinned_host_key/account_mode.
    params = load_ssh_targets()[target]
    factory = _build_target_factory(target, params, key_secret_ref, pem)

    marker = f"HM_PROBE_RESTRICTION_CHECK_{target}"
    arbitrary = f"echo {marker}"

    try:
        async with factory.open(target) as conn:
            result = await conn.run(arbitrary)
    except HostKeyMismatch as exc:
        print(
            f"CRITICAL: host key mismatch for {target!r} — possible MITM",
            file=sys.stderr,
        )
        print(f"error: {exc} (HostKeyMismatch)", file=sys.stderr)
        return 1
    except (
        HostKeyNotPinned,
        SshAuthError,
        SshTimeout,
        SshConnectionRefused,
        SshTransportError,
    ) as exc:
        print(f"error: {exc} ({type(exc).__name__})", file=sys.stderr)
        return 1

    if marker not in result.stdout:
        print(f"forced-command output:\n{result.stdout.rstrip()}")
        print(f"PASS: forced-command restriction enforced for {target!r}")
        return 0

    print(
        f"FAIL: restriction NOT enforced for {target!r} — the arbitrary command "
        'executed. Re-check the authorized_keys line includes the command="..." option.',
        file=sys.stderr,
    )
    return _RESTRICTION_EXIT_CODE
