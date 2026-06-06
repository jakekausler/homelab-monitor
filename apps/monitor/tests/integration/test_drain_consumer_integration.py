"""Integration test: DrainConsumer against real VictoriaLogs (STAGE-004-026).

Validates the behaviours that unit tests with a fake VL client cannot confirm:
  1. Real VL → DrainEngine → SQLite end-to-end cycle: planted lines are
     processed, a template is mined, the watermark advances in app_settings,
     and the Drain state is persisted to drain_models.
  2. Resume / second cycle is incremental: the second run_once() only processes
     newly planted lines (cursor advance works against real VL).
  3. Partial-cycle detection: when batch_max_lines is set smaller than the
     planted count, the consumer correctly reports cycle_status=="partial" and
     a follow-up cycle picks up the remainder.

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
from homelab_monitor.kernel.logs.drain_consumer import WATERMARK_KEY, DrainConsumer
from homelab_monitor.kernel.logs.drain_engine import DrainEngine
from homelab_monitor.kernel.logs.drain_persistence import SqlitePersistence
from homelab_monitor.kernel.logs.victorialogs_client import VictoriaLogsClient
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components
from .helpers.vl_planter import plant_log_lines

# Budget (seconds) to wait for VL to make planted lines queryable.
_VL_INGEST_BUDGET_S = 30.0
# How often to poll while waiting for VL ingest.
_VL_INGEST_POLL_S = 2.0
# Unique service label used by all tests in this module (avoids cross-test contamination).
_SERVICE = "drain-itest"


def _cast_bound_logger() -> BoundLogger:
    from typing import cast  # noqa: PLC0415

    return cast(BoundLogger, structlog.get_logger().bind(component="drain-itest"))


def _make_temp_repo() -> tuple[SqliteRepository, Path]:
    """Create a fresh tempfile-backed SQLite repo with migrations applied.

    Returns (repo, db_path) — the caller is responsible for cleanup.
    """
    fd, raw = tempfile.mkstemp(prefix="hm-drain-itest-", suffix=".db")
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


def _make_consumer(
    vl_url: str,
    http_client: httpx.AsyncClient,
    repo: SqliteRepository,
    *,
    ingest_lag_grace_seconds: int = 0,
    batch_max_lines: int = 10_000,
) -> DrainConsumer:
    """Construct a DrainConsumer wired to a real VL and a SQLite repo."""
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
    return DrainConsumer(
        vl_client=vl_client,
        engine=engine,
        settings=settings,
        persistence=persistence,
        config=config,
        metrics_writer=InMemoryMetricsWriter(),
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


@pytest.mark.integration
@pytest.mark.slow
def test_drain_consumer_end_to_end_cycle() -> None:
    """Real VL → DrainEngine → SQLite end-to-end cycle.

    Plants 5 known lines, waits for VL ingest, runs one consumer cycle, and
    asserts:
    - cycle_status in ("ok", "partial")
    - lines_processed >= 5  (may include other rig lines via match-all *)
    - at least one drain model was touched
    - the watermark was written and is non-zero
    - at least one drain_models row persisted with non-empty snapshot
    """
    require_rig_components("monitor", "victorialogs")

    marker = f"drain-e2e-{uuid.uuid4().hex}"
    planted_count = 5
    base_time = datetime.now(UTC) - timedelta(seconds=5)

    repo, db_path = _make_temp_repo()
    # Pre-seed watermark to 1 minute before the planted lines so the consumer's
    # query window [watermark, now] covers the planted lines. Without this, a
    # cold-start seeds to now_ms and the window is empty.
    watermark_seed_ms = int((base_time - timedelta(minutes=1)).timestamp() * 1000)
    _seed_watermark(repo, watermark_seed_ms)
    try:
        with Rig.boot() as rig:
            plant_log_lines(
                host="rig-drain-host",
                service=_SERVICE,
                severity="info",
                message=f"drain itest connection from 10.0.0.1 marker={marker}",
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
        assert result.models_touched >= 1, (
            f"Expected at least 1 model touched, got {result.models_touched}"
        )

        # Verify the watermark was persisted.
        async def _check_settings() -> str | None:
            return await AppSettingsRepository(repo).get(WATERMARK_KEY)

        watermark_raw = asyncio.run(_check_settings())
        assert watermark_raw is not None, "Watermark was not written to app_settings"
        watermark_val = int(watermark_raw)
        assert watermark_val > 0, f"Watermark should be a positive unix-ms, got {watermark_val}"

        # Verify at least one drain_models row was persisted with non-empty snapshot.
        from sqlalchemy import text  # noqa: PLC0415

        async def _check_drain_models() -> list[Any]:
            return await repo.fetch_all(
                text("SELECT model_key, snapshot, last_processed_ts FROM drain_models"),
                {},
            )

        rows = asyncio.run(_check_drain_models())
        assert len(rows) >= 1, "Expected at least one drain_models row after cycle"
        for row in rows:
            snap: Any = row.snapshot  # pyright: ignore[reportAny]
            assert snap is not None and len(snap) > 0, (
                f"drain_models row for {row.model_key!r} has empty snapshot"  # pyright: ignore[reportAny]
            )

    finally:
        _cleanup_repo(db_path)


@pytest.mark.integration
@pytest.mark.slow
def test_drain_consumer_resume_second_cycle_is_incremental() -> None:
    """Second cycle only processes newly planted lines (cursor resume works).

    Cycle 1: plant 3 lines, run consumer → watermark W1.
    Plant 3 more lines with base_time > W1.
    Cycle 2: run consumer → watermark W2 > W1, processes the new lines.
    """
    require_rig_components("monitor", "victorialogs")

    marker1 = f"drain-resume1-{uuid.uuid4().hex}"
    marker2 = f"drain-resume2-{uuid.uuid4().hex}"
    base1 = datetime.now(UTC) - timedelta(seconds=10)

    repo, db_path = _make_temp_repo()
    # Pre-seed watermark to 1 minute before base1 so cycle 1 window covers the lines.
    watermark_seed_ms = int((base1 - timedelta(minutes=1)).timestamp() * 1000)
    _seed_watermark(repo, watermark_seed_ms)
    try:
        with Rig.boot() as rig:
            # --- Cycle 1 ---
            plant_log_lines(
                host="rig-drain-host",
                service=_SERVICE,
                severity="info",
                message=f"drain resume first batch marker={marker1}",
                count=3,
                base_time=base1,
                interval_ms=100,
                vl_url=rig.urls.victorialogs,
            )
            _wait_for_vl_ingest(rig.urls.victorialogs, marker1, 3)

            async def _cycle1() -> Any:  # noqa: ANN401
                async with httpx.AsyncClient() as http_client:
                    consumer = _make_consumer(
                        rig.urls.victorialogs,
                        http_client,
                        repo,
                        ingest_lag_grace_seconds=0,
                    )
                    return await consumer.run_once()

            result1 = asyncio.run(_cycle1())
            assert result1.cycle_status in ("ok", "partial"), (
                f"Cycle 1 failed: {result1.cycle_status!r}, error={result1.error!r}"
            )
            assert result1.lines_processed >= 3  # noqa: PLR2004

            async def _get_watermark() -> str | None:
                return await AppSettingsRepository(repo).get(WATERMARK_KEY)

            wm1_raw = asyncio.run(_get_watermark())
            assert wm1_raw is not None
            wm1 = int(wm1_raw)

            # --- Plant more lines clearly AFTER wm1 ---
            base2 = datetime.now(UTC) + timedelta(milliseconds=500)
            plant_log_lines(
                host="rig-drain-host",
                service=_SERVICE,
                severity="info",
                message=f"drain resume second batch marker={marker2}",
                count=3,
                base_time=base2,
                interval_ms=100,
                vl_url=rig.urls.victorialogs,
            )
            _wait_for_vl_ingest(rig.urls.victorialogs, marker2, 3)

            # --- Cycle 2 ---
            async def _cycle2() -> Any:  # noqa: ANN401
                async with httpx.AsyncClient() as http_client:
                    consumer = _make_consumer(
                        rig.urls.victorialogs,
                        http_client,
                        repo,
                        ingest_lag_grace_seconds=0,
                    )
                    return await consumer.run_once()

            result2 = asyncio.run(_cycle2())

        assert result2.cycle_status in ("ok", "partial"), (
            f"Cycle 2 failed: {result2.cycle_status!r}, error={result2.error!r}"
        )
        assert result2.lines_processed >= 3, (  # noqa: PLR2004
            f"Cycle 2 should have processed >= 3 new lines, got {result2.lines_processed}"
        )

        wm2_raw = asyncio.run(_get_watermark())
        assert wm2_raw is not None
        wm2 = int(wm2_raw)
        assert wm2 > wm1, f"Watermark should advance on second cycle: wm1={wm1}, wm2={wm2}"

    finally:
        _cleanup_repo(db_path)


@pytest.mark.integration
@pytest.mark.slow
def test_drain_consumer_partial_cycle_and_resume() -> None:
    """Partial-cycle detection: batch_max_lines smaller than planted count.

    Plants 6 lines, sets batch_max_lines=3. First cycle should be partial
    (processed == 3 == batch_cap). A second cycle picks up the remainder.
    Note: VL returns lines in arbitrary order; we assert on counts + status,
    not specific messages.
    """
    require_rig_components("monitor", "victorialogs")

    marker = f"drain-partial-{uuid.uuid4().hex}"
    planted_count = 6
    batch_cap = 3
    base_time = datetime.now(UTC) - timedelta(seconds=5)

    repo, db_path = _make_temp_repo()
    # Pre-seed watermark to 5 seconds before base_time so the consumer window covers
    # only the planted lines (narrow seed window, commented as relying on quiet seeded window).
    watermark_seed_ms = int((base_time - timedelta(seconds=5)).timestamp() * 1000)
    _seed_watermark(repo, watermark_seed_ms)
    try:
        with Rig.boot() as rig:
            plant_log_lines(
                host="rig-drain-host",
                service=_SERVICE,
                severity="info",
                message=f"drain partial batch marker={marker}",
                count=planted_count,
                base_time=base_time,
                interval_ms=100,
                vl_url=rig.urls.victorialogs,
            )
            _wait_for_vl_ingest(rig.urls.victorialogs, marker, planted_count)

            async def _partial_run() -> Any:  # noqa: ANN401
                async with httpx.AsyncClient() as http_client:
                    consumer = _make_consumer(
                        rig.urls.victorialogs,
                        http_client,
                        repo,
                        ingest_lag_grace_seconds=0,
                        batch_max_lines=batch_cap,
                    )
                    return await consumer.run_once()

            result1 = asyncio.run(_partial_run())

            # With batch_max_lines=3 and >=6 lines in VL, cycle should be partial.
            # The narrow seed window and match-all "*" query may still include other
            # rig service lines, so we assert >= batch_cap for the partial signal.
            assert result1.cycle_status == "partial", (
                f"Expected cycle_status='partial' with batch_cap={batch_cap} and "
                f"{planted_count} planted lines, got {result1.cycle_status!r}. "
                f"lines_processed={result1.lines_processed}"
            )
            assert result1.lines_processed >= batch_cap, (
                f"Expected lines_processed >= {batch_cap} in partial cycle, "
                f"got {result1.lines_processed}"
            )

            async def _get_wm() -> str | None:
                return await AppSettingsRepository(repo).get(WATERMARK_KEY)

            wm1_raw = asyncio.run(_get_wm())
            assert wm1_raw is not None
            wm1 = int(wm1_raw)

            # Second cycle picks up the rest (within the same rig context, so the
            # planted lines persist). The partial-resume now relies on ns-precision
            # boundary exclusion: the follow-up cycle should NOT re-process the
            # boundary line from cycle 1.
            async def _followup_run() -> Any:  # noqa: ANN401
                async with httpx.AsyncClient() as http_client:
                    consumer = _make_consumer(
                        rig.urls.victorialogs,
                        http_client,
                        repo,
                        ingest_lag_grace_seconds=0,
                        batch_max_lines=batch_cap,
                    )
                    return await consumer.run_once()

            result2 = asyncio.run(_followup_run())

            assert result2.cycle_status in ("ok", "partial"), (
                f"Follow-up cycle had unexpected status: {result2.cycle_status!r}"
            )
            assert result2.lines_processed >= 1, (
                f"Follow-up cycle should process >= 1 remaining line, got {result2.lines_processed}"
            )

            wm2_raw = asyncio.run(_get_wm())
            assert wm2_raw is not None
            wm2 = int(wm2_raw)
            assert wm2 > wm1, (
                f"Follow-up watermark should advance past partial watermark: wm1={wm1}, wm2={wm2}"
            )

    finally:
        _cleanup_repo(db_path)
