# Scheduler Architecture

**Source stage:** STAGE-001-007
**Spec references:** `docs/superpowers/specs/2026-05-04-homelab-monitor-design.md` §3.1, §5.4

---

## 1. Overview

The scheduler is the runtime core of the homelab-monitor kernel. It drives periodic execution of in-process Python collector plugins, routing each tick through the correct concurrency arm (ASYNC, THREAD, or PROCESS) and emitting five self-metrics per tick regardless of outcome. It sits between the plugin discovery layer (`PluginLoader`) and the rest of the kernel stack (DB, HTTP, metrics destination), which are injected via a `ctx_factory` callable.

The scheduler's existence is justified by a constraint: collectors are self-describing (they carry `interval`, `timeout`, `run_kind` as ClassVars), but nothing in FastAPI or asyncio provides "run this coroutine every N seconds with absolute deadlines and heterogeneous executors." The scheduler fills that gap without pulling in APScheduler or similar libraries.

```
PluginLoader.load_all()
        |
        v
 [LoadedCollector, ...]          SchedulerConfig
        |                               |
        +--------> Scheduler.__init__() <+
                        |
               scheduler.start()
                        |
          .-------------+-------------.
          |             |             |
   _run_collector  _run_collector  _run_collector    (one asyncio.Task each)
    (collector A)   (collector B)   (collector C)
          |
   absolute-deadline tick loop
          |
   create_task(_tick(c))          <-- fire-and-track; loop doesn't await
          |
   _dispatch(c, ctx)
     |         |         |
   ASYNC     THREAD    PROCESS
   await    run_in_   run_in_
   c.run()  executor  executor
            (_thread_ (_process_
            runner)   runner)
                           |
                    BufferingMetricsWriter
                    replay -> ctx.vm
          |
   CollectorResult
          |
   self-metrics emission (5 metrics, unconditional)
```

---

## 2. Lifecycle

### Construction

```python
Scheduler(
    loaded: list[LoadedCollector],
    ctx_factory: Callable[[Collector], CollectorContext],
    self_metrics: MetricsWriter,
    config: SchedulerConfig | None = None,
)
```

- `loaded` — output of `PluginLoader.load_all()`.
- `ctx_factory` — called once per tick to build the `CollectorContext` handed to `collector.run()`. The factory pattern keeps the scheduler ignorant of DB/HTTP/log construction. STAGE-001-010 wires in the real handles here.
- `self_metrics` — `MetricsWriter` instance for the five scheduler-owned self-metrics. Currently the in-memory stub; `VictoriaMetricsWriter` lands in STAGE-001-015.
- `config` — optional `SchedulerConfig`; defaults applied if `None`.

`SchedulerConfig` fields:

| Field | Default | Notes |
|---|---|---|
| `shutdown_grace_seconds` | `30.0` | Max wait for in-flight ticks on stop |
| `process_pool_size` | `min(2, os.cpu_count() or 1)` | Workers in shared `ProcessPoolExecutor` |
| `thread_pool_size` | `4` | Workers in shared `ThreadPoolExecutor` |

Construction does not touch the event loop. Executor creation and task spawning happen in `start()`.

### `await scheduler.start()`

1. Raises `RuntimeError("Scheduler already started")` if `_tick_tasks` is non-empty (double-start guard).
2. Captures the running event loop into `self._loop`.
3. Creates `ThreadPoolExecutor(max_workers=thread_pool_size)` and `ProcessPoolExecutor(max_workers=process_pool_size)`.
4. Spawns one `asyncio.Task` per collector via `loop.create_task(_run_collector(c))`. The task list is stored in `self._tick_tasks`.

The scheduler is not reusable. Call `start()` once, `stop()` once. To restart, construct a new instance.

### `await scheduler.stop()`

1. Sets `_stopping = True`.
2. Cancels all tick-loop tasks (`_tick_tasks`).
3. Awaits them via `gather(..., return_exceptions=True)` — `CancelledError` is swallowed at the loop level.
4. Cancels all in-flight `_tick` tasks **immediately** (not after waiting). In-flight tasks that were mid-execution receive `CancelledError`; because `_stopping` is `True`, they emit `shutdown_total` instead of `failure_total`.
5. Awaits in-flight tasks within `shutdown_grace_seconds`. On timeout, force-cancels any stragglers and awaits again.
6. Shuts down both executor pools (`wait=False, cancel_futures=True`).
7. Clears `_tick_tasks`.

### `scheduler.running`

