"""`uptime` exemplar SSH probe (EPIC-017).

The end-to-end framework exemplar: a trivial SshProbe that emits seconds-since-boot
for any configured ssh_target, in BOTH account-modes (appliance + dedicated-user).
The framework ships ONLY this trivial exemplar; real consumer probes (Unifi lease —
EPIC-007; Synology DSM SMART/btrfs — EPIC-008) come later. Target ids are NOT
hard-coded here — the operator declares targets in ssh_targets (the public repo stays
target-id-agnostic); registration synthesizes one probe per configured target.
"""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar

from homelab_monitor.kernel.ssh.probe import ProbeMetric, ProbeOutcome, SshProbe
from homelab_monitor.kernel.ssh.result import SshCommandResult


class UptimeProbe(SshProbe):
    """Generic ``/proc/uptime`` exemplar probe.

    ``target_id`` / ``name`` / ``concurrency_group`` are set per-target by
    :func:`make_uptime_probe`; ``parse`` is pure (no clock).
    """

    abstract: ClassVar[bool] = True  # not itself registered; factory makes concretes
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=10)
    command: ClassVar[str] = "cat /proc/uptime"  # advisory; forced-command targets ignore it

    def parse(self, result: SshCommandResult) -> ProbeOutcome:
        if result.exit_status != 0:
            return ProbeOutcome(up=False, metrics=[])
        try:
            seconds = float(result.stdout.split()[0])
        except (ValueError, IndexError):
            return ProbeOutcome(up=False, metrics=[])
        return ProbeOutcome(
            up=True,
            metrics=[
                ProbeMetric(
                    name="homelab_ssh_uptime_seconds",
                    value=seconds,
                    labels={"target": self.target_id},
                )
            ],
        )


def make_uptime_probe(target_id: str) -> type[UptimeProbe]:
    """Synthesize a registerable per-target UptimeProbe subclass.

    Uses ``type()`` so no target ids are hard-coded in the public repo.
    The returned class:
    - has ``abstract=False`` (passes BaseCollector enforcement)
    - inherits ``interval``, ``timeout``, ``command``, ``parse`` from UptimeProbe
    - sets ``name``, ``target_id``, ``concurrency_group`` for this target
    """
    return type(  # pyright: ignore[reportReturnType]
        f"UptimeProbe_{target_id}",
        (UptimeProbe,),
        {
            "abstract": False,
            "name": f"uptime-{target_id}",
            "target_id": target_id,
            "concurrency_group": f"ssh_{target_id}",
        },
    )
