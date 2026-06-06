"""Integration test: DrainConsumer metrics emission against real VictoriaLogs (STAGE-004-027).

Validates the behaviours that unit tests with a fake VL client / InMemoryMetricsWriter
cannot confirm:
  1. Real VL → DrainEngine → InMemoryMetricsWriter: correct metric names, labels,
     and values after a real cycle (including severity label flowing from the real VL line).
  2. Re-entrancy guard: concurrent run_once() raises CycleInProgressError for the
     second caller.
  3. HTTP endpoints against rig backend (skipped — rig backend has drain disabled;
     see HOMELAB_MONITOR_DRAIN_ENABLED not set in monitor environment).
  4. Best-effort: metrics in VictoriaMetrics after a cycle driven via vmagent scrape.
     Implemented as best-effort / skipped (only works if rig backend drives cycle).

Requires the docker-compose.test.yml rig (``make integration``). All tests
auto-skip fast when any required component is unreachable.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.config import DrainConfig, VlQueryLimits
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.drain_consumer import (
    WATERMARK_KEY,
    CycleInProgressError,
    DrainConsumer,
)
from homelab_monitor.kernel.logs.drain_engine import DrainEngine
from homelab_monitor.kernel.logs.drain_persistence import SqlitePersistence
from homelab_monitor.kernel.logs.signature_sync import SignatureCatalogSync
from homelab_monitor.kernel.logs.victorialogs_client import VictoriaLogsClient
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter, MetricEntry

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components
from .helpers.vl_planter import plant_log_lines

# Budget (seconds) to wait for VL to make planted lines queryable.
_VL_INGEST_BUDGET_S = 30.0
# How often to poll while waiting for VL ingest.
_VL_INGEST_POLL_S = 2.0
# Unique service label used by all tests in this module (avoids cross-test contamination).
_SERVICE = "drain-mtest"

# Metric name constants (mirrors drain_consumer.py)
_M_CYCLE_LINES = "homelab_drain_cycle_lines_total"
_M_CYCLE_NEW_TEMPLATES = "homelab_drain_cycle_new_templates_total"
_M_CYCLE_DURATION = "homelab_drain_cycle_duration_seconds"
_M_SIG_COUNT = "homelab_log_signature_count"
_M_SIG_TOTAL = "homelab_log_signature_total"
_M_SIG_FIRST_SEEN = "homelab_log_signature_first_seen_ts"
_M_SIG_CARD_WARN = "homelab_log_signature_cardinality_warn"

# Severity planted in VL lines — we assert this flows through as the severity label.
_PLANTED_SEVERITY = "error"


def _cast_bound_logger() -> BoundLogger:
    from typing import cast  # noqa: PLC0415

    return cast(BoundLogger, structlog.get_logger().bind(component="drain-mtest"))


def _make_temp_repo() -> tuple[SqliteRepository, Path]:
    """Create a fresh tempfile-backed SQLite repo with migrations applied.

    Returns (repo, db_path) — the caller is responsible for cleanup.
    """
    fd, raw = tempfile.mkstemp(prefix="hm-drain-mtest-", suffix=".db")
    os.close(fd)
    db_path = Path(raw)
    db_path.unlink(missing_ok=True)  # let SQLite create it fresh
    db_url = f"sqlite+aiosqlite:///{db_path}"
    alembic_upgrade_head(db_url)
    engine = get_engine(url=db_url)
    return SqliteRepository(engine=engine), db_path


def _cleanup_repo(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        (db_path.parent / (db_path.name + suffix)).unlink(missing_ok=True)


def _seed_watermark(repo: SqliteRepository, watermark_ms: int) -> None:
    """Pre-seed the watermark in app_settings so the consumer doesn't cold-start to 'now'.

    Without this the consumer seeds to now_ms (when ingest_lag_grace=0) and the
    query window [now, now] is empty — all planted-in-the-past lines are missed.
    Seeding to a time before base_time gives a real query window.
    """

    async def _set() -> None:
        await AppSettingsRepository(repo).set(WATERMARK_KEY, str(watermark_ms))

    asyncio.run(_set())


def _make_consumer(  # noqa: PLR0913
    vl_url: str,
    http_client: httpx.AsyncClient,
    repo: SqliteRepository,
    metrics_writer: InMemoryMetricsWriter,
    *,
    ingest_lag_grace_seconds: int = 0,
    batch_max_lines: int = 10_000,
) -> DrainConsumer:
    """Construct a DrainConsumer wired to a real VL, a SQLite repo, and the given writer."""
    limits = VlQueryLimits(max_lines=50_000, max_bytes=50 * 1024 * 1024, timeout_seconds=30.0)
    vl_client = VictoriaLogsClient(vl_url=vl_url, http_client=http_client, limits=limits)
    persistence = SqlitePersistence(repo)
    engine = DrainEngine(persistence)
    settings = AppSettingsRepository(repo)
    config = DrainConfig(
        interval_seconds=300,
        batch_max_lines=batch_max_lines,
        ingest_lag_grace_seconds=ingest_lag_grace_seconds,
    )
    sig_sync = SignatureCatalogSync(repo)
    return DrainConsumer(
        vl_client=vl_client,
        engine=engine,
        settings=settings,
        persistence=persistence,
        config=config,
        metrics_writer=metrics_writer,
        sig_sync=sig_sync,
        log=_cast_bound_logger(),
    )


def _wait_for_vl_ingest(
    vl_url: str,
    marker: str,
    expected_count: int,
    budget_s: float = _VL_INGEST_BUDGET_S,
) -> None:
    """Poll VL /select/logsql/query until at least `expected_count` matching lines appear.

    Uses a direct httpx.get (synchronous) to mirror vl_planter's approach.
    Raises AssertionError if the budget is exhausted without seeing enough lines.
    """
    deadline = time.time() + budget_s
    last_count = 0
    while time.time() < deadline:
        now = datetime.now(UTC)
        start = (now - timedelta(minutes=10)).isoformat()
        end = now.isoformat()
        params: dict[str, Any] = {
            "query": f'service:{_SERVICE} "{marker}"',
            "start": start,
            "end": end,
            "limit": str(expected_count + 10),
        }
        try:
            resp = httpx.get(
                f"{vl_url}/select/logsql/query",
                params=params,
                timeout=10.0,
            )
            if resp.status_code == 200:  # noqa: PLR2004
                lines = [ln for ln in resp.text.splitlines() if ln.strip()]
                last_count = len(lines)
                if last_count >= expected_count:
                    return
        except httpx.RequestError:
            pass
        time.sleep(_VL_INGEST_POLL_S)
    msg = (
        f"VL ingest wait: marker {marker!r} did not surface {expected_count} lines "
        f"within {budget_s}s (last count: {last_count})."
    )
    raise AssertionError(msg)


def _find_entries(recorded: list[MetricEntry], name: str) -> list[MetricEntry]:
    """Return all recorded entries with the given metric name."""
    return [e for e in recorded if e.name == name]


def _find_entry_with_labels(
    recorded: list[MetricEntry],
    name: str,
    required_labels: dict[str, str],
) -> MetricEntry | None:
    """Return the first entry matching name and all required_labels, or None."""
    for entry in recorded:
        if entry.name != name:
            continue
        if all(entry.labels.get(k) == v for k, v in required_labels.items()):
            return entry
    return None


@pytest.mark.integration
@pytest.mark.slow
def test_drain_metrics_emitted_after_real_cycle() -> None:
    """Real VL cycle emits correct metrics with correct labels to InMemoryMetricsWriter.

    Plants 5 lines with severity='error' and a repeating-shape message into real VL,
    waits for ingest, seeds a past watermark, runs run_once(), then asserts:
    - homelab_drain_cycle_lines_total counter emitted (>= planted count).
    - homelab_drain_cycle_duration_seconds summary emitted.
    - homelab_log_signature_count gauge with severity='error' label.
    - homelab_log_signature_total gauge with a template_hash label.
    - homelab_log_signature_first_seen_ts gauge (nanoseconds).
    - homelab_log_signature_cardinality_warn = 0 (below default 100_000 threshold).
    """
    require_rig_components("monitor", "victorialogs")

    marker = f"drain-mtest-{uuid.uuid4().hex}"
    planted_count = 5
    base_time = datetime.now(UTC) - timedelta(seconds=5)

    repo, db_path = _make_temp_repo()
    # Pre-seed watermark to 1 minute before base_time so the consumer window covers lines.
    watermark_seed_ms = int((base_time - timedelta(minutes=1)).timestamp() * 1000)
    _seed_watermark(repo, watermark_seed_ms)

    metrics_writer = InMemoryMetricsWriter()

    try:
        with Rig.boot() as rig:
            plant_log_lines(
                host="rig-drain-mtest-host",
                service=_SERVICE,
                severity=_PLANTED_SEVERITY,
                message=f"connection reset from 10.0.0.1 marker={marker}",
                count=planted_count,
                base_time=base_time,
                interval_ms=100,
                vl_url=rig.urls.victorialogs,
            )

            _wait_for_vl_ingest(rig.urls.victorialogs, marker, planted_count)

            async def _run() -> Any:  # noqa: ANN401
                async with httpx.AsyncClient() as http_client:
                    consumer = _make_consumer(
                        rig.urls.victorialogs,
                        http_client,
                        repo,
                        metrics_writer,
                        ingest_lag_grace_seconds=0,
                    )
                    return await consumer.run_once()

            result = asyncio.run(_run())

        assert result.cycle_status in ("ok", "partial"), (
            f"Expected cycle_status in ('ok', 'partial'), got {result.cycle_status!r}. "
            f"error={result.error!r}"
        )
        assert result.lines_processed >= planted_count, (
            f"Expected lines_processed >= {planted_count}, got {result.lines_processed}"
        )

        recorded = metrics_writer.recorded

        # --- cycle lines counter ---
        cycle_lines_entries = _find_entries(recorded, _M_CYCLE_LINES)
        assert len(cycle_lines_entries) >= 1, (
            f"{_M_CYCLE_LINES} not emitted. All recorded names: {[e.name for e in recorded]}"
        )
        total_lines = sum(e.value for e in cycle_lines_entries)
        assert total_lines >= planted_count, (
            f"Expected {_M_CYCLE_LINES} total >= {planted_count}, got {total_lines}"
        )

        # --- cycle duration summary ---
        duration_entries = _find_entries(recorded, _M_CYCLE_DURATION)
        assert len(duration_entries) >= 1, (
            f"{_M_CYCLE_DURATION} not emitted. All recorded names: {[e.name for e in recorded]}"
        )
        assert duration_entries[0].value >= 0.0, (
            f"Expected duration >= 0, got {duration_entries[0].value}"
        )

        # --- signature count gauge with severity='error' ---
        sig_count_with_error = _find_entry_with_labels(
            recorded,
            _M_SIG_COUNT,
            {"severity": _PLANTED_SEVERITY},
        )
        assert sig_count_with_error is not None, (
            f"{_M_SIG_COUNT} with severity={_PLANTED_SEVERITY!r} not found. "
            f"All {_M_SIG_COUNT} entries: "
            f"{[e.labels for e in _find_entries(recorded, _M_SIG_COUNT)]}"
        )
        # Confirm the service_key label is present (non-empty string) and template_hash label.
        assert sig_count_with_error.labels.get("service_key"), (
            f"service_key label missing or empty: {sig_count_with_error.labels}"
        )
        assert sig_count_with_error.labels.get("template_hash"), (
            f"template_hash label missing or empty: {sig_count_with_error.labels}"
        )
        assert sig_count_with_error.value >= 1.0, (
            f"Expected {_M_SIG_COUNT} >= 1, got {sig_count_with_error.value}"
        )

        # --- signature total gauge ---
        sig_total_entries = _find_entries(recorded, _M_SIG_TOTAL)
        assert len(sig_total_entries) >= 1, (
            f"{_M_SIG_TOTAL} not emitted. All recorded names: {[e.name for e in recorded]}"
        )
        assert sig_total_entries[0].value >= 1.0, (
            f"Expected {_M_SIG_TOTAL} >= 1, got {sig_total_entries[0].value}"
        )

        # --- first_seen_ts gauge (nanoseconds) ---
        first_seen_entries = _find_entries(recorded, _M_SIG_FIRST_SEEN)
        assert len(first_seen_entries) >= 1, (
            f"{_M_SIG_FIRST_SEEN} not emitted. All recorded names: {[e.name for e in recorded]}"
        )
        # Value should be a positive nanosecond timestamp (well above 0).
        assert first_seen_entries[0].value > 0.0, (
            f"Expected {_M_SIG_FIRST_SEEN} > 0, got {first_seen_entries[0].value}"
        )

        # --- cardinality warn gauge = 0 ---
        card_warn_entries = _find_entries(recorded, _M_SIG_CARD_WARN)
        assert len(card_warn_entries) >= 1, (
            f"{_M_SIG_CARD_WARN} not emitted. All recorded names: {[e.name for e in recorded]}"
        )
        assert card_warn_entries[-1].value == 0.0, (
            f"Expected {_M_SIG_CARD_WARN} == 0, got {card_warn_entries[-1].value}"
        )

    finally:
        _cleanup_repo(db_path)


@pytest.mark.integration
@pytest.mark.slow
def test_drain_consumer_reentrancy_guard() -> None:
    """Re-entrancy guard: concurrent run_once() raises CycleInProgressError for the second.

    Acquires the consumer's _cycle_lock directly to simulate an in-flight cycle,
    then asserts run_once() immediately raises CycleInProgressError (no hanging).
    Releasing the lock lets subsequent run_once() proceed normally.

    This is deterministic (no real-VL timing dependency) — relies only on the lock
    inspection path in run_once().
    """
    require_rig_components("monitor", "victorialogs")

    repo, db_path = _make_temp_repo()
    metrics_writer = InMemoryMetricsWriter()

    try:
        with Rig.boot() as rig:

            async def _run_guard_check() -> None:
                async with httpx.AsyncClient() as http_client:
                    consumer = _make_consumer(
                        rig.urls.victorialogs,
                        http_client,
                        repo,
                        metrics_writer,
                    )
                    # Acquire the lock manually to simulate an in-flight cycle.
                    await consumer._cycle_lock.acquire()  # pyright: ignore[reportPrivateUsage]
                    try:
                        # run_once() sees the lock held → raises CycleInProgressError.
                        with pytest.raises(CycleInProgressError):
                            await consumer.run_once()
                    finally:
                        consumer._cycle_lock.release()  # pyright: ignore[reportPrivateUsage]

            asyncio.run(_run_guard_check())

    finally:
        _cleanup_repo(db_path)


@pytest.mark.integration
@pytest.mark.slow
def test_drain_http_endpoints_skipped_drain_disabled() -> None:
    """HTTP endpoint validation: skipped because the rig backend has drain disabled.

    The docker-compose.test.yml monitor service does NOT set
    HOMELAB_MONITOR_DRAIN_ENABLED, so POST /api/logs/signatures/refresh returns
    503 (drain disabled). The HTTP endpoint layer is fully covered by unit tests
    (test_logs_endpoints.py). This test documents the skip reason explicitly.
    """
    pytest.skip(
        "Rig backend drain disabled (HOMELAB_MONITOR_DRAIN_ENABLED not set in "
        "docker-compose.test.yml monitor environment). Endpoint HTTP layer validated "
        "by unit tests in test_logs_endpoints.py."
    )


@pytest.mark.integration
@pytest.mark.slow
def test_drain_metrics_in_victoriametrics_best_effort() -> None:
    """Best-effort: metrics scraped into VictoriaMetrics via vmagent.

    This path only works when the rig backend's drain consumer is ENABLED
    (so vmagent can scrape /metrics from the monitor after a cycle). Since drain
    is disabled on the rig backend, this test is skipped with an explanatory
    reason.

    The reliable core validation (in-process InMemoryMetricsWriter assertions)
    is covered by test_drain_metrics_emitted_after_real_cycle.
    """
    pytest.skip(
        "VM scrape validation skipped: requires rig backend drain enabled so a cycle "
        "emits metrics to /metrics → vmagent scrapes → VM. Rig backend has drain disabled "
        "(HOMELAB_MONITOR_DRAIN_ENABLED not set). In-process metric assertions in "
        "test_drain_metrics_emitted_after_real_cycle cover the reliable validation path."
    )
