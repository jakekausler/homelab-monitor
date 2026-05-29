"""RedactionAuditCollector — 5-min counts-only audit of vector_redactions_total.

STAGE-004-006. Scheduler-registered BaseCollector (mirrors CronRunReconciler /
HeartbeatStateCollector registration in lifespan.py). Queries the VictoriaMetrics
instant endpoint for the redaction counter, diffs per-pattern_type cumulative
against the last-seen snapshot held in memory on the instance, and writes ONE
audit_log row per tick when any pattern fired since the last tick.

D-REDACT-AUDIT-COUNTS-ONLY: writes per-pattern_type {delta, cumulative} ONLY —
never matched values. NEVER logs to VictoriaLogs (avoids redaction-loop
recursion). In-memory last-seen is acceptable: the audit row records the
cumulative, so a process restart (last-seen reset to 0) merely produces one
delta == cumulative row on the first post-restart tick, which is correct
(the audit trail stays monotonic per the cumulative field).
"""

from __future__ import annotations

import os
import time
from datetime import timedelta
from typing import ClassVar, cast

from homelab_monitor.kernel.db.audit import audit_write
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

_VM_TIMEOUT_S = 5.0
_HTTP_OK = 200


class RedactionAuditCollector(BaseCollector):
    """Writes a 5-min counts-only audit row from vector_redactions_total."""

    name: ClassVar[str] = "redaction_audit"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=20)
    concurrency_group: ClassVar[str] = "redaction_audit"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(self) -> None:
        # Per-pattern_type cumulative seen on the previous tick. Survives only
        # in-process; see module docstring for restart semantics.
        self._last_seen: dict[str, float] = {}

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        start = time.monotonic()
        errors: list[str] = []
        vm_url = os.environ.get("HOMELAB_MONITOR_VM_URL", "http://victoriametrics:8428")
        try:
            current = await self._query_counts(ctx, vm_url)
            await self._write_audit(ctx, current)
        except Exception as exc:  # tick isolation — never crash the scheduler loop
            errors.append(f"redaction_audit: {exc}")
            ctx.log.warning("redaction_audit.tick_failed", error=str(exc))

        return CollectorResult(
            ok=(len(errors) == 0),
            metrics_emitted=0,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    async def _query_counts(self, ctx: CollectorContext, vm_url: str) -> dict[str, float]:
        """Query the VM instant endpoint; return {pattern_type: cumulative}."""
        resp = await ctx.http.get(
            f"{vm_url}/api/v1/query",
            params={"query": "vector_redactions_total"},
            timeout=_VM_TIMEOUT_S,
        )
        if resp.status_code != _HTTP_OK:
            msg = f"VM query returned status {resp.status_code}"
            raise RuntimeError(msg)
        body_raw = resp.json()  # pyright: ignore[reportAssignmentType, reportReturnType]
        body = cast(dict[str, object], body_raw) if isinstance(body_raw, dict) else {}
        counts: dict[str, float] = {}
        if body.get("status") != "success":
            return counts
        data_raw = body.get("data")
        data = cast(dict[str, object], data_raw) if isinstance(data_raw, dict) else {}
        result_raw = data.get("result")
        result_list = cast(list[object], result_raw) if isinstance(result_raw, list) else []
        for series in result_list:
            if not isinstance(series, dict):
                continue
            series_dict = cast(dict[str, object], series)
            metric_raw = series_dict.get("metric")
            value_raw = series_dict.get("value")
            metric = cast(dict[str, object], metric_raw) if isinstance(metric_raw, dict) else {}
            if not isinstance(value_raw, list):
                continue
            value_list = cast(list[object], value_raw)
            if len(value_list) < 2:  # noqa: PLR2004
                continue
            pattern_type_raw = metric.get("pattern_type")
            if not isinstance(pattern_type_raw, str):
                continue
            try:
                counts[pattern_type_raw] = float(str(value_list[1]))
            except (TypeError, ValueError):
                continue
        return counts

    async def _write_audit(self, ctx: CollectorContext, current: dict[str, float]) -> None:
        """Diff against last-seen; write ONE counts-only audit row if any delta>0."""
        deltas: dict[str, dict[str, float]] = {}
        for pattern_type, cumulative in current.items():
            previous = self._last_seen.get(pattern_type, 0.0)
            delta = cumulative - previous
            if delta > 0:
                deltas[pattern_type] = {"delta": delta, "cumulative": cumulative}
        # Update last-seen for ALL series (even delta==0) so a counter reset
        # (cumulative < previous) re-baselines without emitting a negative delta.
        self._last_seen = dict(current)
        if not deltas:
            return
        await audit_write(
            ctx.db,
            who="system",
            what="logs.redaction_counts",
            after=deltas,
        )


__all__ = ["RedactionAuditCollector"]
