"""SSH transport package (STAGE-017-001).

asyncssh-backed ``SshClientFactory`` / ``SshConnection`` impl with pinned
host-key verification, per-target client keys, and typed transport errors.
"""

from __future__ import annotations

from homelab_monitor.kernel.ssh.client import AsyncSshClientFactory
from homelab_monitor.kernel.ssh.config import SshTargetConfig, load_ssh_targets
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

__all__ = [
    "AsyncSshClientFactory",
    "HostKeyMismatch",
    "HostKeyNotPinned",
    "SshAuthError",
    "SshCommandResult",
    "SshConnectionRefused",
    "SshTargetConfig",
    "SshTargetParams",
    "SshTimeout",
    "SshTransportError",
    "load_ssh_targets",
]
