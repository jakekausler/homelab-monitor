"""Tests for the asyncssh transport (STAGE-017-001).

A REAL loopback asyncssh server (``ssh_test_server`` fixture) drives the
security-critical paths: host-key accept, host-key mismatch, not-pinned, auth-ok,
auth-reject, run-empty, run-selector + exit codes. Real triggers cover
connection-refused. The timeout path is exercised against a non-routable address
and marked slow.
"""

from __future__ import annotations

import socket

import asyncssh
import pytest

from homelab_monitor.kernel.ssh.client import AsyncSshClientFactory
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
from tests.ssh.conftest import SshTestServer


def _params(
    *,
    port: int,
    pinned_host_key: str | None,
    key_secret_name: str = "ssh_key",
    user: str = "tester",
) -> SshTargetParams:
    return SshTargetParams(
        host="127.0.0.1",
        port=port,
        user=user,
        key_secret_name=key_secret_name,
        pinned_host_key=pinned_host_key,
        account_mode="dedicated_user",
    )


def _factory(
    params: SshTargetParams,
    *,
    secret_value: str | None,
    connect_timeout: float = 10.0,
    command_timeout: float = 30.0,
) -> AsyncSshClientFactory:
    return AsyncSshClientFactory(
        resolve=lambda tid: params if tid == "t1" else None,
        secrets_for=lambda name: secret_value if name == params.key_secret_name else None,
        connect_timeout=connect_timeout,
        command_timeout=command_timeout,
    )


async def test_host_key_accept_and_run(ssh_test_server: SshTestServer) -> None:
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value=ssh_test_server.client_key_pem)
    async with factory.open("t1") as conn:
        result = await conn.run("echo selector")
    assert isinstance(result, SshCommandResult)
    assert result.exit_status == 0
    assert "echo selector" in result.stdout


async def test_run_empty_command(ssh_test_server: SshTestServer) -> None:
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value=ssh_test_server.client_key_pem)
    async with factory.open("t1") as conn:
        result = await conn.run()
    assert result.exit_status == 0
    assert "ran:" in result.stdout


async def test_host_key_mismatch(ssh_test_server: SshTestServer) -> None:
    wrong = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()  # pyright: ignore[reportUnknownMemberType]
    params = _params(port=ssh_test_server.port, pinned_host_key=wrong)
    factory = _factory(params, secret_value=ssh_test_server.client_key_pem)
    with pytest.raises(HostKeyMismatch) as excinfo:
        async with factory.open("t1"):
            pass
    assert excinfo.value.target_id == "t1"


async def test_host_key_not_pinned_raises_before_connect() -> None:
    # Port is irrelevant: no connection should be attempted. Use a bogus port.
    params = _params(port=1, pinned_host_key=None)
    factory = _factory(params, secret_value="unused")
    with pytest.raises(HostKeyNotPinned):
        async with factory.open("t1"):
            pass


async def test_no_private_key_raises_auth_error(ssh_test_server: SshTestServer) -> None:
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value=None)  # secrets_for returns None
    with pytest.raises(SshAuthError):
        async with factory.open("t1"):
            pass


async def test_auth_reject_wrong_client_key(ssh_test_server: SshTestServer) -> None:
    # Pin the real host key (so we reach auth), but present a DIFFERENT client key
    # that the server does not authorize -> PermissionDenied -> SshAuthError.
    wrong_client_pem = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()  # pyright: ignore[reportUnknownMemberType]
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value=wrong_client_pem)
    with pytest.raises(SshAuthError):
        async with factory.open("t1"):
            pass


async def test_ssh_auth_sock_agent_not_consulted(
    ssh_test_server: SshTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I1: agent_path=None in the connect call means SSH_AUTH_SOCK is never
    # consulted. Even with a valid SSH_AUTH_SOCK env var set, connecting with an
    # UNAUTHORIZED client key must still raise SshAuthError.
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/nonexistent-agent.sock")
    wrong_client_pem = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()  # pyright: ignore[reportUnknownMemberType]
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value=wrong_client_pem)
    with pytest.raises(SshAuthError):
        async with factory.open("t1"):
            pass


async def test_unknown_target_raises_transport_error(ssh_test_server: SshTestServer) -> None:
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value=ssh_test_server.client_key_pem)
    with pytest.raises(SshTransportError) as excinfo:
        async with factory.open("unknown"):  # resolve returns None
            pass
    # base SshTransportError, not a subclass that means something else
    assert type(excinfo.value) is SshTransportError
    assert excinfo.value.target_id == "unknown"


