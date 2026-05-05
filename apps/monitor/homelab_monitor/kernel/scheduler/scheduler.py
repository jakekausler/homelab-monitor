"""The async tick scheduler.

Per-collector ``asyncio.Task`` with absolute-deadline tick loops. Three
``run_kind`` dispatch arms (ASYNC, THREAD, PROCESS). Five scheduler-owned
self-metrics emitted unconditionally per tick. Graceful shutdown with
``shutdown_total`` outcome distinct from ``failure_total``.

Spec references: STAGE-001-007.md §"Decisions in this stage" (D1-D5),
``docs/superpowers/specs/2026-05-04-homelab-monitor-design.md`` §3.1, §5.4.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field

from homelab_monitor.kernel.plugins.base import Collector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import MetricEntry, MetricsWriter
from homelab_monitor.kernel.plugins.loader import LoadedCollector
from homelab_monitor.kernel.plugins.process_context import (
    BufferingMetricsWriter,
    ProcessCollectorContext,
)
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """Knobs for :class:`Scheduler`.

    - ``shutdown_grace_seconds`` — max time :meth:`Scheduler.stop` waits for
      in-flight ticks to complete before forced cancellation. Default 30s.
    - ``process_pool_size`` — workers in the shared ``ProcessPoolExecutor``.
      Default ``min(2, os.cpu_count() or 1)`` (matches stage decision).
    - ``thread_pool_size`` — workers in the shared ``ThreadPoolExecutor``.
      Bounded; THREAD plugins are rare initially. Default 4.
    """

    shutdown_grace_seconds: float = 30.0
    process_pool_size: int = field(
        default_factory=lambda: min(2, os.cpu_count() or 1),
    )
    thread_pool_size: int = 4


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

    def __init__(
        self,
        loaded: list[LoadedCollector],
        ctx_factory: Callable[[Collector], CollectorContext],
        self_metrics: MetricsWriter,
        config: SchedulerConfig | None = None,
    ) -> None:
        """Stash dependencies; do NOT touch the event loop here.

        ``ctx_factory`` is a callable the scheduler invokes once per tick to
        produce the :class:`CollectorContext` handed to the collector's ``run``
        method. The factory pattern lets STAGE-001-010 wire real DB / HTTP /
        log handles without the scheduler caring about construction details.
        """
        self._loaded: list[LoadedCollector] = list(loaded)
        self._ctx_factory: Callable[[Collector], CollectorContext] = ctx_factory
        self._self_metrics: MetricsWriter = self_metrics
        self._config: SchedulerConfig = config if config is not None else SchedulerConfig()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._tick_tasks: list[asyncio.Task[None]] = []
        self._inflight: set[asyncio.Task[None]] = set()
        self._stopping: bool = False
        self._last_error_ts: dict[str, float] = {}
        self._thread_pool: ThreadPoolExecutor | None = None
        self._process_pool: ProcessPoolExecutor | None = None

    @property
    def running(self) -> bool:
        """Return ``True`` between :meth:`start` and :meth:`stop`.

        Note: a scheduler with zero collectors returns ``False`` even after a
        successful :meth:`start`, because ``bool([]) is False``.
        """
        return bool(self._tick_tasks) and not self._stopping

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
        self._thread_pool = ThreadPoolExecutor(max_workers=self._config.thread_pool_size)
        self._process_pool = ProcessPoolExecutor(max_workers=self._config.process_pool_size)
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
                await asyncio.sleep(max(0.0, deadline - now))
                if self._stopping:  # pragma: no branch -- mid-tick stop is timing-race;
                    # not deterministically testable
                    break  # pragma: no cover -- timing-race, not testable
                inflight = self._loop.create_task(self._tick(c))
                self._inflight.add(inflight)
                inflight.add_done_callback(self._inflight.discard)
                deadline += interval
        except asyncio.CancelledError:
            return  # Cancellation from stop() is the normal exit path.

    # --- Single tick -----------------------------------------------------------------

    async def _tick(self, c: Collector) -> None:
        """Run one tick: build ctx, dispatch, classify outcome, emit self-metrics.

        Catch order is exact (CancelledError → TimeoutError → Exception);
        do NOT swap them. ``asyncio.timeout`` raises :class:`TimeoutError`
        (the builtin, NOT ``asyncio.TimeoutError`` — they're the same on
        3.11+ but the builtin is the documented form).
        """
        assert self._loop is not None
        start = self._loop.time()
        ctx = self._ctx_factory(c)
        try:
            async with asyncio.timeout(c.timeout.total_seconds()):
                # SCAFFOLDING: STAGE-001-008 will add
                #   `async with self._group_locks[c.concurrency_group]:`
                # around _dispatch here. No driver changes.
                result = await self._dispatch(c, ctx)
        except asyncio.CancelledError:
            if self._stopping:
                self._self_metrics.write_counter(
                    "homelab_collector_run_shutdown_total",
                    1.0,
                    {"name": c.name},
                )
                return
            # External cancellation of a tick task (not via Scheduler.stop())
            # is unexpected; propagate so the supervising _run_collector loop
            # sees the cancel.
            raise  # pragma: no cover -- not reachable via Scheduler.stop()
        except TimeoutError:
            self._self_metrics.write_counter(
                "homelab_collector_run_failure_total",
                1.0,
                {"name": c.name, "reason": "timeout"},
            )
            self._record_error(c.name)
            return
        except Exception:  # — scheduler must isolate plugin failures
            self._self_metrics.write_counter(
                "homelab_collector_run_failure_total",
                1.0,
                {"name": c.name, "reason": "exception"},
            )
            self._record_error(c.name)
            return
        finally:
            self._self_metrics.write_summary(
                "homelab_collector_run_duration_seconds",
                self._loop.time() - start,
                {"name": c.name},
            )

        # No exception: classify by result.ok.
        if result.ok:
            self._self_metrics.write_counter(
                "homelab_collector_run_success_total",
                1.0,
                {"name": c.name},
            )
            # Emit last-error-age gauge ONLY if we have a baseline failure.
            if c.name in self._last_error_ts:
                age = self._loop.time() - self._last_error_ts[c.name]
                self._self_metrics.write_gauge(
                    "homelab_collector_run_last_error_age_seconds",
                    age,
                    {"name": c.name},
                )
        else:
            self._self_metrics.write_counter(
                "homelab_collector_run_failure_total",
                1.0,
                {"name": c.name, "reason": "result_error"},
            )
            self._record_error(c.name)

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
            # SCAFFOLDING: STAGE-001-008+ — pool recovery on BrokenProcessPool.
            # NOTE: a PROCESS worker hard-crash (e.g., os._exit) propagates
            # `concurrent.futures.process.BrokenProcessPool` and the executor
            # becomes unusable for subsequent submits — every following PROCESS
            # tick will fail with the same exception. Rebuild logic on broken
            # state is deferred to STAGE-001-008 (alongside quarantine), where
            # the failure budget will catch the chain of failures and quarantine
            # the offending collector. Until then, a single hard-crash will
            # disable PROCESS-mode for ALL collectors until scheduler restart.
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

        # Defensive — RunKind is exhaustively enumerated above; only reachable if
        # a future RunKind variant is added without a dispatch arm.
        msg = f"Unknown run_kind: {c.run_kind}"  # pragma: no cover
        raise NotImplementedError(msg)  # pragma: no cover -- RunKind enum exhaustively dispatched
