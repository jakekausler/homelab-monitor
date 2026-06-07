"""NewSignatureCollector (STAGE-004-035).

Anomaly Type A — "new signature detected". A pure DB reader: each tick it scans
log_signatures and emits homelab_log_signature_new{service_key, template_hash,
severity}=1 for every signature that is (first-seen within the window) AND (not
suppressed) AND (first_seen_severity in the configured in-scope set). A DUMB
vmalert-metrics rule (deploy/vmalert/metrics/log_anomaly.yaml) fires on
homelab_log_signature_new == 1.

Self-resolution: the family is replace_family'd every tick. A signature that ages
out of the window, gets suppressed, or drops out of scope simply stops being
emitted -> its series disappears from the registry -> the alert resolves. No
separate suppression gauge (suppression is folded into this per-tick decision for
atomicity; D-SUPPRESSED-SIGNATURES-NEVER-ALERT).

Self-metric: homelab_collector_run_new_signature{phase, result}.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final

from sqlalchemy import text

from homelab_monitor.kernel.config import NewSignatureConfig
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

_METRIC_NEW: Final[str] = "homelab_log_signature_new"
_SELF_METRIC: Final[str] = "homelab_collector_run_new_signature"
_DEFAULT_INTERVAL_SECONDS: Final[int] = 60
_DEFAULT_TIMEOUT_SECONDS: Final[int] = 20


def _now_ms() -> int:
    """Current unix time in milliseconds (matches log_signatures.first_seen_at units)."""
    return int(time.time() * 1000)


class NewSignatureCollector(BaseCollector):
    """Emit homelab_log_signature_new for in-window, in-scope, unsuppressed signatures."""

    name: ClassVar[str] = "new_signature"
    interval: ClassVar[timedelta] = timedelta(seconds=_DEFAULT_INTERVAL_SECONDS)
    timeout: ClassVar[timedelta] = timedelta(seconds=_DEFAULT_TIMEOUT_SECONDS)
    concurrency_group: ClassVar[str] = "new_signature"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(self, *, config: NewSignatureConfig | None = None) -> None:
        self._config: NewSignatureConfig | None = config

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run a single tick. Emits/refreshes the homelab_log_signature_new family."""
        start = time.monotonic()

        if self._config is None:
            ctx.log.error("new_signature_collector.dependencies_unwired")
            self._emit_self_metric(ctx, phase="tick", result="dependencies_unwired")
            return CollectorResult(
                ok=False,
                metrics_emitted=1,
                errors=["dependencies_unwired"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        window_ms = self._config.window_seconds * 1000
        now_ms = _now_ms()

        try:
            rows = await ctx.db.fetch_all(
                text(
                    "SELECT service_key, template_hash, status, "
                    "  first_seen_at, first_seen_severity "
                    "FROM log_signatures"
                )
            )
        except Exception as exc:
            ctx.log.warning("new_signature_collector.query_failed", error=str(exc))
            self._emit_self_metric(ctx, phase="tick", result="error")
            return CollectorResult(
                ok=False,
                metrics_emitted=1,
                errors=[f"query_failed: {exc}"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        entries: list[tuple[float, dict[str, str]]] = []
        for row in rows:
            status = str(row.status)  # pyright: ignore[reportAttributeAccessIssue]
            if status == "suppressed":
                continue
            raw_sev = row.first_seen_severity  # pyright: ignore[reportAttributeAccessIssue]
            if raw_sev is None:
                continue
            severity = str(raw_sev)
            if severity not in self._config.severities:
                continue
            first_seen_at = int(row.first_seen_at)  # pyright: ignore[reportAttributeAccessIssue]
            if now_ms - first_seen_at > window_ms:
                continue
            entries.append(
                (
                    1.0,
                    {
                        "service_key": str(row.service_key),  # pyright: ignore[reportAttributeAccessIssue]
                        "template_hash": str(row.template_hash),  # pyright: ignore[reportAttributeAccessIssue]
                        "severity": severity,
                    },
                )
            )

        # Emit the family fresh each tick. replace_family clears every prior
        # label-set child before re-emitting -> aged-out / newly-suppressed
        # signatures disappear (natural self-resolution). Duck-typed because
        # production ctx.vm is a MultiplexMetricsWriter, not the concrete
        # MemoryRetainingMetricsWriter; an isinstance check would silently drop
        # every series.
        replacer = getattr(ctx.vm, "replace_family", None)
        if callable(replacer):
            replacer(_METRIC_NEW, entries)

        self._emit_self_metric(ctx, phase="tick", result="ok")
        return CollectorResult(
            ok=True,
            metrics_emitted=len(entries) + 1,  # family entries + self-metric
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    @staticmethod
    def _emit_self_metric(ctx: CollectorContext, *, phase: str, result: str) -> None:
        ctx.vm.write_gauge(_SELF_METRIC, 1.0, {"phase": phase, "result": result})


__all__ = ["NewSignatureCollector"]