Returns `bool(self._tick_tasks) and not self._stopping`. A scheduler with zero collectors returns `False` even after a successful `start()`.

---

## 3. Tick Model

Each collector gets one persistent `asyncio.Task` running `_run_collector(c)`.

### Initial offset

```python
interval = c.interval.total_seconds()
offset = float(hash(c.name) % max(1, int(interval))) if interval >= 1 else 0.0
deadline = loop.time() + offset
```

This decorrelates collectors that share the same interval so they don't all fire simultaneously. The formula has a known gap: any interval in `[1.0, 2.0)` produces `int(interval) == 1`, so `hash % 1 == 0`, meaning offset is always 0 for that range. Decorrelation only activates for intervals ≥ 2.0 seconds. This is acceptable because production intervals are integer seconds.

### Loop body

```
while not _stopping:
    now = loop.time()
    if deadline + interval < now:
        deadline = now + interval    # skip-forward: no catch-up storm
    await asyncio.sleep(max(0.0, deadline - now))
    if _stopping: break
    inflight = loop.create_task(_tick(c))
    _inflight.add(inflight)
    inflight.add_done_callback(_inflight.discard)
    deadline += interval
```

Key properties:

- **Fire-and-track**: the tick task is not awaited. The loop advances `deadline` immediately and goes back to sleep. Tick duration does not accumulate into inter-tick gaps.
- **Drift skip-forward**: if a tick ran longer than its interval and `deadline` has already passed by more than one full interval, the loop jumps forward rather than issuing back-to-back catch-up ticks.
- **Clock source**: `asyncio.get_running_loop().time()` — the same monotonic clock asyncio uses internally. Immune to NTP adjustments and DST transitions.
- **Exit**: the loop exits via `CancelledError` propagated from `stop()`, not via the `while not _stopping` predicate.

---

## 4. RunKind Dispatch

`_dispatch(c, ctx)` selects the execution path based on `c.run_kind`.

### ASYNC

**Use case:** network I/O collectors, HA queries, anything already async. The majority of built-in collectors.

**Dispatch:** `await c.run(ctx)` directly on the FastAPI event loop, wrapped in `asyncio.timeout(c.timeout.total_seconds())`.

**State:** the same collector instance is used on every tick. Instance state (e.g., reused `httpx.AsyncClient`, last-seen caches) persists across ticks.

### THREAD

**Use case:** blocking I/O that cannot be made async (e.g., a synchronous SDK, subprocess blocking read). Rare in the built-in set.

**Dispatch:** `await loop.run_in_executor(thread_pool, _thread_runner, c, ctx)`. `_thread_runner` is a module-level function (not a closure) that calls `asyncio.run(collector.run(ctx))` in the worker thread.

**State:** the same collector instance is passed to the worker. State persists across ticks (ThreadPoolExecutor does not require pickling).

**Timeout caveat:** timeout is best-effort. Python cannot forcibly terminate a thread. If the thread overruns, the scheduler logs and emits `failure_total{reason=timeout}`, but the thread continues until it returns. THREAD collectors must use cooperative cancellation if hard timeouts are required. PROCESS is the escape hatch.

### PROCESS

**Use case:** CPU-intensive collectors (parsing, hashing, image analysis) that would block the event loop even from a thread, or untrusted third-party plugins.

**Dispatch:**

1. Constructs a `ProcessCollectorContext(config=ctx.config, secrets=ctx.secrets, metrics=BufferingMetricsWriter())`.
2. Submits `_process_runner(collector_cls, proc_ctx)` to the `ProcessPoolExecutor`.
3. `_process_runner` (module-level, picklable) re-instantiates `collector_cls()` in the worker process and calls `asyncio.run(collector.run(proc_ctx))`.
4. Worker returns `(CollectorResult, list[MetricEntry])`.
5. Parent replays the buffered metrics through the real `ctx.vm` (`write_gauge` / `write_counter` / `write_summary`).

**State:** workers re-instantiate the collector on every tick. No instance state survives between ticks.

**Context restrictions:** PROCESS collectors receive `ProcessCollectorContext`, not `CollectorContext`. Available handles: `config`, `secrets`, `metrics`. Not available: `db`, `http`, `log`, `vl`, `ssh`, `ha`. Attempting to access missing handles will raise `AttributeError` at runtime. This is by design — the contract for PROCESS is "pure CPU work, no cross-process I/O."