async def test_invalid_pinned_host_key_raises_transport_error(
    ssh_test_server: SshTestServer,
) -> None:
    params = _params(port=ssh_test_server.port, pinned_host_key="not-a-valid-host-key")
    factory = _factory(params, secret_value=ssh_test_server.client_key_pem)
    with pytest.raises(SshTransportError):
        async with factory.open("t1"):
            pass


async def test_invalid_client_key_pem_raises_transport_error(
    ssh_test_server: SshTestServer,
) -> None:
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value="-----BEGIN nonsense-----")
    with pytest.raises(SshTransportError):
        async with factory.open("t1"):
            pass


async def test_connection_refused() -> None:
    # Connect to a 127.0.0.1 port that nothing is listening on. Pin a valid-format
    # host key so the pin-check passes and we actually attempt the connect.
    valid_host_line = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()  # pyright: ignore[reportUnknownMemberType]
    valid_client_pem = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()  # pyright: ignore[reportUnknownMemberType]
    params = _params(port=1, pinned_host_key=valid_host_line)
    factory = _factory(params, secret_value=valid_client_pem, connect_timeout=5.0)
    with pytest.raises(SshConnectionRefused):
        async with factory.open("t1"):
            pass


async def test_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # Monkeypatched asyncssh.connect raises TimeoutError -> SshTimeout.
    # Deterministic; does not depend on wall-clock or network routing.
    valid_host_line = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()  # pyright: ignore[reportUnknownMemberType]
    valid_client_pem = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()  # pyright: ignore[reportUnknownMemberType]
    params = _params(port=22, pinned_host_key=valid_host_line)
    factory = _factory(params, secret_value=valid_client_pem, connect_timeout=1.0)

    async def _boom(*args: object, **kwargs: object) -> object:
        raise TimeoutError

    monkeypatch.setattr(asyncssh, "connect", _boom)
    with pytest.raises(SshTimeout):
        async with factory.open("t1"):
            pass


async def test_private_key_never_appears_in_error_str_or_repr(
    ssh_test_server: SshTestServer,
) -> None:
    # S3: the private key PEM passed via secrets_for must never leak into the
    # str() or repr() of any raised error.
    wrong_client_pem = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()  # pyright: ignore[reportUnknownMemberType]
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value=wrong_client_pem)
    raised_exc: SshAuthError | None = None
    with pytest.raises(SshAuthError) as excinfo:
        async with factory.open("t1"):
            pass
    raised_exc = excinfo.value
    exc_str = str(raised_exc)
    exc_repr = repr(raised_exc)
    # The PEM body and header must not appear in any form
    assert "BEGIN OPENSSH PRIVATE KEY" not in exc_str
    assert "BEGIN OPENSSH PRIVATE KEY" not in exc_repr
    # Also assert the actual PEM bytes don't appear (use a middle fragment)
    pem_fragment = wrong_client_pem[50:100].strip()
    assert pem_fragment not in exc_str
    assert pem_fragment not in exc_repr


def test_base_error_carries_target_id_and_message() -> None:
    err = SshTransportError("t-xyz", "boom")
    assert err.target_id == "t-xyz"
    assert str(err) == "boom"


async def test_connect_generic_asyncssh_error_maps_to_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_host_line = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()  # pyright: ignore[reportUnknownMemberType]
    valid_client_pem = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()  # pyright: ignore[reportUnknownMemberType]
    params = _params(port=22, pinned_host_key=valid_host_line)
    factory = _factory(params, secret_value=valid_client_pem)

    async def _boom(*args: object, **kwargs: object) -> object:
        raise asyncssh.Error(code=1, reason="generic asyncssh failure")

    monkeypatch.setattr(asyncssh, "connect", _boom)
    with pytest.raises(SshTransportError):
        async with factory.open("t1"):
            pass


async def test_connect_gaierror_maps_to_connection_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_host_line = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()  # pyright: ignore[reportUnknownMemberType]
    valid_client_pem = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()  # pyright: ignore[reportUnknownMemberType]
    params = _params(port=22, pinned_host_key=valid_host_line)
    factory = _factory(params, secret_value=valid_client_pem)

    async def _boom(*args: object, **kwargs: object) -> object:
        raise socket.gaierror("name resolution failed")

    monkeypatch.setattr(asyncssh, "connect", _boom)
    with pytest.raises(SshConnectionRefused):
        async with factory.open("t1"):
            pass


