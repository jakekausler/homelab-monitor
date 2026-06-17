"""asyncssh-backed SSH transport (STAGE-017-001).

Implements ``SshClientFactory`` / ``SshConnection`` (see
``kernel/plugins/io.py``) on top of asyncssh:

- open-per-run, no pool: ``open(target_id)`` resolves params, connects with the
  per-target client key + the PINNED host key, yields a connection, and closes it
  on context exit.
- pinned host-key verification: a target with no pinned key raises
  ``HostKeyNotPinned`` BEFORE connecting; a mismatch surfaces as the distinct
  ``HostKeyMismatch``. We NEVER pass ``known_hosts=None`` and never auto-trust.
- typed error mapping: connect-refused / auth / host-key / timeout map to the
  ``kernel.ssh.errors`` hierarchy. A non-zero command exit code is NOT an error.

The ``resolve`` and ``secrets_for`` seams are injected at construction. At
lifespan, ``secrets_for`` is backed by the UNFILTERED
``ttl_resolver.current().get`` (NOT the per-collector filtered ``ctx.secrets``
view), because the factory is a process-wide singleton — mirroring the HA
``token_provider`` pattern. SECURITY: the private key PEM and host keys are never
logged.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import asyncssh

from homelab_monitor.kernel.ssh.errors import (
    HostKeyMismatch,
    HostKeyNotPinned,
    SshAuthError,
    SshConnectionRefused,
    SshTimeout,
    SshTransportError,
)
from homelab_monitor.kernel.ssh.params import SshTargetParams
from homelab_monitor.kernel.ssh.result import SshCommandResult

_DEFAULT_CONNECT_TIMEOUT: float = 10.0
_DEFAULT_COMMAND_TIMEOUT: float = 30.0


class _AsyncSshConnection:
    """Wraps a live ``asyncssh.SSHClientConnection`` and runs one command."""

    def __init__(
        self,
        target_id: str,
        conn: asyncssh.SSHClientConnection,
        *,
        command_timeout: float,
    ) -> None:
        self._target_id = target_id
        self._conn = conn
        self._command_timeout = command_timeout

    async def run(self, command: str = "") -> SshCommandResult:
        """Run ``command`` on the remote and return typed output.

        A non-zero exit status is NOT raised (``check=False``); the caller
        interprets exit codes. ``None`` exit status (e.g. signal-killed) maps to
        ``-1``. Command-phase transport failures map to ``SshTimeout`` /
        ``SshTransportError``.
        """
        try:
            proc = await self._conn.run(command, check=False, timeout=self._command_timeout)
        except TimeoutError as exc:
            raise SshTimeout(self._target_id, "ssh command timed out") from exc
        except asyncssh.Error as exc:
            raise SshTransportError(self._target_id, "ssh command failed") from exc
        exit_status = proc.exit_status if proc.exit_status is not None else -1
        # asyncssh defaults to utf-8 str output; we never pass encoding=None.
        assert isinstance(proc.stdout, str), f"expected str stdout, got {type(proc.stdout)}"
        assert isinstance(proc.stderr, str), f"expected str stderr, got {type(proc.stderr)}"
        return SshCommandResult(stdout=proc.stdout, stderr=proc.stderr, exit_status=exit_status)


class AsyncSshClientFactory:
    """Opens pinned-host-key SSH connections to resolved targets, one per run."""

    def __init__(
        self,
        resolve: Callable[[str], SshTargetParams | None],
        secrets_for: Callable[[str], str | None],
        *,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        """Construct the factory.

        Args:
            resolve: maps a ``target_id`` to its ``SshTargetParams`` (or ``None``
                when the target is unknown). STAGE-017-002 supplies the real
                config-backed resolver; tests pass a dict-backed lambda.
            secrets_for: reads a per-target private key PEM by secret name (or
                ``None`` when absent). At lifespan this is backed by the
                UNFILTERED ``ttl_resolver.current().get`` — see module docstring.
            connect_timeout: per-connect timeout in seconds. Bounds BOTH the TCP
                connect phase AND the SSH login/auth phase (passed as both
                ``connect_timeout`` and ``login_timeout`` to asyncssh).
            command_timeout: per-command run timeout in seconds.
        """
        self._resolve = resolve
        self._secrets_for = secrets_for
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout

    def open(self, target_id: str) -> AbstractAsyncContextManager[_AsyncSshConnection]:
        """Open a pinned connection to ``target_id``; yield it, close on exit.

        Resolve + pin-check + key-read + connect + error-mapping all happen on
        context ENTRY, so those failures raise from ``async with``. ``run()``
        raises only command-phase failures.

        Both the TCP connect phase AND the SSH login/auth phase are bounded by
        ``connect_timeout`` (passed as ``connect_timeout`` and ``login_timeout``
        to asyncssh, which tracks them separately).
        """
        return self._open(target_id)

    @asynccontextmanager
    async def _open(self, target_id: str) -> AsyncGenerator[_AsyncSshConnection, None]:
        params = self._resolve(target_id)
        if params is None:
            raise SshTransportError(target_id, "unknown ssh target")
        if not params.pinned_host_key:
            raise HostKeyNotPinned(target_id, "no pinned host key for target")
        pem = self._secrets_for(params.key_secret_name)
        if pem is None:
            raise SshAuthError(target_id, "no private key available for target")

        try:
            pinned = asyncssh.import_public_key(params.pinned_host_key)
            client_key = asyncssh.import_private_key(pem)
        except (asyncssh.Error, ValueError) as exc:
            raise SshTransportError(target_id, "invalid pinned host key or client key") from exc

        def _known_hosts(
            host: str, addr: str, port: int
        ) -> tuple[list[asyncssh.SSHKey], list[asyncssh.SSHKey], list[asyncssh.SSHKey]]:
            del host, addr, port
            # Tuple positions: (host_keys, ca_keys, revoked_keys)
            return ([pinned], [], [])

        try:
            conn = await asyncssh.connect(
                params.host,
                port=params.port,
                username=params.user,
                known_hosts=_known_hosts,
                client_keys=[client_key],
                agent_path=None,
                password=None,
                connect_timeout=self._connect_timeout,
                login_timeout=self._connect_timeout,
            )
        except asyncssh.HostKeyNotVerifiable as exc:
            raise HostKeyMismatch(target_id, "server host key did not match pinned key") from exc
        except asyncssh.PermissionDenied as exc:
            raise SshAuthError(target_id, "ssh authentication failed") from exc
        except TimeoutError as exc:
            raise SshTimeout(target_id, "ssh connect timed out") from exc
        except (ConnectionRefusedError, socket.gaierror) as exc:
            raise SshConnectionRefused(target_id, "ssh connection refused or unreachable") from exc
        except asyncssh.Error as exc:  # asyncssh.Error is NOT an OSError subclass — distinct branch
            raise SshTransportError(target_id, "ssh connect failed") from exc
        except OSError as exc:  # non-asyncssh socket errors (no route, etc.)
            raise SshConnectionRefused(target_id, "ssh connection refused or unreachable") from exc

        try:
            async with conn:
                yield _AsyncSshConnection(target_id, conn, command_timeout=self._command_timeout)
        finally:
            conn.close()
            await conn.wait_closed()