**BrokenProcessPool caveat:** a worker hard-crash (e.g., `os._exit(1)`) corrupts the `ProcessPoolExecutor`. Every subsequent PROCESS tick fails with `BrokenProcessPool` until scheduler restart. ASYNC and THREAD collectors are unaffected. STAGE-001-008's failure budget + quarantine will catch the chain of failures and quarantine the offending collector; pool rebuild is deferred to that stage.

---

## 5. Self-Metrics

The scheduler emits these five metrics from `_tick` on every tick. Collectors do not emit them; the scheduler owns this layer unconditionally.

| Metric | Type | Labels | When emitted |
|---|---|---|---|
| `homelab_collector_run_success_total` | counter | `name` | `result.ok == True` and no exception |
| `homelab_collector_run_failure_total` | counter | `name`, `reason` | `reason` ∈ `{"timeout", "exception", "result_error"}` |
| `homelab_collector_run_shutdown_total` | counter | `name` | `CancelledError` while `_stopping == True` |
| `homelab_collector_run_duration_seconds` | summary | `name` | Always, in `finally` block |
| `homelab_collector_run_last_error_age_seconds` | gauge | `name` | See pattern below |

`last_error_age_seconds` follows pattern (ii):

- On failure: emits `0.0` immediately (so alerting rules see the gauge drop to 0 the instant failure occurs, without waiting a full interval).
- On subsequent success: emits `loop.time() - last_error_ts[name]` (age since last failure).
- Before the first failure: never emitted (gauge is absent, not zero).

`shutdown_total` is distinct from `failure_total` so that alertmanager rules can fire on `rate(failure_total[5m]) > 0` during normal shutdown without false positives.

Collectors emit their domain metrics (e.g., `homelab_ping_rtt_seconds`) independently via `ctx.vm`. The scheduler self-metrics and collector domain metrics are orthogonal.

---

## 6. Graceful Shutdown Semantics

`stop()` sequence in order:

1. `_stopping = True` — tick loops check this flag at the top of each iteration and after each sleep.
2. Cancel all `_tick_tasks` (the per-collector loop tasks).
3. `await gather(*_tick_tasks, return_exceptions=True)` — loops swallow `CancelledError` and exit cleanly.
4. Cancel all tasks in `_inflight` **immediately** — do not wait for natural completion first. This ensures in-flight ticks reach their `except CancelledError` branch and emit `shutdown_total`.
5. `await gather(*_inflight, ...)` within `asyncio.timeout(shutdown_grace_seconds)`.
6. On timeout: force-cancel any remaining inflight tasks and await again. This branch is marked `# pragma: no cover` — cancellation propagation is expected to complete well within 30 seconds in practice.
7. Shut down executor pools with `wait=False, cancel_futures=True`.

An in-flight tick that is cancelled during shutdown emits exactly one metric: `shutdown_total{name}`. It does not emit `failure_total` or `success_total`. `duration_seconds` is still observed in the `finally` block.

---

## 7. PluginLoader

`PluginLoader` is an in-memory programmatic registry. For STAGE-001-007 it is the complete discovery mechanism; later stages extend it without changing the scheduler-facing API.

**`register(collector_cls, config_overrides)`**

- Validates `config_overrides` via `CollectorConfig.model_validate(overrides)`. Raises `pydantic.ValidationError` on constraint violations (name regex, interval bounds, unknown fields).
- Instantiates `collector_cls()` (zero-arg constructor required by the `Collector` Protocol).
- Appends and returns the `LoadedCollector(collector=instance, config=config)`.

**`load_all()`**

Returns a defensive copy (`list(self._loaded)`) of all registered records. SCAFFOLDING comments inside `load_all` mark the insertion points for filesystem scan (STAGE-001-009) and entry-point scan (EPIC-002).

**`LoadedCollector`**

Frozen dataclass pairing a `Collector` instance with its `CollectorConfig`. The `config` field is not yet consumed by the scheduler — the scheduler reads `c.interval` / `c.timeout` from ClassVars. STAGE-001-010's `ctx_factory` will use `LoadedCollector.config` to deliver per-instance overrides via `CollectorContext.config`.

---

## 8. Forward Integration Points

SCAFFOLDING comments mark the exact insertion points for upcoming stages. File references are relative to `apps/monitor/`.

