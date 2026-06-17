"""Per-target SSH connection parameters (STAGE-017-001).

The ``resolve`` seam on ``AsyncSshClientFactory`` maps a ``target_id`` to one of
these. STAGE-017-002 supplies the real config-backed resolver; this stage's tests
use a hand-built dict-backed lambda. No asyncssh import (kept dependency-light so
config/resolver code can import it freely).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True, frozen=True)
class SshTargetParams:
    """Everything needed to open + authenticate one SSH target.

    ``pinned_host_key`` is the target's OpenSSH **public-key line** (e.g.
    ``"ssh-ed25519 AAAA..."``). ``None``/empty means the host key has not been
    pinned yet (pre-capture) and the target is correctly unprobeable: ``open()``
    raises ``HostKeyNotPinned`` before any connection attempt — NEVER auto-trust.

    ``key_secret_name`` is the secret name under which the per-target private key
    PEM is stored; the factory reads it via its ``secrets_for`` seam.

    ``account_mode`` distinguishes an appliance login (UDM/Synology shell) from a
    dedicated monitoring user; carried for downstream stages, unused here.
    """

    host: str
    port: int
    user: str
    key_secret_name: str
    pinned_host_key: str | None
    account_mode: Literal["appliance", "dedicated_user"]
