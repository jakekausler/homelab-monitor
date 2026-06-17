"""Typed result of a single SSH command run (STAGE-017-001).

Split into its own module (no asyncssh import) so the ``SshConnection`` Protocol
in ``kernel/plugins/io.py`` and the future SshProbe base can annotate against it
WITHOUT importing asyncssh — avoiding an import cycle and keeping asyncssh out of
the kernel's hot import path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SshCommandResult:
    """The captured output of one SSH command.

    A non-zero ``exit_status`` is NOT an error: the probe interprets exit codes.
    Transport failures (connect/auth/host-key/timeout) raise instead (see
    ``kernel.ssh.errors``). ``exit_status`` is ``-1`` when asyncssh reports
    ``None`` (e.g. the remote was killed by a signal with no numeric exit).
    """

    stdout: str
    stderr: str
    exit_status: int
