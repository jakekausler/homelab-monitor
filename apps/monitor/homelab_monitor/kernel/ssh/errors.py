"""Typed SSH transport errors (STAGE-017-001).

Unlike the HA client (return-not-raise), the SSH transport RAISES these on a
transport-level failure (connect refused, host-key mismatch, auth failure,
timeout). A non-zero command exit code is NOT a transport failure and does not
raise — it is carried in ``SshCommandResult.exit_status`` for the probe to
interpret.

``HostKeyMismatch`` is deliberately a DISTINCT type: STAGE-017-003 consumes it as
the ``homelab_ssh_host_key_mismatch`` signal and must not see it swallowed into a
generic failure.

SECURITY: error ``message`` values must never contain the private key, the host
key, or any secret — only the target_id and a short reason.
"""

from __future__ import annotations


class SshTransportError(Exception):
    """Base class for all SSH transport failures. Carries the ``target_id``."""

    def __init__(self, target_id: str, message: str) -> None:
        self.target_id = target_id
        super().__init__(message)


class HostKeyNotPinned(SshTransportError):
    """No host key is pinned for the target — refuse to connect (never auto-trust)."""


class HostKeyMismatch(SshTransportError):
    """The server's host key did not match the pinned key."""


class SshConnectionRefused(SshTransportError):
    """Connection refused / host unreachable / DNS failure."""


class SshAuthError(SshTransportError):
    """Authentication failed (key rejected) or no private key available."""


class SshTimeout(SshTransportError):
    """The connection (or command) timed out."""