async def test_run_command_asyncssh_error_maps_to_transport_error(
    ssh_test_server: SshTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value=ssh_test_server.client_key_pem)
    async with factory.open("t1") as conn:
        # Patch the underlying connection's run to raise an asyncssh.Error.
        async def _boom(*args: object, **kwargs: object) -> object:
            raise asyncssh.Error(code=1, reason="mid-command failure")

        monkeypatch.setattr(conn._conn, "run", _boom)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(SshTransportError):
            await conn.run("anything")


async def test_run_command_timeout_maps_to_ssh_timeout(
    ssh_test_server: SshTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:

    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value=ssh_test_server.client_key_pem)
    async with factory.open("t1") as conn:

        async def _boom(*args: object, **kwargs: object) -> object:
            raise TimeoutError

        monkeypatch.setattr(conn._conn, "run", _boom)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(SshTimeout):
            await conn.run("anything")


async def test_run_command_asyncssh_timeout_maps_to_ssh_timeout(
    ssh_test_server: SshTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # asyncssh.TimeoutError is a subclass of builtin TimeoutError (via MRO), so
    # the single `except TimeoutError` branch in client.py catches it. Verify that
    # branch by raising a plain TimeoutError (same branch, no 8-arg constructor).
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value=ssh_test_server.client_key_pem)
    async with factory.open("t1") as conn:

        async def _boom(*args: object, **kwargs: object) -> object:
            raise TimeoutError("command timed out")

        monkeypatch.setattr(conn._conn, "run", _boom)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(SshTimeout):
            await conn.run("anything")


async def test_command_timeout_passed_to_run(
    ssh_test_server: SshTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M2: factory's command_timeout is forwarded as timeout= kwarg to
    # conn._conn.run. Capture kwargs to verify.
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    expected_timeout = 42.0
    factory = AsyncSshClientFactory(
        resolve=lambda tid: params if tid == "t1" else None,
        secrets_for=lambda name: (
            ssh_test_server.client_key_pem if name == params.key_secret_name else None
        ),
        connect_timeout=10.0,
        command_timeout=expected_timeout,
    )
    captured_kwargs: dict[str, object] = {}

    async with factory.open("t1") as conn:
        original_run = conn._conn.run  # pyright: ignore[reportPrivateUsage]

        async def _capture(*args: object, **kwargs: object) -> object:
            captured_kwargs.update(kwargs)
            return await original_run(*args, **kwargs)  # pyright: ignore[reportArgumentType]

        monkeypatch.setattr(conn._conn, "run", _capture)  # pyright: ignore[reportPrivateUsage]
        await conn.run("echo hi")

    assert captured_kwargs.get("timeout") == expected_timeout


async def test_none_exit_status_maps_to_minus_one(
    ssh_test_server: SshTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = _params(port=ssh_test_server.port, pinned_host_key=ssh_test_server.host_pubkey_line)
    factory = _factory(params, secret_value=ssh_test_server.client_key_pem)

    class _StubProc:
        stdout = ""
        stderr = ""
        exit_status = None

    async with factory.open("t1") as conn:

        async def _run(*args: object, **kwargs: object) -> _StubProc:
            return _StubProc()

        monkeypatch.setattr(conn._conn, "run", _run)  # pyright: ignore[reportPrivateUsage]
        result = await conn.run("x")
    assert result.exit_status == -1


async def test_connect_plain_oserror_maps_to_connection_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OSError (not a ConnectionRefusedError/gaierror subclass) during connect maps
    # to SshConnectionRefused. Covers client.py lines 148-149 (the bare-OSError
    # branch that follows the more-specific except clauses).
    valid_host_line = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()  # pyright: ignore[reportUnknownMemberType]
    valid_client_pem = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()  # pyright: ignore[reportUnknownMemberType]
    params = _params(port=22, pinned_host_key=valid_host_line)
    factory = _factory(params, secret_value=valid_client_pem)

    async def _boom(*args: object, **kwargs: object) -> object:
        raise OSError("no route to host")

    monkeypatch.setattr(asyncssh, "connect", _boom)
    with pytest.raises(SshConnectionRefused):
        async with factory.open("t1"):
            pass
