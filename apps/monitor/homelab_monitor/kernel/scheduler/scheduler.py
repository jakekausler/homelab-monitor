"""The async tick scheduler.

Per-collector ``asyncio.Task`` with absolute-deadline tick loops. Three
``run_kind`` dispatch arms (ASYNC, THREAD, PROCESS). Five scheduler-owned
self-metrics emitted unconditionally per tick. Graceful shutdown with
``shutdown_total`` outcome distinct from ``failure_total``.

Spec references: STAGE-001-007.md §"Decisions in this stage" (D1-D5),
``docs/superpowers/specs/2026-05-04-homelab-monitor-design.md`` §3.1, §5.4.

Updated by STAGE-001-008 with concurrency-group locks, quarantine gate via
``FailureBudget``, and the 6th self-metric ``homelab_collector_run_skipped_total``.
See ``docs/architecture/scheduler.md`` §5-§6.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from structlog.contextvars import bound_contextvars

from homelab_monitor.kernel.alerts.events import AlertResolvedEvent
from homelab_monitor.kernel.alerts.fingerprinting import quarantine_fingerprint
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.events import (
    EventSink,
    NullEventSink,
    SchedulerTickEvent,
    TriggerContext,
    reset_current_tick,
    set_current_tick,
)
from homelab_monitor.kernel.plugins.base import Collector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import MetricEntry, MetricsWriter
from homelab_monitor.kernel.plugins.loader import LoadedCollector
from homelab_monitor.kernel.plugins.process_context import (
    BufferingMetricsWriter,
    ProcessCollectorContext,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig, CollectorResult, RunKind
from homelab_monitor.kernel.scheduler.failure_budget import FailureBudget

if TYPE_CHECKING:
    from homelab_monitor.kernel.alerts.repository import AlertRepository
    from homelab_monitor.kernel.dispatch.dispatcher import AlertDispatcher


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """Knobs for :class:`Scheduler`.

    - ``shutdown_grace_seconds`` — max time :meth:`Scheduler.stop` waits for
      in-flight ticks to complete before forced cancellation. Default 30s.
    - ``process_pool_size`` — workers in the shared ``ProcessPoolExecutor``.
      Default ``min(2, os.cpu_count() or 1)`` (matches stage decision).
    - ``thread_pool_size`` — workers in the shared ``ThreadPoolExecutor``.
      Bounded; THREAD plugins are rare initially. Default 4.
    - ``event_sink`` — EventSink implementation for publishing tick events
      (e.g., SSE broker). Default NullEventSink (no-op).
    """

    shutdown_grace_seconds: float = 30.0
    process_pool_size: int = field(
        default_factory=lambda: min(2, os.cpu_count() or 1),
    )
    thread_pool_size: int = 4
    event_sink: EventSink = field(default_factory=NullEventSink)


def _thread_runner(collector: Collector, ctx: CollectorContext) -> CollectorResult:
    """Drive an async collector's ``run`` from inside a worker thread.

    Module-level (NOT a method, NOT a closure) for symmetry with
    :func:`_process_runner` (which DOES require module-level for pickling).
    ThreadPoolExecutor does not pickle, so this is stylistic, not load-bearing.

    Receives the SAME collector instance the scheduler has, preserving any
    state the collector keeps on ``self`` across ticks (e.g., reused HTTP
    clients, last-seen caches). PROCESS mode cannot do this because workers
    are separate processes.
    """
    return asyncio.run(collector.run(ctx))


def _process_runner(
    collector_cls: type[Collector],
    ctx: ProcessCollectorContext,
) -> tuple[CollectorResult, list[MetricEntry]]:
    """Drive a collector's ``run`` inside a worker process.

    The worker receives the picklable :class:`ProcessCollectorContext`, NOT
    the live :class:`CollectorContext`. PROCESS collectors are responsible
    for not accessing ``db``/``http``/``ssh``/``vl``/``log``/``ha`` (those
    aren't on the narrow context). The ``# type: ignore[arg-type]`` below
    acknowledges (does not enforce) the mismatch between the Protocol's
    ``CollectorContext`` parameter and the narrow ``ProcessCollectorContext``
    actually delivered. A separate ``ProcessCollector`` Protocol (TODO in
    process_context.py) is the proper fix.

    Returns ``(result, drained_metrics)``; the parent process replays the
    drained metrics through the real :class:`MetricsWriter`.
    """
    collector = collector_cls()  # pragma: no cover
    # PROCESS collectors accept the narrow context; future work (post-009) may
    # introduce a separate ProcessCollector Protocol with the right signature.
    result = asyncio.run(collector.run(ctx))  # type: ignore[arg-type]  # pragma: no cover
    return result, ctx.metrics.drain()  # pragma: no cover


_LAST_REASON_PREFIX = "(last reason: "
_LAST_REASON_SUFFIX = ")"


def _extract_last_reason(quarantine_reason: str) -> str | None:
    """Extract the bare failure-kind from a FailureBudget quarantine reason string.

    ``FailureBudget.record_failure`` formats the reason as::

        "consecutive failures: <n> (last reason: <kind>)"

    where ``<kind>`` is one of ``timeout`` / ``exception`` / ``result_error``.
    This helper returns ``<kind>`` so the scheduler can recompute the
    quarantine fingerprint without depending on json_extract scans over
    scheduler-sourced rows.

    Returns ``None`` if the reason string does not match the expected format
    (defensive: never raise from inside the resolve path).

    NOTE: Uses str.rfind to extract the LAST occurrence of "(last reason: ".
    Callers in this codebase pass kind values from a known set
    (timeout/exception/result_error/group_busy/quarantined), so the inner
    content cannot itself contain the marker. If a future caller passes a
    free-form reason, rfind would extract the inner-most occurrence — not
    necessarily what's intended.
    """
    start = quarantine_reason.rfind(_LAST_REASON_PREFIX)
    if start < 0:
        return None
    inner_start = start + len(_LAST_REASON_PREFIX)
    if not quarantine_reason.endswith(_LAST_REASON_SUFFIX):
        return None
    inner_end = len(quarantine_reason) - len(_LAST_REASON_SUFFIX)
    if inner_end <= inner_start:
        return None
    return quarantine_reason[inner_start:inner_end]


class Scheduler:
    """Per-collector asyncio.Task tick driver.

    Lifecycle:

    - Construct with the loaded collectors, a context factory, the self-metrics
      writer, and an optional :class:`SchedulerConfig`.
    - :meth:`start` spawns one tick-loop task per collector + creates the
      thread/process pools.
    - :meth:`stop` cancels tick loops, waits up to ``shutdown_grace_seconds``
      for in-flight ticks, then shuts down the pools.
    - :attr:`running` reflects current state.

    The scheduler is NOT reusable — call :meth:`start` once, :meth:`stop`
    once. A second :meth:`start` raises :class:`RuntimeError`.

    PROCESS-mode resilience caveat: a worker hard-crash (e.g., ``os._exit``)
    breaks the underlying ``ProcessPoolExecutor``; subsequent PROCESS ticks fail
    with ``BrokenProcessPool`` until scheduler restart. STAGE-001-008's failure
    budget will quarantine the offender; pool rebuild on broken state is
    deferred. ASYNC and THREAD modes are unaffected.
    """

    def __init__(  # noqa: PLR0913
        self,
        loaded: list[LoadedCollector],
        ctx_factory: Callable[[Collector], CollectorContext],
        self_metrics: MetricsWriter,
        config: SchedulerConfig | None = None,
        failure_budget: FailureBudget | None = None,
        *,
        alert_repo: AlertRepository | None = None,
        alert_dispatcher: AlertDispatcher | None = None,
    ) -> None:
        """Stash dependencies; do NOT touch the event loop here.

        ``ctx_factory`` is a callable the scheduler invokes once per tick to
        produce the :class:`CollectorContext` handed to the collector's ``run``
        method. The factory pattern lets STAGE-001-010 wire real DB / HTTP /
        log handles without the scheduler caring about construction details.

        ``alert_repo`` and ``alert_dispatcher`` (STAGE-001-013) are optional
        and only used by :meth:`clear_quarantine` to mark the corresponding
        active alert resolved + dispatch ``AlertResolvedEvent``.
        """
        self._loaded: list[LoadedCollector] = list(loaded)
        self._ctx_factory: Callable[[Collector], CollectorContext] = ctx_factory
        self._self_metrics: MetricsWriter = self_metrics
        self._config: SchedulerConfig = config if config is not None else SchedulerConfig()
        self._failure_budget: FailureBudget | None = failure_budget
        self._alert_repo: AlertRepository | None = alert_repo
        self._alert_dispatcher: AlertDispatcher | None = alert_dispatcher
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._configs: dict[str, CollectorConfig] = {lc.collector.name: lc.config for lc in loaded}

        self._loop: asyncio.AbstractEventLoop | None = None
        self._tick_tasks: list[asyncio.Task[None]] = []
        self._inflight: set[asyncio.Task[None]] = set()
        self._stopping: bool = False
        self._last_error_ts: dict[str, float] = {}
        self._thread_pool: ThreadPoolExecutor | None = None
        self._process_pool: ProcessPoolExecutor | None = None
        self._immediate_runs: dict[str, asyncio.Queue[tuple[str, TriggerContext]]] = {}
        self._pending_awaitables: dict[str, asyncio.Future[CollectorResult | None]] = {}

    @property
    def running(self) -> bool:
        """Return ``True`` between :meth:`start` and :meth:`stop`.

        Note: a scheduler with zero collectors returns ``False`` even after a
        successful :meth:`start`, because ``bool([]) is False``.
        """
        return bool(self._tick_tasks) and not self._stopping

    @property
    def failure_budget(self) -> FailureBudget | None:
        """Return the failure budget instance (read-only)."""
        return self._failure_budget

    async def start(self) -> None:
        """Spawn per-collector tick loops + create the thread/process pools.

        Raises:
            RuntimeError: if called twice without an intervening :meth:`stop`.
        """
        if self._tick_tasks:
            msg = "Scheduler already started"
            raise RuntimeError(msg)
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        if self._failure_budget is not None:
            await self._failure_budget.load_state()
        self._thread_pool = ThreadPoolExecutor(max_workers=self._config.thread_pool_size)
        self._process_pool = ProcessPoolExecutor(
            max_workers=self._config.process_pool_size,
            mp_context=multiprocessing.get_context("forkserver"),
        )
        self._tick_tasks = [
            self._loop.create_task(self._run_collector(lc.collector)) for lc in self._loaded
        ]

    async def stop(self) -> None:
        """Gracefully shut down: cancel tick loops, drain in-flight ticks, kill pools.

        Tick-loop cancellations propagate as :class:`asyncio.CancelledError`
        which the loops swallow at top level. In-flight ``_tick`` tasks see
        :attr:`_stopping` and convert their CancelledError into a
        ``shutdown_total`` increment (NOT ``failure_total``). After
        ``shutdown_grace_seconds`` we forcibly cancel anything still inflight.
        """
        self._stopping = True
        for t in self._tick_tasks:
            t.cancel()
        # Wait for tick-loop tasks to exit (they shouldn't raise; CancelledError is swallowed).
        if self._tick_tasks:
            await asyncio.gather(*self._tick_tasks, return_exceptions=True)
        # Cancel all in-flight ticks immediately so they reach the CancelledError
        # path in _tick (which emits shutdown_total). Then wait up to the grace
        # window for their finally-blocks and done-callbacks to complete.
        if self._inflight:
            for t in list(self._inflight):
                if not t.done():  # pragma: no cover
                    t.cancel()
            try:
                async with asyncio.timeout(self._config.shutdown_grace_seconds):
                    await asyncio.gather(*self._inflight, return_exceptions=True)
            except (
                TimeoutError
            ):  # pragma: no cover -- defensive: cancellation already propagated above
                for t in list(self._inflight):
                    if not t.done():  # pragma: no cover
                        t.cancel()
                await asyncio.gather(*self._inflight, return_exceptions=True)
        if self._thread_pool is not None:  # pragma: no cover
            self._thread_pool.shutdown(wait=False, cancel_futures=True)
            self._thread_pool = None
        if self._process_pool is not None:  # pragma: no cover
            self._process_pool.shutdown(wait=False, cancel_futures=True)
            self._process_pool = None
        self._tick_tasks.clear()

    async def clear_quarantine(self, name: str, by: str = "operator") -> None:
        """Manually clear a collector's quarantine.

        Thin delegation to the FailureBudget. STAGE-001-010's
        ``POST /api/collectors/{name}/retry`` endpoint will call this with the
        authenticated user's identifier as ``by``.

        STAGE-001-013 additionally marks any active scheduler-sourced
        quarantine alert for this collector as resolved + dispatches an
        ``AlertResolvedEvent``. Skipped silently when alert_repo/dispatcher
        are not wired.

        Args:
            name: Collector name.
            by: Actor identifier for audit purposes.

        Raises:
            RuntimeError: If the scheduler was constructed without a
                ``FailureBudget``.
        """
        if self._failure_budget is None:
            msg = "Scheduler was constructed without a FailureBudget"
            raise RuntimeError(msg)

        # Reorder per F4/F5: resolve the alert FIRST (using the still-set
        # quarantine_reason to compute the fingerprint), THEN clear the
        # budget. mark_resolved is idempotent (rowcount-guarded), so an
        # operator retry after a partial failure does not double-resolve
        # or double-audit. Lookup by fingerprint (cheap, indexed) instead
        # of json_extract scan over scheduler-sourced rows.
        if self._alert_repo is not None and self._alert_dispatcher is not None:
            qstate = self._failure_budget.quarantine_state(name)
            if qstate is not None and qstate.quarantine_reason is not None:
                # Reason format from FailureBudget.record_failure:
                #   "consecutive failures: <n> (last reason: <kind>)"
                # _emit_quarantine_alert was given the bare <kind> as reason,
                # so we extract it back out for fingerprinting.
                last_reason = _extract_last_reason(qstate.quarantine_reason)
                if last_reason is not None:
                    fp = quarantine_fingerprint(name, last_reason)
                    active = await self._alert_repo.find_active_by_fingerprint(fp)
                    if active is not None:
                        ts = utc_now_iso()
                        await self._alert_repo.mark_resolved(active.id, ts)
                        event = AlertResolvedEvent(
                            alert_id=active.id,
                            fingerprint=active.fingerprint,
                            source_tool="scheduler",
                            severity=active.severity,
                            resolved_at=ts,
                            labels=active.labels,
                            annotations=active.annotations,
                            ts=ts,
                        )
                        await self._alert_dispatcher.dispatch(event)

        await self._failure_budget.clear_quarantine(name, by=by)

    async def request_immediate_run(self, name: str, trigger: TriggerContext) -> str:
        """Enqueue an out-of-band run for ``name``. Returns the future tick_id.

        The next iteration of the per-collector tick loop will dequeue the
        request, attach the trigger to the tick, and run through the full
        pipeline (lock → timeout → failure budget → event sink). The next
        scheduled tick is deferred by one interval to avoid back-to-back.

        Args:
            name: Collector name.
            trigger: TriggerContext describing what initiated the run.

        Returns:
            tick_id: The ID of the future tick (generated up-front so caller
                can correlate with events).

        Raises:
            KeyError: If ``name`` is not a known collector.
        """
        if name not in self._configs:
            msg = f"unknown collector: {name}"
            raise KeyError(msg)
        q = self._immediate_runs.setdefault(name, asyncio.Queue())
        tick_id = uuid4().hex
        await q.put((tick_id, trigger))
        return tick_id

    async def await_immediate_run(
        self, name: str, *, trigger: TriggerContext, timeout: float = 30.0
    ) -> CollectorResult | None:
        """Enqueue an out-of-band run for ``name`` and await its completion.

        Semantics:
        - Same as ``request_immediate_run`` (enqueues a run for collector ``name``).
        - BUT awaits until the run completes (or timeout).
        - Returns the CollectorResult (or None on timeout).

        The run goes through the full pipeline (lock → timeout → failure budget →
        event sink) with the same semantics as a scheduled tick.

        Args:
            name: Collector name.
            trigger: TriggerContext describing what initiated the run.
            timeout: Max seconds to wait for completion. Default 30.0.

        Returns:
            CollectorResult if the run completes within timeout, None on timeout.

        Raises:
            KeyError: If ``name`` is not a known collector.
        """
        if name not in self._configs:
            msg = f"unknown collector: {name}"
            raise KeyError(msg)

        assert self._loop is not None
        q = self._immediate_runs.setdefault(name, asyncio.Queue())
        tick_id = uuid4().hex
        future: asyncio.Future[CollectorResult | None] = self._loop.create_future()
        self._pending_awaitables[tick_id] = future
        try:
            await q.put((tick_id, trigger))
            try:
                result = await asyncio.wait_for(future, timeout=timeout)
                return result
            except TimeoutError:
                return None
        finally:
            self._pending_awaitables.pop(tick_id, None)

    def _signal_awaitable_done(self, tick_id: str, result: CollectorResult | None) -> None:
        """Resolve a pending await_immediate_run future if any exists for this tick.

        Called from early-return paths in _tick to wake up any caller waiting in
        await_immediate_run. Passes the CollectorResult on success or None on
        early failure (quarantine, group lock timeout, timeout, exception, etc).
        The future receiver gracefully handles None (lifespan code already has
        try/except wrappers).
        """
        future = self._pending_awaitables.pop(tick_id, None)
        if future is not None and not future.done():
            future.set_result(result)

    # --- Per-collector tick loop -----------------------------------------------------

    async def _run_collector(self, c: Collector) -> None:
        """Per-collector tick loop with absolute deadlines + drift skip-forward.

        Initial offset: ``hash(c.name) % max(1, int(interval))`` (decorrelates
        same-interval collectors). Sub-second intervals get offset = 0.

        After each scheduled tick, ``deadline += interval``. If ``deadline``
        has fallen behind ``now`` (because a tick took longer than its
        interval, or the loop was starved), we **skip forward** to
        ``now + interval`` rather than firing back-to-back catch-up ticks.

        Tick execution is fire-and-track: ``inflight = create_task(_tick(c))``
        (NOT awaited). The next sleep_until uses the new deadline immediately;
        the inflight task lifecycles itself out of ``self._inflight`` via the
        done callback. This gives spec-mandated tick precision regardless of
        tick duration (within the safety net of asyncio's own scheduling).

        Note on the offset formula: ``hash(name) % max(1, int(interval))`` produces
        ``offset == 0`` for any interval in ``[0, 2.0)``, because ``int(1.x) == 1``
        and ``hash % 1 == 0``. Decorrelation only kicks in for intervals ≥ 2.0
        seconds. Acceptable since most production intervals are integer seconds; if
        non-integer fractional intervals become common, switch to a millisecond-
        precision modulus.
        """
        assert self._loop is not None  # set in start()
        interval = c.interval.total_seconds()
        offset = float(hash(c.name) % max(1, int(interval))) if interval >= 1 else 0.0
        deadline = self._loop.time() + offset
        # Poll period for immediate-run queue: short enough to make retry feel
        # interactive, but not so short it spins. 50ms is a reasonable default.
        immediate_poll_seconds = min(0.05, interval)
        try:
            while (
                not self._stopping
            ):  # pragma: no branch  -- exits via CancelledError, not loop predicate
                now = self._loop.time()
                if (
                    deadline + interval < now
                ):  # pragma: no cover -- drift > 1 interval is timing-dependent
                    # Drift > 1 interval: skip forward; do NOT catch up.
                    deadline = now + interval

                # Sleep until either the next scheduled deadline OR the next
                # immediate-poll wake-up, whichever comes first. This lets immediate
                # runs fire promptly without waiting for the full interval.
                wait_seconds = max(0.0, min(deadline - now, immediate_poll_seconds))
                await asyncio.sleep(wait_seconds)
                if self._stopping:  # pragma: no branch -- mid-tick stop is timing-race;
                    # not deterministically testable
                    break  # pragma: no cover -- timing-race, not testable

                # Check immediate-run queue first; drain ALL pending entries before
                # considering the scheduled tick so a burst of retries doesn't get
                # interleaved with scheduled fire.
                q = self._immediate_runs.get(c.name)
                drained_immediate = False
                if q is not None:
                    while True:
                        try:
                            tick_id, trigger = q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        inflight = self._loop.create_task(
                            self._tick(c, trigger=trigger, predetermined_tick_id=tick_id)
                        )
                        self._inflight.add(inflight)
                        inflight.add_done_callback(self._inflight.discard)
                        drained_immediate = True
                if drained_immediate:
                    deadline = self._loop.time() + interval
                    continue

                # No immediate runs pending; only fire the scheduled tick if the
                # scheduled deadline has actually arrived.
                if self._loop.time() < deadline:
                    continue

                inflight = self._loop.create_task(self._tick(c))
                self._inflight.add(inflight)
                inflight.add_done_callback(self._inflight.discard)
                deadline += interval
        except asyncio.CancelledError:
            return  # Cancellation from stop() is the normal exit path.

    # --- Single tick -----------------------------------------------------------------

    async def _tick(  # noqa: PLR0912, PLR0915  -- explicit catch order required by spec
        self,
        c: Collector,
        trigger: TriggerContext | None = None,
        predetermined_tick_id: str | None = None,
    ) -> None:
        """Run one tick: gate on quarantine, acquire group lock, build ctx, dispatch,
        classify outcome, emit self-metrics, record failure-budget state.

        Catch order is exact (CancelledError -> TimeoutError -> Exception);
        do NOT swap them. The lock-acquisition timeout is OUTSIDE the inner
        ``asyncio.timeout()`` block so its TimeoutError is distinguishable from
        a tick's own timeout.

        Args:
            c: Collector to run.
            trigger: TriggerContext if this is an immediate run (request_immediate_run).
            predetermined_tick_id: Tick ID (pre-allocated by request_immediate_run).
                If None, a fresh ID is generated.
        """
        assert self._loop is not None

        # Generate tick_id if not provided by request_immediate_run
        tick_id = predetermined_tick_id if predetermined_tick_id is not None else uuid4().hex

        # Default to "scheduled" trigger if not provided
        if trigger is None:
            trigger = TriggerContext(kind="scheduled", request_id=None)

        # Bind tick context to structlog contextvars for this tick
        token = set_current_tick(tick_id, trigger)

        try:
            ctx_kwargs: dict[str, object] = {
                "tick_id": tick_id,
                "collector": c.name,
                "trigger_kind": trigger.kind,
            }
            if trigger.request_id is not None:
                ctx_kwargs["request_id"] = trigger.request_id
            with bound_contextvars(**ctx_kwargs):
                # Quarantine gate.
                if self._failure_budget is not None and self._failure_budget.is_quarantined(c.name):
                    self._self_metrics.write_counter(
                        "homelab_collector_run_skipped_total",
                        1.0,
                        {"name": c.name, "reason": "quarantined"},
                    )
                    await self._publish_event(
                        SchedulerTickEvent(
                            collector=c.name,
                            tick_id=tick_id,
                            outcome="skipped",
                            reason="quarantined",
                            trigger_kind=trigger.kind,
                            request_id=trigger.request_id,
                            ts=utc_now_iso(),
                        )
                    )
                    self._signal_awaitable_done(tick_id, None)
                    return

                # Group lock acquisition with interval/2 deadline. Outside the inner
                # timeout block so the lock-acquisition TimeoutError is distinguishable
                # from the tick's own timeout.
                group_key = c.name if c.concurrency_group == "default" else c.concurrency_group
                lock = self._group_locks.setdefault(group_key, asyncio.Lock())
                try:
                    await asyncio.wait_for(
                        lock.acquire(),
                        timeout=c.interval.total_seconds() / 2,
                    )
                except TimeoutError:
                    self._self_metrics.write_counter(
                        "homelab_collector_run_skipped_total",
                        1.0,
                        {"name": c.name, "reason": "group_busy"},
                    )
                    await self._publish_event(
                        SchedulerTickEvent(
                            collector=c.name,
                            tick_id=tick_id,
                            outcome="skipped",
                            reason="group_busy",
                            trigger_kind=trigger.kind,
                            request_id=trigger.request_id,
                            ts=utc_now_iso(),
                        )
                    )
                    self._signal_awaitable_done(tick_id, None)
                    return

                # Lock held; main tick body wrapped in try/finally for guaranteed release.
                try:
                    start = self._loop.time()
                    ctx = self._ctx_factory(c)

                    try:
                        async with asyncio.timeout(c.timeout.total_seconds()):
                            result = await self._dispatch(c, ctx)
                    except asyncio.CancelledError:
                        if self._stopping:
                            duration = self._loop.time() - start
                            self._self_metrics.write_counter(
                                "homelab_collector_run_shutdown_total",
                                1.0,
                                {"name": c.name},
                            )
                            await self._publish_event(
                                SchedulerTickEvent(
                                    collector=c.name,
                                    tick_id=tick_id,
                                    outcome="shutdown",
                                    duration_seconds=duration,
                                    trigger_kind=trigger.kind,
                                    request_id=trigger.request_id,
                                    ts=utc_now_iso(),
                                )
                            )
                            self._signal_awaitable_done(tick_id, None)
                            return
                        raise  # pragma: no cover -- not reachable via Scheduler.stop()
                    except TimeoutError:
                        duration = self._loop.time() - start
                        self._self_metrics.write_counter(
                            "homelab_collector_run_failure_total",
                            1.0,
                            {"name": c.name, "reason": "timeout"},
                        )
                        self._record_error(c.name)
                        if self._failure_budget is not None:
                            await self._failure_budget.record_failure(
                                c.name,
                                "timeout",
                                threshold=self._threshold_for(c),
                            )
                        await self._publish_event(
                            SchedulerTickEvent(
                                collector=c.name,
                                tick_id=tick_id,
                                outcome="failure",
                                reason="timeout",
                                duration_seconds=duration,
                                trigger_kind=trigger.kind,
                                request_id=trigger.request_id,
                                ts=utc_now_iso(),
                            )
                        )
                        self._signal_awaitable_done(tick_id, None)
                        return
                    except Exception:
                        duration = self._loop.time() - start
                        self._self_metrics.write_counter(
                            "homelab_collector_run_failure_total",
                            1.0,
                            {"name": c.name, "reason": "exception"},
                        )
                        self._record_error(c.name)
                        if self._failure_budget is not None:
                            await self._failure_budget.record_failure(
                                c.name,
                                "exception",
                                threshold=self._threshold_for(c),
                            )
                        await self._publish_event(
                            SchedulerTickEvent(
                                collector=c.name,
                                tick_id=tick_id,
                                outcome="failure",
                                reason="exception",
                                duration_seconds=duration,
                                trigger_kind=trigger.kind,
                                request_id=trigger.request_id,
                                ts=utc_now_iso(),
                            )
                        )
                        self._signal_awaitable_done(tick_id, None)
                        return
                    finally:
                        self._self_metrics.write_summary(
                            "homelab_collector_run_duration_seconds",
                            self._loop.time() - start,
                            {"name": c.name},
                        )

                    duration = self._loop.time() - start
                    if result.ok:
                        self._self_metrics.write_counter(
                            "homelab_collector_run_success_total",
                            1.0,
                            {"name": c.name},
                        )
                        if c.name in self._last_error_ts:
                            age = self._loop.time() - self._last_error_ts[c.name]
                            self._self_metrics.write_gauge(
                                "homelab_collector_run_last_error_age_seconds",
                                age,
                                {"name": c.name},
                            )
                        if self._failure_budget is not None:
                            await self._failure_budget.record_success(c.name)
                        await self._publish_event(
                            SchedulerTickEvent(
                                collector=c.name,
                                tick_id=tick_id,
                                outcome="success",
                                duration_seconds=duration,
                                trigger_kind=trigger.kind,
                                request_id=trigger.request_id,
                                ts=utc_now_iso(),
                            )
                        )
                    else:
                        self._self_metrics.write_counter(
                            "homelab_collector_run_failure_total",
                            1.0,
                            {"name": c.name, "reason": "result_error"},
                        )
                        self._record_error(c.name)
                        if self._failure_budget is not None:
                            await self._failure_budget.record_failure(
                                c.name,
                                "result_error",
                                threshold=self._threshold_for(c),
                            )
                        await self._publish_event(
                            SchedulerTickEvent(
                                collector=c.name,
                                tick_id=tick_id,
                                outcome="failure",
                                reason="result_error",
                                duration_seconds=duration,
                                trigger_kind=trigger.kind,
                                request_id=trigger.request_id,
                                ts=utc_now_iso(),
                            )
                        )

                    # Set the result on any awaitable future (await_immediate_run)
                    self._signal_awaitable_done(tick_id, result)

                finally:
                    lock.release()
        finally:
            reset_current_tick(token)

    def _threshold_for(self, c: Collector) -> int | None:
        """Return per-collector ``quarantine_after`` override, or ``None`` for default."""
        # SCAFFOLDING: STAGE-001-009/010 may add dynamic collector reload; if so,
        # self._configs (snapshot at __init__) will need to be refreshed via a
        # public update method. Currently no reload mechanism exists.
        cfg = self._configs.get(c.name)
        return cfg.quarantine_after if cfg is not None else None

    def _record_error(self, name: str) -> None:
        """Stash the error timestamp + emit ``last_error_age_seconds`` = 0.

        Called from every failure arm in :meth:`_tick`. The gauge value of 0
        means "the error happened just now"; subsequent successful ticks emit
        the age (now - last_error_ts).

        We emit 0.0 (not skip-emit) on failure so that downstream alerting on
        ``last_error_age_seconds > threshold`` will see the gauge fall to 0 the
        instant a failure occurs — without the gauge being recorded at the failure
        moment, alerts would lag by an interval until the next successful tick.
        """
        assert self._loop is not None
        self._last_error_ts[name] = self._loop.time()
        self._self_metrics.write_gauge(
            "homelab_collector_run_last_error_age_seconds",
            0.0,
            {"name": name},
        )

    async def _publish_event(self, event: SchedulerTickEvent) -> None:
        """Publish a tick event to the event sink.

        Non-throwing: exceptions are logged and ignored so scheduler ticks
        are never disturbed by sink failures.
        """
        with contextlib.suppress(Exception):
            await self._config.event_sink.publish(
                event
            )  # Per Protocol contract: publish MUST NOT raise

    # --- Dispatch by run_kind --------------------------------------------------------

    async def _dispatch(self, c: Collector, ctx: CollectorContext) -> CollectorResult:
        """Run the collector via the appropriate executor for its ``run_kind``.

        ASYNC: directly awaited on the FastAPI loop.
        THREAD: ``run_in_executor`` against ``self._thread_pool`` driving
            :func:`_thread_runner`.
        PROCESS: ``run_in_executor`` against ``self._process_pool`` driving
            :func:`_process_runner`; buffered metrics are replayed through
            ``ctx.vm`` after the future resolves.
        """
        if c.run_kind == RunKind.ASYNC:
            return await c.run(ctx)

        assert self._loop is not None
        if c.run_kind == RunKind.THREAD:
            assert self._thread_pool is not None
            return await self._loop.run_in_executor(
                self._thread_pool,
                _thread_runner,
                c,
                ctx,
            )

        if c.run_kind == RunKind.PROCESS:
            # SCAFFOLDING: STAGE-001-008 added failure-budget quarantine which will
            # quarantine a chronically-failing PROCESS collector after threshold,
            # bounding the impact of a hard-crash. However, the executor pool itself
            # remains broken until scheduler restart. Pool rebuild on broken state is
            # still a future enhancement (post-009).
            assert self._process_pool is not None
            proc_ctx = ProcessCollectorContext(
                config=ctx.config,
                secrets=ctx.secrets,
                metrics=BufferingMetricsWriter(),
            )
            result, buffered = await self._loop.run_in_executor(
                self._process_pool,
                _process_runner,
                type(c),
                proc_ctx,
            )
            for m in buffered:
                if m.kind == "gauge":
                    ctx.vm.write_gauge(m.name, m.value, m.labels)
                elif m.kind == "counter":
                    ctx.vm.write_counter(m.name, m.value, m.labels)
                elif m.kind == "summary":  # pragma: no cover -- subprocess isolation
                    ctx.vm.write_summary(m.name, m.value, m.labels)
            return result

        if c.run_kind == RunKind.SUBPROCESS:
            # SubprocessCollector.run delegates to subprocess_runner internally;
            # scheduler stays in-loop and just awaits. Group locks + FailureBudget
            # are RunKind-agnostic (keyed by collector name), so no further wiring
            # needed here. Self-metrics are emitted by the parent-side _tick loop
            # for all RunKinds uniformly.
            return await c.run(ctx)

        # Defensive — RunKind is exhaustively enumerated above; only reachable if
        # a future RunKind variant is added without a dispatch arm.
        msg = f"Unknown run_kind: {c.run_kind}"  # pragma: no cover
        raise NotImplementedError(msg)  # pragma: no cover -- RunKind enum exhaustively dispatched