| Stage | What lands | Insertion point |
|---|---|---|
| STAGE-001-008 | Concurrency-group lock | `scheduler.py:_tick`, just before `_dispatch` call: `async with self._group_locks[c.concurrency_group]:` |
| STAGE-001-008 | Quarantine skip | `scheduler.py:_run_collector`, top of tick loop: `if c.name in self._quarantined: continue` |
| STAGE-001-008 | `BrokenProcessPool` recovery | `scheduler.py:_dispatch` PROCESS arm, on `BrokenProcessPool` exception: quarantine collector, rebuild pool |
| STAGE-001-009 | `RunKind.SUBPROCESS` dispatch arm | `scheduler.py:_dispatch`, new elif after PROCESS arm |
| STAGE-001-009 | Filesystem + entry-point scan | `loader.py:load_all`, inside SCAFFOLDING comment block |
| STAGE-001-010 | FastAPI lifespan wiring | New lifespan function: `app.state.scheduler = Scheduler(loader.load_all(), ctx_factory, vm_writer)` → `await app.state.scheduler.start()` on startup, `await app.state.scheduler.stop()` on shutdown |
| STAGE-001-010 | Real `ctx_factory` | Builds `CollectorContext` from real `SqliteRepository`, `httpx.AsyncClient`, `SyncSecretsResolver`, structlog logger, VM writer, VL writer, SSH factory, HA client |

---

## 9. Known Limitations

**PROCESS + fork() under uvicorn**

`ProcessPoolExecutor` uses Python's default `fork()` start method. In a multi-threaded process (asyncio + thread pool), `fork()` can deadlock in the child. Python 3.12 emits `DeprecationWarning: This process is multi-threaded, use of fork() may lead to deadlocks in the child.` This is not a blocker while PROCESS collectors aren't running in production, but must be resolved before STAGE-001-010 enables PROCESS mode: pass `mp_context=multiprocessing.get_context("forkserver")` to `ProcessPoolExecutor`. Tracked in `epics/EPIC-001-foundation/regression.md` item #4.

**Sub-second interval offset**

`hash(name) % max(1, int(interval))` produces `0` for any interval in `[0.0, 2.0)`. Collectors with sub-second or fractional intervals below 2s all start at the same time. If fractional sub-2s intervals become common, switch to a millisecond-precision modulus.

**THREAD timeout is best-effort**

Python cannot kill a running thread. A THREAD collector that ignores cooperative cancellation will run past its timeout. The scheduler correctly emits `failure_total{reason=timeout}` and moves on, but the thread remains alive until it returns. Use PROCESS for hard timeout requirements.

**PROCESS worker hard-crash disables all PROCESS collectors**

A single `os._exit()` from any PROCESS worker breaks the shared `ProcessPoolExecutor`. All subsequent PROCESS ticks fail until scheduler restart. STAGE-001-008 quarantine will bound the blast radius to the offending collector; pool rebuilding is deferred.

**`_process_runner` coverage gap**

`_process_runner` body is `# pragma: no cover` because pytest-cov cannot aggregate coverage from worker processes. Behavior is verified via runtime e2e tests in `test_scheduler_e2e.py`.

**Scheduler is not reusable**

Calling `start()` a second time raises `RuntimeError`. Construct a new `Scheduler` instance for restart scenarios.

---

## 10. Testing

| File | Scope | Count |
|---|---|---|
| `apps/monitor/tests/test_scheduler.py` | Unit: tick precision, RunKind dispatch, lifecycle, failure modes, self-metrics, drift, offset | 16+ tests |
| `apps/monitor/tests/test_plugin_loader.py` | Unit: `PluginLoader.register`, validation, `load_all` | — |
| `apps/monitor/tests/test_process_context.py` | Unit: `BufferingMetricsWriter` drain semantics, pickle roundtrip, `ProcessCollectorContext` fields | — |
| `apps/monitor/tests/test_scheduler_e2e.py` | E2E: real kernel types (no mocks), 6 scenarios | 6 tests |

E2E scenarios:

1. Real kernel types plumbing — `SqliteRepository`, `httpx.AsyncClient`, `SyncSecretsResolver` (not mocks).
2. Long-running wall-clock precision (30s) — 4 collectors at 1s/2s/5s/10s; all tick counts within ±15% of ideal.
3. Mixed RunKind with real context — ASYNC + THREAD + PROCESS at 2s over 8s; all three dispatch arms succeed; PROCESS metric replay verified.
4. PROCESS worker hard crash (`os._exit(1)`) — emits `failure_total{reason=exception}`; ASYNC companion unaffected.
5. Scheduler restart independence — two sequential instances share no state; counters fresh on second start.
6. Hash-offset spread — 20 collectors at identical 2s interval; first-tick times spread across ~2s window.

Coverage gate: 100% kernel statements. The `_process_runner` body and a handful of timing-dependent defensive branches are the only `# pragma: no cover` lines, each with a one-line rationale comment in the source.
