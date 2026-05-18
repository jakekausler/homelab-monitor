"""HeartbeatStateCollector — emits per-cron freshness metrics.

STAGE-002-010: Reads crons + heartbeats_state join; emits 6 Prometheus
gauges per cron tracking heartbeat freshness (seconds since ok/fail,
current streak, time to deadline) + wrapper log-scrape evidence.

Filters to non-hidden, non-soft-deleted crons (D7). Skips metrics with NULL
values (never-pinged cron → no streak/duration series, but logscrape counter
always emits as 0). Special logic for @reboot crons (cadence==0): only emit
negative expected_next_seconds if the cron should have run since the last boot.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import text
from sqlalchemy.engine import Row

from homelab_monitor.kernel.metrics.host_boot_time import read_host_btime_dt
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

# Per-family accumulator: metric name -> list of (value, labels) entries.
_FamilyEntries = dict[str, list[tuple[float, dict[str, str]]]]

# All 6 metric families this collector owns. Every family is replace_family'd
# every tick (even with zero entries) so a now-hidden/soft-deleted cron's
# stale label-set children are cleared from the Prometheus registry (D7).
_HEARTBEAT_METRIC_FAMILIES: tuple[str, ...] = (
    "homelab_heartbeat_seconds_since_last_ok",
    "homelab_heartbeat_seconds_since_last_fail",
    "homelab_heartbeat_current_streak",
    "homelab_heartbeat_expected_next_seconds",
    "homelab_heartbeat_last_duration_seconds",
    "homelab_heartbeat_logscrape_count_since_last_heartbeat",
)


def _seconds_since(iso: str | None, now: datetime) -> float | None:
    """Return seconds between iso timestamp and now, or None if iso is None."""
    if iso is None:
        return None
    ts = datetime.fromisoformat(iso)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds()


def _compute_reboot_expected_next(
    last_ok_at: str | None,
    last_discovered_at: str | None,
    host_boot_dt: datetime,
    now: datetime,
) -> float | None:
    """Compute expected_next_seconds for @reboot cron.

    Returns negative (overdue) only if the cron should have run since boot.
    Returns 0.0 if not yet eligible or if already ran since boot.
    """
    should_emit_negative = False
    if last_ok_at is None and last_discovered_at is not None:
        # Never ran: check if discovered before boot
        discovered_ts = datetime.fromisoformat(last_discovered_at)
        if discovered_ts.tzinfo is None:
            discovered_ts = discovered_ts.replace(tzinfo=UTC)
        should_emit_negative = discovered_ts < host_boot_dt
    elif last_ok_at is not None:
        # Has run before: check if last_ok was before boot
        ok_ts = datetime.fromisoformat(last_ok_at)
        if ok_ts.tzinfo is None:
            ok_ts = ok_ts.replace(tzinfo=UTC)
        should_emit_negative = ok_ts < host_boot_dt

    if should_emit_negative:
        return -((now - host_boot_dt).total_seconds())
    return 0.0


class HeartbeatStateCollector(BaseCollector):
    """Emit per-cron freshness metrics from the heartbeats_state table.

    6 metrics per cron (5 share a 5-label set; logscrape is 4-label):
    1. homelab_heartbeat_seconds_since_last_ok (5 labels)
    2. homelab_heartbeat_seconds_since_last_fail (5 labels)
    3. homelab_heartbeat_current_streak (5 labels)
    4. homelab_heartbeat_expected_next_seconds (5 labels)
    5. homelab_heartbeat_last_duration_seconds (5 labels)
    6. homelab_heartbeat_logscrape_count_since_last_heartbeat (4 labels, no cadence_seconds)

    Label set (5 labels): {fingerprint, host, name, cadence_seconds, wrapper_installed}
    Label set (4 labels): {fingerprint, host, name, wrapper_installed}

    Run interval: 30s (equals the vmalert 30s evaluationInterval; freshness
    within an evaluation relies on vmalert's staleness lookback window).
    """

    name: ClassVar[str] = "heartbeat_state"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=10)
    concurrency_group: ClassVar[str] = "heartbeat_state"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def _collect_cron_metrics(
        self,
        families: _FamilyEntries,
        row: Row[Any],
        now: datetime,
        host_boot_dt: datetime | None,
    ) -> tuple[int, list[str]]:
        """Collect metrics for a single cron row. Returns (collected, errors)."""
        collected = 0
        errors: list[str] = []

        try:
            fingerprint = str(row.fingerprint)
            name = str(row.name)
            host = str(row.host)
            cadence_seconds = int(row.cadence_seconds)
            wrapper_installed = bool(row.wrapper_installed)
            last_discovered_at = (
                None if row.last_discovered_at is None else str(row.last_discovered_at)
            )

            # State columns may be NULL if no heartbeats_state row
            last_ok_at = None if row.last_ok_at is None else str(row.last_ok_at)
            last_fail_at = None if row.last_fail_at is None else str(row.last_fail_at)
            current_streak = None if row.current_streak is None else int(row.current_streak)
            expected_next_at = None if row.expected_next_at is None else str(row.expected_next_at)
            last_duration_seconds = (
                None if row.last_duration_seconds is None else float(row.last_duration_seconds)
            )
            logscrape_runs_since_heartbeat = (
                0
                if row.logscrape_runs_since_heartbeat is None
                else int(row.logscrape_runs_since_heartbeat)
            )

            # 5-label set for most metrics
            labels_5 = {
                "fingerprint": fingerprint,
                "host": host,
                "name": name,
                "cadence_seconds": str(cadence_seconds),
                "wrapper_installed": "yes" if wrapper_installed else "no",
            }

            # 4-label set for logscrape (no cadence_seconds)
            labels_4 = {
                "fingerprint": fingerprint,
                "host": host,
                "name": name,
                "wrapper_installed": "yes" if wrapper_installed else "no",
            }

            # Metric 1: seconds since last OK
            secs_since_ok = _seconds_since(last_ok_at, now)
            if secs_since_ok is not None:
                families["homelab_heartbeat_seconds_since_last_ok"].append(
                    (secs_since_ok, labels_5)
                )
                collected += 1

            # Metric 2: seconds since last fail
            secs_since_fail = _seconds_since(last_fail_at, now)
            if secs_since_fail is not None:
                families["homelab_heartbeat_seconds_since_last_fail"].append(
                    (secs_since_fail, labels_5)
                )
                collected += 1

            # Metric 3: current streak
            if current_streak is not None:
                families["homelab_heartbeat_current_streak"].append(
                    (float(current_streak), labels_5)
                )
                collected += 1

            # Metric 4: expected_next_seconds (special logic for @reboot)
            expected_next_value: float | None = None
            if cadence_seconds > 0:
                # Normal interval cron: emit based on expected_next_at
                if expected_next_at is not None:
                    expected_ts = datetime.fromisoformat(expected_next_at)
                    if expected_ts.tzinfo is None:
                        expected_ts = expected_ts.replace(tzinfo=UTC)
                    expected_next_value = (expected_ts - now).total_seconds()
            elif host_boot_dt is not None:
                # @reboot cron: only emit negative (overdue) if should have run since boot
                expected_next_value = _compute_reboot_expected_next(
                    last_ok_at, last_discovered_at, host_boot_dt, now
                )

            if expected_next_value is not None:
                families["homelab_heartbeat_expected_next_seconds"].append(
                    (expected_next_value, labels_5)
                )
                collected += 1

            # Metric 5: last duration seconds
            if last_duration_seconds is not None:
                families["homelab_heartbeat_last_duration_seconds"].append(
                    (last_duration_seconds, labels_5)
                )
                collected += 1

            # Metric 6: logscrape count (always emit, treat NULL as 0)
            families["homelab_heartbeat_logscrape_count_since_last_heartbeat"].append(
                (float(logscrape_runs_since_heartbeat), labels_4)
            )
            collected += 1

        except (ValueError, TypeError) as exc:
            errors.append(f"cron {getattr(row, 'fingerprint', '<unknown>')}: {exc}")

        return collected, errors

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run a single tick. Emits up to 6 metrics per non-hidden/non-soft-deleted cron."""
        start = time.monotonic()
        errors: list[str] = []
        emitted = 0

        now = datetime.now(UTC)
        host_boot_dt = read_host_btime_dt()

        # Query: crons + LEFT JOIN heartbeats_state, filtered to visible crons
        query = text(
            "SELECT "
            "  c.fingerprint, c.name, c.host, c.cadence_seconds, c.wrapper_installed, "
            "  c.last_discovered_at, "
            "  s.last_ok_at, s.last_fail_at, s.current_streak, "
            "  s.expected_next_at, s.last_duration_seconds, "
            "  s.logscrape_runs_since_heartbeat "
            "FROM crons c "
            "LEFT JOIN heartbeats_state s ON s.cron_fingerprint = c.fingerprint "
            "WHERE c.hidden_at IS NULL AND c.soft_deleted_at IS NULL"
        )

        try:
            rows = await ctx.db.fetch_all(query)
        except Exception as exc:
            errors.append(f"query_failed: {exc}")
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=errors,
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Accumulate all entries per family, then atomically replace each
        # family. replace_family clears every existing label-set child before
        # re-emitting — so a cron that became hidden/soft-deleted since the
        # last tick (and is no longer in `rows`) has its stale series dropped.
        # A family with zero entries this tick is replaced with an empty set,
        # clearing all of its prior label-sets.
        families: _FamilyEntries = {fam: [] for fam in _HEARTBEAT_METRIC_FAMILIES}
        for row in rows:
            n, errs = self._collect_cron_metrics(families, row, now, host_boot_dt)
            emitted += n
            errors.extend(errs)

        # replace_family is implemented by the retaining writer AND by
        # MultiplexMetricsWriter (which forwards it to inner writers). Detect
        # the capability by duck-typing rather than a concrete isinstance —
        # in production ctx.vm is a MultiplexMetricsWriter, not the concrete
        # MemoryRetainingMetricsWriter, and an isinstance check silently drops
        # every heartbeat series.
        replacer = getattr(ctx.vm, "replace_family", None)
        if callable(replacer):
            for family_name in _HEARTBEAT_METRIC_FAMILIES:
                replacer(family_name, families[family_name])

        return CollectorResult(
            ok=(len(errors) == 0),
            metrics_emitted=emitted,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )
