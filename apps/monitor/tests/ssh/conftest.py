"""Loopback asyncssh server fixture for SSH transport tests (STAGE-017-001).

Stands up a real in-process asyncssh server on 127.0.0.1:0 (OS-assigned port,
xdist-safe) with a generated host key + a generated authorized client key. The
``process_factory`` echoes the requested command (so command-routing assertions
work) and returns a fixed exit status. Tests pin the server's REAL host pubkey to
exercise the accept path, and a DIFFERENT freshly-generated pubkey to exercise the
mismatch path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import asyncssh
import pytest_asyncio


@dataclass(slots=True, frozen=True)
class SshTestServer:
    """Handle to the running loopback server."""

    port: int
    host_pubkey_line: str
    client_key_pem: str


def _handle_process(process: asyncssh.SSHServerProcess) -> None:  # pyright: ignore[reportUnknownParameterType,reportMissingTypeArgument]
    """Echo the requested command (or a default) and exit 0."""
    command = process.command
    if command:
        process.stdout.write(f"ran: {command}\n")  # pyright: ignore[reportUnknownMemberType]
    else:
        process.stdout.write("ran: <shell>\n")  # pyright: ignore[reportUnknownMemberType]
    process.exit(0)


@pytest_asyncio.fixture
async def ssh_test_server() -> AsyncIterator[SshTestServer]:
    """Yield a running loopback SSH server with a generated host + client key."""
    server_host_key = asyncssh.generate_private_key("ssh-ed25519")  # pyright: ignore[reportUnknownMemberType]
    client_key = asyncssh.generate_private_key("ssh-ed25519")  # pyright: ignore[reportUnknownMemberType]
    client_pub_line = client_key.export_public_key().decode()

    acceptor = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_host_keys=[server_host_key],
        authorized_client_keys=asyncssh.import_authorized_keys(client_pub_line),
        process_factory=_handle_process,
    )
    try:
        yield SshTestServer(
            port=acceptor.get_port(),
            host_pubkey_line=server_host_key.export_public_key().decode(),
            client_key_pem=client_key.export_private_key().decode(),
        )
    finally:
        acceptor.close()
        await acceptor.wait_closed()
