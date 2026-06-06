"""DrainConsumer — periodic VictoriaLogs → DrainEngine batch consumer (STAGE-004-026).

A CONTINUOUS in-process asyncio service. Every ``interval_seconds`` it:
  1. reads (or cold-start seeds) a single GLOBAL watermark from app_settings
     (key ``drain.cycle_watermark_ms``, value = str(unix_ms));
  2. runs ONE bounded match-all LogsQL query over [watermark_iso, query_end_iso]
     where ``query_end_ms = now_ms - ingest_lag_grace_ms`` (so still-propagating
     lines are not skipped);
  3. streams up to ``batch_max_lines`` lines, feeds each through DrainEngine.add_line
     (which advances per-model cursors), tracking count + MAX(line ts) over the batch;
  4. snapshots the engine and advances the watermark.

Watermark advance rules (Q2):
  - partial = (count == batch_max_lines): more lines remain in the window, so the
    watermark advances only to ``max_ts_seen`` (the newest line streamed) — the next
    cycle resumes mid-window. VL line order is arbitrary, so we MAX over EVERY streamed
    line, NOT the last yielded.
  - complete (count < batch_max_lines, including count == 0): the whole window drained,
    so the watermark advances to ``query_end_ms``.
  - VL failure mid-stream: snapshot what we have, DO NOT advance the watermark (the
    failed window is retried next cycle), return cycle_status="failed".

    The next cycle's query START is built at NANOSECOND precision as
    ``_ns_to_iso(watermark_ms * 1e6 + 1)`` (NOT ``ms_to_iso(watermark_ms)``):
    VL ``_time:[start,end]`` is inclusive on both ends, so resuming AT the watermark
    ms would re-feed every line at that ms. Re-feeding is non-idempotent for
    line_count and drain3 cluster.size, and a same-ms burst >= batch_max_lines at
    the watermark would wedge the cursor forever (max_ts_seen == watermark_ms =>
    no advance). The +1ns advance is below real log-line granularity (no data loss)
    and excludes only the already-processed boundary millisecond.

Cold-start seed (watermark key absent): resume from persistence.get_max_cursor() if
not None; else seed to (now_ms - ingest_lag_grace_ms) so we do NOT replay history.

Cancellation (Q4): no snapshot-on-cancel. run_forever re-raises CancelledError
naturally; start_task/stop_task mirror OverrideLoader.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from homelab_monitor.kernel.logs.histogram import ms_to_iso
from homelab_monitor.kernel.logs.models import from_victorialogs_line

# _ns_to_iso is private but importable, reused intentionally (mirrors tail_service)
from homelab_monitor.kernel.logs.pagination import (
    _ns_to_iso,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.logs.victorialogs_client import VictoriaLogsClientError

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from homelab_monitor.kernel.config import DrainConfig
    from homelab_monitor.kernel.db.repositories.app_settings_repository import (
        AppSettingsRepository,
    )
    from homelab_monitor.kernel.logs.drain_engine import DrainEngine
    from homelab_monitor.kernel.logs.drain_persistence import DrainPersistence
    from homelab_monitor.kernel.logs.victorialogs_client import (
        VictoriaLogsClient,
    )
    from homelab_monitor.kernel.plugins.io import MetricsWriter

from homelab_monitor.kernel.logs.drain_engine import (
    _now_ms,  # pyright: ignore[reportPrivateUsage]
    _parse_iso_ms,  # pyright: ignore[reportPrivateUsage]
)

WATERMARK_KEY: Final[str] = "drain.cycle_watermark_ms"

# Match-all LogsQL: every line in the bounded [start, end] window.
_MATCH_ALL_EXPR: Final[str] = "*"


class CycleInProgressError(Exception):
    """Raised by run_once() when a cycle is already running (re-entrancy guard).

    ``started_at`` is the unix-ms timestamp of the in-flight cycle (or None if the
    guard fired before the start timestamp was recorded — not normally observable).
    """

    def __init__(self, *, started_at: int | None) -> None:
        self.started_at: int | None = started_at
        super().__init__("a drain cycle is already running")


_M_CYCLE_LINES: Final[str] = "homelab_drain_cycle_lines_total"
_M_CYCLE_NEW_TEMPLATES: Final[str] = "homelab_drain_cycle_new_templates_total"
_M_CYCLE_DURATION: Final[str] = "homelab_drain_cycle_duration_seconds"
_M_SIG_COUNT: Final[str] = "homelab_log_signature_count"
_M_SIG_TOTAL: Final[str] = "homelab_log_signature_total"
_M_SIG_FIRST_SEEN: Final[str] = "homelab_log_signature_first_seen_ts"
_M_SIG_CARD_WARN: Final[str] = "homelab_log_signature_cardinality_warn"


@dataclass(frozen=True, slots=True)
class DrainCycleResult:
    """Outcome of one run_once() cycle."""

    started_at: int
    finished_at: int
    lines_processed: int
    new_templates: int
    models_touched: int
    cycle_status: Literal["ok", "partial", "failed"]
    error: str | None


class DrainConsumer:
    """Periodic VictoriaLogs → DrainEngine batch consumer."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        vl_client: VictoriaLogsClient,
        engine: DrainEngine,
        settings: AppSettingsRepository,
        persistence: DrainPersistence,
        config: DrainConfig,
        metrics_writer: MetricsWriter,
        log: BoundLogger,
    ) -> None:
        self._vl_client: VictoriaLogsClient = vl_client
        self._engine: DrainEngine = engine
        self._settings: AppSettingsRepository = settings
        self._persistence: DrainPersistence = persistence
        self._config: DrainConfig = config
        self._metrics_writer: MetricsWriter = metrics_writer
        self._log: BoundLogger = log
        self._task: asyncio.Task[None] | None = None
        self._cycle_lock: asyncio.Lock = asyncio.Lock()
        self._cycle_started_at: int | None = None
        self._cardinality_warned: bool = False

    @property
    def cycle_started_at(self) -> int | None:
        """Unix-ms start of the in-flight cycle, or None when idle."""
        return self._cycle_started_at

    def is_cycle_running(self) -> bool:
        """True iff a run_once() cycle currently holds the cycle lock."""
        return self._cycle_lock.locked()

    # ---- Lifecycle (mirrors OverrideLoader) ----

    def start_task(self) -> None:
        """Launch the periodic cycle task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run_forever(), name="drain_consumer.run")

    async def stop_task(self) -> None:
        """Cancel + await the cycle task."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def run_forever(self) -> None:
        """Run forever, executing one cycle every ``interval_seconds``.

        Re-raises asyncio.CancelledError so the awaiter observes cancellation
        (no snapshot-on-cancel — Q4). Per-cycle exceptions are caught + logged;
        the loop keeps running. run_once() already converts VL errors into a
        "failed" DrainCycleResult, so this backstop only catches UNEXPECTED
        programming errors.
        """
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except CycleInProgressError:
                # A manual /refresh holds the lock; skip this scheduled tick.
                self._log.debug("drain_consumer.cycle_skipped_in_progress")
            except Exception:
                self._log.exception("drain_consumer.cycle_failed")
            await asyncio.sleep(self._config.interval_seconds)

    # ---- One cycle ----

    async def run_once(self) -> DrainCycleResult:
        """Run a single drain cycle. Never raises VictoriaLogsClientError.

        Re-entrancy guard: if a cycle is already running, raises
        CycleInProgressError immediately (no waiting). VL errors are caught and
        returned as cycle_status="failed"; other exceptions propagate to
        run_forever's backstop.
        """
        if self._cycle_lock.locked():  # NO await before the `async with` below.
            raise CycleInProgressError(started_at=self._cycle_started_at)
        async with self._cycle_lock:
            self._cycle_started_at = _now_ms()
            try:
                return await self._run_once_locked(self._cycle_started_at)
            finally:
                self._cycle_started_at = None

    async def _run_once_locked(self, started_at: int) -> DrainCycleResult:
        """Run a single drain cycle with the re-entrancy lock held.

        Never raises VictoriaLogsClientError. VL errors are caught and returned
        as cycle_status="failed" (watermark NOT advanced). Other exceptions
        propagate to run_forever's backstop.
        """
        watermark_ms = await self._read_or_seed_watermark(started_at)
        # Persist the resolved watermark immediately so a freshly seeded/reseeded
        # value survives restarts and the early-return path leaves a valid watermark.
        await self._settings.set(WATERMARK_KEY, str(watermark_ms))
        query_end_ms = started_at - self._config.ingest_lag_grace_seconds * 1000

        # Early return: nothing to query yet (lag grace exceeds elapsed time, or
        # the watermark already covers everything up to query_end).
        if query_end_ms <= watermark_ms:
            finished_at = _now_ms()
            # Early return: no window to query. Emit the cycle counters (0) +
            # duration only; no signatures were evaluated, so skip per-signature
            # gauges + the cardinality gauge.
            self._emit_cycle_metrics(
                count=0,
                new_templates=0,
                started_at=started_at,
                finished_at=finished_at,
                cycle_counts={},
                sig_state={},
                emit_signatures=False,
            )
            return DrainCycleResult(
                started_at=started_at,
                finished_at=finished_at,
                lines_processed=0,
                new_templates=0,
                models_touched=0,
                cycle_status="ok",
                error=None,
            )

        # VL _time:[start,end] is INCLUSIVE on both ends. The watermark is the ms
        # of the newest line already processed last cycle (partial) or query_end
        # (complete). Advance the query start to (watermark_ms in ns) + 1ns so the
        # boundary-ms lines are NOT re-fed: re-feeding is non-idempotent for
        # line_count + drain3 cluster.size (add_line increments both unconditionally),
        # and a same-ms burst at the watermark would otherwise wedge the cursor
        # forever (max_ts_seen == watermark_ms => no advance). +1ns is below real
        # log-line granularity, so it can never skip a real line (no data loss);
        # it only excludes the already-processed boundary millisecond. LIMITATION:
        # a single millisecond holding > batch_max_lines lines loses the lines
        # beyond the cap within that ms (mirrors pagination._BOUNDARY_GROUP_MAX_LINES;
        # not producible by real log sources at batch_max_lines=50_000).
        start_iso = _ns_to_iso(watermark_ms * 1_000_000 + 1)
        end_iso = ms_to_iso(query_end_ms)
        batch_cap = self._config.batch_max_lines

        count = 0
        new_templates = 0
        models_seen: set[str] = set()
        max_ts_seen = watermark_ms  # floor: never regress below the watermark
        cycle_counts: dict[tuple[str, str, str], int] = {}
        sig_state: dict[tuple[str, str], tuple[int, int]] = {}

        try:
            async for vl_line in self._vl_client.stream_query(
                expr=_MATCH_ALL_EXPR,
                start=start_iso,
                end=end_iso,
                limit=batch_cap,
            ):
                line = from_victorialogs_line(vl_line)
                event = await self._engine.add_line(line)
                count += 1
                ts = _parse_iso_ms(line.timestamp)
                max_ts_seen = max(max_ts_seen, ts)
                if event.is_new:
                    new_templates += 1
                models_seen.add(event.model_key)
                severity = line.severity or "unknown"
                ckey = (event.model_key, event.template_hash, severity)
                cycle_counts[ckey] = cycle_counts.get(ckey, 0) + 1
                sig_state[(event.model_key, event.template_hash)] = (
                    event.cluster_size,
                    event.first_seen_ts,
                )
        except VictoriaLogsClientError as exc:
            # Snapshot whatever we processed; DO NOT advance the watermark — the
            # failed window is retried next cycle.
            # snapshot() persists lines fed before the VL failure. If snapshot()
            # itself raises (DB error), it propagates to run_forever's except-Exception
            # backstop — intentional: the cycle is lost but the watermark was NOT
            # advanced, so the next cycle safely retries the same window.
            await self._engine.snapshot()
            self._log.warning("drain_consumer.vl_error", error=str(exc))
            finished_at = _now_ms()
            self._emit_cycle_metrics(
                count=count,
                new_templates=new_templates,
                started_at=started_at,
                finished_at=finished_at,
                cycle_counts=cycle_counts,
                sig_state=sig_state,
                emit_signatures=True,
            )
            return DrainCycleResult(
                started_at=started_at,
                finished_at=finished_at,
                lines_processed=count,
                new_templates=new_templates,
                models_touched=len(models_seen),
                cycle_status="failed",
                error=str(exc),
            )

        await self._engine.snapshot()
        partial = count == batch_cap
        next_watermark = max_ts_seen if partial else query_end_ms
        await self._settings.set(WATERMARK_KEY, str(next_watermark))
        finished_at = _now_ms()
        self._emit_cycle_metrics(
            count=count,
            new_templates=new_templates,
            started_at=started_at,
            finished_at=finished_at,
            cycle_counts=cycle_counts,
            sig_state=sig_state,
            emit_signatures=True,
        )
        return DrainCycleResult(
            started_at=started_at,
            finished_at=finished_at,
            lines_processed=count,
            new_templates=new_templates,
            models_touched=len(models_seen),
            cycle_status="partial" if partial else "ok",
            error=None,
        )

    def _emit_cycle_metrics(  # noqa: PLR0913
        self,
        *,
        count: int,
        new_templates: int,
        started_at: int,
        finished_at: int,
        cycle_counts: dict[tuple[str, str, str], int],
        sig_state: dict[tuple[str, str], tuple[int, int]],
        emit_signatures: bool,
    ) -> None:
        """Emit per-cycle metrics. Shared by ok/partial, failed, and early-return.

        Always emits the cycle counters + duration. Per-signature gauges + the
        cardinality gauge are emitted only when ``emit_signatures`` is True
        (False for the early-return path, where no signatures were evaluated).
        """
        mw = self._metrics_writer
        mw.write_counter(_M_CYCLE_LINES, float(count), {})
        mw.write_counter(_M_CYCLE_NEW_TEMPLATES, float(new_templates), {})
        mw.write_summary(_M_CYCLE_DURATION, (finished_at - started_at) / 1000.0, {})
        if not emit_signatures:
            return
        for (model_key, template_hash, severity), c in cycle_counts.items():
            mw.write_gauge(
                _M_SIG_COUNT,
                float(c),
                {
                    "service_key": model_key,
                    "template_hash": template_hash,
                    "severity": severity,
                },
            )
        for (model_key, template_hash), (size, first_seen) in sig_state.items():
            labels = {"service_key": model_key, "template_hash": template_hash}
            mw.write_gauge(_M_SIG_TOTAL, float(size), labels)
            # first_seen is unix-ms; emit as nanoseconds (ms -> ns).
            mw.write_gauge(_M_SIG_FIRST_SEEN, float(first_seen * 1_000_000), labels)
        distinct = len(sig_state)
        threshold = self._config.signature_cardinality_warn_threshold
        over = distinct > threshold
        mw.write_gauge(_M_SIG_CARD_WARN, 1.0 if over else 0.0, {})
        # Rising-edge log: warn once on the transition into the over-threshold
        # state; reset when it drops back below so a later breach re-warns.
        if over and not self._cardinality_warned:
            self._log.warning(
                "drain_consumer.signature_cardinality_high",
                count=distinct,
                threshold=threshold,
            )
            self._cardinality_warned = True
        elif not over:
            self._cardinality_warned = False

    # ---- Watermark ----

    async def _read_or_seed_watermark(self, now_ms: int) -> int:
        """Return the current watermark (unix-ms), seeding it on cold start.

        Cold start (key absent): resume from persistence.get_max_cursor() if not
        None; else seed to (now_ms - ingest_lag_grace_ms) so we do NOT replay
        history. The seed is persisted immediately by run_once after this returns,
        before the early-return check, so a stable value survives restarts.
        """
        raw = await self._settings.get(WATERMARK_KEY)
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                # Corrupt watermark value — fall through to reseed.
                self._log.warning("drain_consumer.corrupt_watermark", value=raw)
        cursor = await self._persistence.get_max_cursor()
        if cursor is not None:
            return cursor
        return now_ms - self._config.ingest_lag_grace_seconds * 1000


__all__ = ["WATERMARK_KEY", "CycleInProgressError", "DrainConsumer", "DrainCycleResult"]
