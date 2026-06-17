"""``SshProbe`` framework base + outcome types (STAGE-017-003).

``SshProbe`` is the abstract :class:`BaseCollector` subtype that owns the entire SSH
probe lifecycle: open the pinned connection via ``ctx.ssh.open(target_id)``, run the
forced ``command``, hand the captured :class:`SshCommandResult` to the concrete
probe's pure :meth:`SshProbe.parse`, and emit the framework health surface plus any
payload metrics. Concrete probes (STAGE-017-006, the uptime exemplar) supply ONLY
``target_id``, ``command``, and ``parse`` — they never touch ``ctx.vm`` or transport
errors.

SCAFFOLDING: this is the framework base consumed by concrete probes in STAGE-017-006.
The ``command`` / ``target_id`` ClassVars and the ``parse`` method are the subclass
extension points; everything else (emission, ok-semantics, metric counting) is fixed
by the framework here.

Metric surface (see STAGE-017-003 Design):
- ``homelab_ssh_up{target}`` — 1/0, emitted every run.
- ``homelab_ssh_probe_duration_seconds{target,probe}`` — emitted every run.
- ``homelab_ssh_host_key_mismatch{target}`` — 0/1, emitted every run.
- ``homelab_ssh_last_success_age_seconds{target,probe}`` — emitted only once a prior
  ``up==1`` run exists (age = seconds since that run; 0.0 on the first success).
- payload metrics from ``parse().metrics`` — emitted only on the connected+ran path.

Ok-semantics (load-bearing): connected AND command ran -> ``ok=True`` with ``up``
reflecting payload health (1 healthy / 0 sad). Any transport error (refused / auth /
timeout / host-key / unknown) -> ``ok=False``, ``up=0``, no payload. The kernel
``homelab_collector_run_*`` metrics are emitted by the SCHEDULER, not here.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.kernel.ssh.errors import HostKeyMismatch, SshTransportError
from homelab_monitor.kernel.ssh.result import SshCommandResult


@dataclass(frozen=True, slots=True)
class ProbeMetric:
    """One payload metric a concrete probe emits from its parsed output.

    ``labels`` is a required constructor arg (no default) — a mutable default on a
    frozen/slots dataclass is disallowed, and an empty-dict default would be a
    shared-mutable trap. Concrete probes pass ``{}`` explicitly when label-free.
    The ``SshProbe`` base adds the ``target``/``probe`` framework labels itself; do
    NOT duplicate them here.
    """

    name: str
    value: float
    labels: dict[str, str]


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """The pure result of :meth:`SshProbe.parse`.

    ``up`` is the health verdict (True -> ``homelab_ssh_up=1``). ``metrics`` are the
    payload metrics to emit on the connected+ran path (empty list when the probe has
    no payload beyond ``up``).
    """

    up: bool
    metrics: list[ProbeMetric] = field(default_factory=lambda: [])


class SshProbe(BaseCollector):
    """Abstract base for SSH probes — owns open/run/parse/emit/close + ok-semantics.

    Concrete subclasses set ``name``/``interval``/``timeout`` (BaseCollector
    requirements), ``target_id``, optionally ``command``, and implement ``parse``.
    They MUST NOT override ``run`` — the framework lifecycle is fixed here.
    """

    abstract: ClassVar[bool] = True
    # target_id is annotation-only on the base (no value): concrete probes MUST set
    # it. __init_subclass__ enforces only name/interval/timeout, so an
    # annotation-without-value here does not trip enforcement.
    target_id: ClassVar[str]
    command: ClassVar[str] = ""

    def __init__(self) -> None:
        """Initialize per-instance last-success state (scheduler reuses instances)."""
        super().__init__()
        self._last_success_monotonic: float | None = None

    @abstractmethod
    def parse(self, result: SshCommandResult) -> ProbeOutcome:
        """Interpret the captured command output into a :class:`ProbeOutcome`.

        Pure function: no ctx/vm/transport access. ``result.exit_status`` and
        ``result.stdout``/``result.stderr`` are the only inputs. Raising is NOT
        expected — return ``ProbeOutcome(up=False, ...)`` for a sad-but-reachable
        target.
        """
        ...

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Open -> run -> parse -> emit the full health surface. See module docstring."""
        start = time.monotonic()
        up = 0.0
        mismatch = 0.0
        ok = True
        errors: list[str] = []
        outcome: ProbeOutcome | None = None
        try:
            async with ctx.ssh.open(self.target_id) as conn:
                result = await conn.run(self.command)
            outcome = self.parse(result)
            up = 1.0 if outcome.up else 0.0
        except SshTransportError as exc:
            ok = False
            up = 0.0
            mismatch = 1.0 if isinstance(exc, HostKeyMismatch) else 0.0
            errors.append(str(exc))
        finally:
            now = time.monotonic()
            duration = now - start

        metrics_emitted = 0
        target_label = {"target": self.target_id}
        target_probe_label = {"target": self.target_id, "probe": self.name}

        ctx.vm.write_gauge("homelab_ssh_up", up, target_label)
        metrics_emitted += 1
        ctx.vm.write_gauge("homelab_ssh_probe_duration_seconds", duration, target_probe_label)
        metrics_emitted += 1
        ctx.vm.write_gauge("homelab_ssh_host_key_mismatch", mismatch, target_label)
        metrics_emitted += 1

        # last_success_age: success := up == 1.
        # up=1  → this run IS the last healthy moment → age 0.0; update timestamp.
        # up=0  → if a prior success exists, emit elapsed; else omit entirely.
        if up == 1.0:
            self._last_success_monotonic = now
            ctx.vm.write_gauge("homelab_ssh_last_success_age_seconds", 0.0, target_probe_label)
            metrics_emitted += 1
        elif self._last_success_monotonic is not None:
            age = now - self._last_success_monotonic
            ctx.vm.write_gauge("homelab_ssh_last_success_age_seconds", age, target_probe_label)
            metrics_emitted += 1

        # Payload metrics: only on the connected+ran path (outcome is not None).
        if outcome is not None:
            for metric in outcome.metrics:
                ctx.vm.write_gauge(metric.name, metric.value, metric.labels)
                metrics_emitted += 1

        return CollectorResult(
            ok=ok,
            metrics_emitted=metrics_emitted,
            errors=errors,
            events=[],
            duration_seconds=duration,
        )
