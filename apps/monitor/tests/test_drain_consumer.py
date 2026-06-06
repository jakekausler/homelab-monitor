"""Tests for DrainConsumer (STAGE-004-026).

DB: tempfile-backed SQLite + alembic head via the `repo` fixture (conftest).
VL client: a fake that yields lines in order, with optional failure injection.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import cast

import pytest
import structlog
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.config import DrainConfig
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.drain_consumer import (
    WATERMARK_KEY,
    CycleInProgressError,
    DrainConsumer,
    DrainCycleResult,
    _now_ms,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.logs.drain_engine import DrainEngine
from homelab_monitor.kernel.logs.drain_persistence import SqlitePersistence
from homelab_monitor.kernel.logs.histogram import ms_to_iso
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VictoriaLogsClientError,
    VlLogLine,
)
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter


def _log() -> BoundLogger:
    return cast(BoundLogger, structlog.get_logger().bind(component="test"))


class _FakeVlClient:
    """Stand-in for VictoriaLogsClient exposing only stream_query.

    ``lines`` are yielded in order; ``raise_after`` (if set) raises a
    VictoriaLogsClientError after that many lines have been yielded.
    Records the last (start, end, limit) it was called with.
    """

    def __init__(
        self,
        lines: Sequence[VlLogLine],
        *,
        raise_after: int | None = None,
    ) -> None:
        self._lines = list(lines)
        self._raise_after = raise_after
        self.last_start: str | None = None
        self.last_end: str | None = None
        self.last_limit: int | None = None
        self.call_count = 0

    async def stream_query(
        self, *, expr: str, start: str, end: str, limit: int
    ) -> AsyncIterator[VlLogLine]:
        del expr
        self.call_count += 1
        self.last_start = start
        self.last_end = end
        self.last_limit = limit
        emitted = 0
        for line in self._lines:
            if emitted >= limit:
                break
            if self._raise_after is not None and emitted >= self._raise_after:
                msg = "fake vl boom"
                raise VictoriaLogsClientError(msg, 503)
            yield line
            emitted += 1  # noqa: SIM113


class _WindowedFakeVlClient:
    """Fake VL that RESPECTS the [start, end] inclusive window (filters by ns).

    Unlike _FakeVlClient (which ignores start/end), this filters its line pool by
    _iso_to_ns(line.ts) >= _iso_to_ns(start), so multi-cycle resume / boundary
    exclusion can be asserted. Records every call's start.
    """

    def __init__(self, lines: Sequence[VlLogLine]) -> None:
        self._lines = list(lines)
        self.starts: list[str] = []
        self.call_count = 0

    async def stream_query(
        self, *, expr: str, start: str, end: str, limit: int
    ) -> AsyncIterator[VlLogLine]:
        del expr, end
        from homelab_monitor.kernel.logs.pagination import (  # noqa: PLC0415
            _iso_to_ns,  # pyright: ignore[reportPrivateUsage]
        )

        self.call_count += 1
        self.starts.append(start)
        start_ns = _iso_to_ns(start)
        emitted = 0
        for line in self._lines:
            if emitted >= limit:
                break
            if _iso_to_ns(line.timestamp) >= start_ns:
                yield line
                emitted += 1


def _as_client(fake: _FakeVlClient | _WindowedFakeVlClient) -> VictoriaLogsClient:
    return cast(VictoriaLogsClient, fake)


def _vl(msg: str, ts: str, *, service: str = "pihole") -> VlLogLine:
    return VlLogLine(timestamp=ts, message=msg, stream="stdout", fields={"service": service})


def _consumer(  # noqa: PLR0913
    repo: SqliteRepository,
    fake: _FakeVlClient | _WindowedFakeVlClient,
    *,
    batch_max_lines: int = 50,
    ingest_lag_grace_seconds: int = 0,
    interval_seconds: int = 300,
    signature_cardinality_warn_threshold: int = 100_000,
) -> tuple[DrainConsumer, AppSettingsRepository, SqlitePersistence, InMemoryMetricsWriter]:
    persistence = SqlitePersistence(repo)
    engine = DrainEngine(persistence)
    settings = AppSettingsRepository(repo)
    config = DrainConfig(
        interval_seconds=interval_seconds,
        batch_max_lines=batch_max_lines,
        ingest_lag_grace_seconds=ingest_lag_grace_seconds,
        enabled=True,
        signature_cardinality_warn_threshold=signature_cardinality_warn_threshold,
    )
    metrics_writer = InMemoryMetricsWriter()
    consumer = DrainConsumer(
        vl_client=_as_client(fake),
        engine=engine,
        settings=settings,
        persistence=persistence,
        config=config,
        metrics_writer=metrics_writer,
        log=_log(),
    )
    return consumer, settings, persistence, metrics_writer


async def test_cold_start_seed_no_watermark_no_models(repo: SqliteRepository) -> None:
    fake = _FakeVlClient([])
    consumer, settings, _p, _mw = _consumer(repo, fake, ingest_lag_grace_seconds=0)
    result = await consumer.run_once()
    assert result.cycle_status == "ok"
    assert result.lines_processed == 0
    assert result.models_touched == 0
    # Watermark should have been written
    raw_watermark = await settings.get(WATERMARK_KEY)
    assert raw_watermark is not None
    watermark_int = int(raw_watermark)
    assert watermark_int > 0


async def test_resume_from_existing_watermark(repo: SqliteRepository) -> None:
    from homelab_monitor.kernel.logs.pagination import (  # noqa: PLC0415
        _ns_to_iso,  # pyright: ignore[reportPrivateUsage]
    )

    consumer, settings, _p, _mw = _consumer(
        repo,
        _FakeVlClient(
            [
                _vl("a", ms_to_iso(2000)),
                _vl("b", ms_to_iso(3000)),
            ]
        ),
        batch_max_lines=50,
        ingest_lag_grace_seconds=0,
    )
    await settings.set(WATERMARK_KEY, "1000")
    result = await consumer.run_once()
    assert result.lines_processed == 2  # noqa: PLR2004
    fake: _FakeVlClient = cast(_FakeVlClient, consumer._vl_client)  # pyright: ignore[reportPrivateUsage]
    assert fake.last_start == _ns_to_iso(1000 * 1_000_000 + 1)
    assert result.cycle_status == "ok"


async def test_resume_from_max_cursor_when_no_watermark(repo: SqliteRepository) -> None:
    from homelab_monitor.kernel.logs.pagination import (  # noqa: PLC0415
        _ns_to_iso,  # pyright: ignore[reportPrivateUsage]
    )

    consumer, _settings, persistence, _mw = _consumer(
        repo, _FakeVlClient([]), ingest_lag_grace_seconds=0
    )
    await persistence.persist(
        model_key="m-seed",
        snapshot=b"x",
        line_count=0,
        template_count=0,
        last_processed_ts=4242,
        first_seen_map_json="{}",
        updated_at=1,
    )
    result = await consumer.run_once()
    fake: _FakeVlClient = cast(_FakeVlClient, consumer._vl_client)  # pyright: ignore[reportPrivateUsage]
    assert fake.last_start == _ns_to_iso(4242 * 1_000_000 + 1)
    assert result.lines_processed == 0


async def test_partial_cycle_advances_to_max_ts_seen(repo: SqliteRepository) -> None:
    consumer, settings, _p, _mw = _consumer(
        repo,
        _FakeVlClient(
            [
                _vl("a", ms_to_iso(9000)),
                _vl("b", ms_to_iso(5000)),
            ]
        ),
        batch_max_lines=2,
        ingest_lag_grace_seconds=0,
    )
    await settings.set(WATERMARK_KEY, "1000")
    result = await consumer.run_once()
    assert result.cycle_status == "partial"
    assert result.lines_processed == 2  # noqa: PLR2004
    raw_watermark = await settings.get(WATERMARK_KEY)
    assert raw_watermark == "9000"


async def test_complete_cycle_advances_to_query_end(repo: SqliteRepository) -> None:
    consumer, settings, _p, _mw = _consumer(
        repo,
        _FakeVlClient(
            [
                _vl("a", ms_to_iso(2000)),
                _vl("b", ms_to_iso(3000)),
                _vl("c", ms_to_iso(4000)),
            ]
        ),
        batch_max_lines=50,
        ingest_lag_grace_seconds=0,
    )
    await settings.set(WATERMARK_KEY, "1000")
    result = await consumer.run_once()
    assert result.cycle_status == "ok"
    assert result.lines_processed == 3  # noqa: PLR2004
    assert result.new_templates >= 1
    assert result.models_touched == 1
    raw_watermark = await settings.get(WATERMARK_KEY)
    # With lag=0, query_end == started_at
    assert raw_watermark is not None
    assert int(raw_watermark) == result.started_at


async def test_empty_cycle_advances_watermark(repo: SqliteRepository) -> None:
    consumer, settings, _p, _mw = _consumer(
        repo,
        _FakeVlClient([]),
        batch_max_lines=50,
        ingest_lag_grace_seconds=0,
    )
    await settings.set(WATERMARK_KEY, "1000")
    result = await consumer.run_once()
    assert result.cycle_status == "ok"
    assert result.lines_processed == 0
    assert result.models_touched == 0
    raw_watermark = await settings.get(WATERMARK_KEY)
    assert raw_watermark is not None
    assert int(raw_watermark) > 1000  # noqa: PLR2004


async def test_vl_failure_does_not_advance_watermark(repo: SqliteRepository) -> None:
    consumer, settings, _p, _mw = _consumer(
        repo,
        _FakeVlClient(
            [
                _vl("a", ms_to_iso(2000)),
                _vl("b", ms_to_iso(3000)),
                _vl("c", ms_to_iso(4000)),
            ],
            raise_after=1,
        ),
        batch_max_lines=50,
        ingest_lag_grace_seconds=0,
    )
    await settings.set(WATERMARK_KEY, "1000")
    result = await consumer.run_once()
    assert result.cycle_status == "failed"
    assert result.error is not None
    assert "fake vl boom" in result.error
    assert result.lines_processed == 1
    raw_watermark = await settings.get(WATERMARK_KEY)
    assert raw_watermark == "1000"


async def test_early_return_when_query_end_le_watermark(repo: SqliteRepository) -> None:
    consumer, settings, _p, _mw = _consumer(
        repo,
        _FakeVlClient([_vl("x", "2999-01-01T00:00:00Z")]),
        ingest_lag_grace_seconds=0,
    )
    future_watermark = _now_ms() + 600_000
    await settings.set(WATERMARK_KEY, str(future_watermark))
    result = await consumer.run_once()
    assert result.cycle_status == "ok"
    assert result.lines_processed == 0
    fake: _FakeVlClient = cast(_FakeVlClient, consumer._vl_client)  # pyright: ignore[reportPrivateUsage]
    assert fake.call_count == 0
    raw_watermark = await settings.get(WATERMARK_KEY)
    assert raw_watermark == str(future_watermark)


async def test_corrupt_watermark_reseeds(repo: SqliteRepository) -> None:
    consumer, settings, _p, _mw = _consumer(
        repo,
        _FakeVlClient([]),
        ingest_lag_grace_seconds=0,
    )
    await settings.set(WATERMARK_KEY, "not-an-int")
    result = await consumer.run_once()
    assert result.cycle_status == "ok"
    raw_watermark = await settings.get(WATERMARK_KEY)
    assert raw_watermark is not None
    int(raw_watermark)  # Should not raise


async def test_run_forever_runs_until_cancelled(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer, _settings, _p, _mw = _consumer(repo, _FakeVlClient([]))
    calls: dict[str, int] = {"run_once": 0, "sleep": 0}
    real_run_once = consumer.run_once

    async def counting_run_once() -> DrainCycleResult:
        calls["run_once"] += 1
        return await real_run_once()

    async def fake_sleep(_seconds: float) -> None:
        calls["sleep"] += 1
        if calls["sleep"] >= 2:  # noqa: PLR2004
            raise asyncio.CancelledError

    monkeypatch.setattr(consumer, "run_once", counting_run_once)
    monkeypatch.setattr("homelab_monitor.kernel.logs.drain_consumer.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await consumer.run_forever()
    assert calls["run_once"] == 2  # noqa: PLR2004


async def test_run_forever_backstop(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer, _s, _p, _mw = _consumer(repo, _FakeVlClient([]))
    seq = iter([RuntimeError("boom"), asyncio.CancelledError()])

    async def flaky_run_once() -> DrainCycleResult:
        raise next(seq)

    async def noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(consumer, "run_once", flaky_run_once)
    monkeypatch.setattr("homelab_monitor.kernel.logs.drain_consumer.asyncio.sleep", noop_sleep)
    with pytest.raises(asyncio.CancelledError):
        await consumer.run_forever()


async def test_start_task_idempotent_and_stop_task(repo: SqliteRepository) -> None:
    consumer, _settings, _p, _mw = _consumer(repo, _FakeVlClient([]))
    consumer.start_task()
    task1 = consumer._task  # pyright: ignore[reportPrivateUsage]
    consumer.start_task()
    task2 = consumer._task  # pyright: ignore[reportPrivateUsage]
    assert task1 is task2
    await consumer.stop_task()
    assert consumer._task is None  # pyright: ignore[reportPrivateUsage]
    await consumer.stop_task()


async def test_malformed_line_timestamp_falls_back(repo: SqliteRepository) -> None:
    consumer, settings, _p, _mw = _consumer(
        repo,
        _FakeVlClient([_vl("msg", "not-iso")]),
        batch_max_lines=50,
        ingest_lag_grace_seconds=0,
    )
    await settings.set(WATERMARK_KEY, "1000")
    result = await consumer.run_once()
    assert result.cycle_status == "ok"
    assert result.lines_processed == 1
    assert result.error is None


async def test_naive_vl_timestamp_parsed_as_utc(repo: SqliteRepository) -> None:
    """_parse_iso_ms line 77: naive timestamp (no Z/offset) is treated as UTC.

    Existing tests always use ms_to_iso() which produces tz-aware strings.
    This test feeds a naive ISO string to exercise the ``dt.tzinfo is None``
    branch that replaces tzinfo with UTC.
    """
    # "2026-06-05T12:00:00" has no timezone — triggers line 77.
    naive_ts = "2026-06-05T12:00:00"
    consumer, settings, _p, _mw = _consumer(
        repo,
        _FakeVlClient([_vl("msg", naive_ts)]),
        batch_max_lines=50,
        ingest_lag_grace_seconds=0,
    )
    await settings.set(WATERMARK_KEY, "1000")
    result = await consumer.run_once()
    # The cycle must complete without error and the line must be processed.
    assert result.cycle_status in ("ok", "partial")
    assert result.lines_processed == 1
    assert result.error is None


async def test_repeated_lines_produce_is_new_false(repo: SqliteRepository) -> None:
    """drain_consumer.py branch 199→201: is_new=False path (skip new_templates increment).

    After drain3 has seen a template hash once, subsequent identical lines return
    is_new=False, exercising the branch that skips line 200 and jumps to 201.
    We verify this empirically: new_templates < lines_processed means at least one
    line was is_new=False.

    Four identical messages are used: the first line creates the template
    (is_new=True), and lines 2-4 match the same hash (is_new=False).
    """
    identical_msg = "connection accepted from 192.168.1.1"
    lines = [_vl(identical_msg, ms_to_iso(1000 + i * 10)) for i in range(4)]
    consumer, settings, _p, _mw = _consumer(
        repo,
        _FakeVlClient(lines),
        batch_max_lines=50,
        ingest_lag_grace_seconds=0,
    )
    await settings.set(WATERMARK_KEY, "1")
    result = await consumer.run_once()
    assert result.cycle_status in ("ok", "partial")
    assert result.lines_processed == 4  # noqa: PLR2004
    # At least one line must have been is_new=False (template hash repeated).
    assert result.new_templates < result.lines_processed


async def test_partial_resume_start_excludes_boundary_ms(repo: SqliteRepository) -> None:
    """Partial cycle: next query START is _ns_to_iso(max_ts_seen*1e6 + 1), so the
    boundary-ms lines are NOT re-fed (no double-count, no wedge)."""
    from homelab_monitor.kernel.logs.pagination import (  # noqa: PLC0415
        _ns_to_iso,  # pyright: ignore[reportPrivateUsage]
    )

    fake = _WindowedFakeVlClient([_vl("a", ms_to_iso(5000)), _vl("b", ms_to_iso(9000))])
    consumer, settings, _p, _mw = _consumer(
        repo, fake, batch_max_lines=2, ingest_lag_grace_seconds=0
    )
    await settings.set(WATERMARK_KEY, "1000")
    result = await consumer.run_once()
    assert result.cycle_status == "partial"
    assert fake.starts[0] == _ns_to_iso(1000 * 1_000_000 + 1)
    assert (await settings.get(WATERMARK_KEY)) == "9000"
    result2 = await consumer.run_once()
    assert fake.starts[1] == _ns_to_iso(9000 * 1_000_000 + 1)
    assert result2.lines_processed == 0


async def test_same_ms_burst_does_not_wedge(repo: SqliteRepository) -> None:
    """C1 wedge: batch_cap lines all at ms T == watermark. Old code pinned the
    watermark at T forever; the ns-advance start lets the next cycle progress."""
    from homelab_monitor.kernel.logs.pagination import (  # noqa: PLC0415
        _ns_to_iso,  # pyright: ignore[reportPrivateUsage]
    )

    burst = [_vl("x", ms_to_iso(1000)), _vl("y", ms_to_iso(1000)), _vl("z", ms_to_iso(2000))]
    fake = _WindowedFakeVlClient(burst)
    consumer, settings, _p, _mw = _consumer(
        repo, fake, batch_max_lines=2, ingest_lag_grace_seconds=0
    )
    await settings.set(WATERMARK_KEY, "500")
    r1 = await consumer.run_once()
    assert r1.cycle_status == "partial"
    assert r1.lines_processed == 2  # noqa: PLR2004
    r2 = await consumer.run_once()
    assert fake.starts[1] == _ns_to_iso(1000 * 1_000_000 + 1)
    assert r2.lines_processed == 1


async def test_boundary_line_not_double_counted(repo: SqliteRepository) -> None:
    """The boundary line is fed exactly once across a partial->resume cycle pair:
    no line_count / cluster.size inflation."""
    from sqlalchemy import text  # noqa: PLC0415

    msg = "connection accepted from 10.0.0.1"
    fake = _WindowedFakeVlClient([_vl(msg, ms_to_iso(3000)), _vl(msg, ms_to_iso(5000))])
    consumer, settings, _p, _mw = _consumer(
        repo, fake, batch_max_lines=2, ingest_lag_grace_seconds=0
    )
    await settings.set(WATERMARK_KEY, "1000")
    await consumer.run_once()
    await consumer.run_once()
    rows = await repo.fetch_all(
        text("SELECT line_count FROM drain_models WHERE model_key = :k"), {"k": "pihole"}
    )
    assert rows
    assert int(rows[0].line_count) == 2  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Re-entrancy guard tests (STAGE-004-027)
# ---------------------------------------------------------------------------


async def test_run_once_raises_when_lock_held(repo: SqliteRepository) -> None:
    """run_once raises CycleInProgressError immediately when the cycle lock is held."""
    consumer, *_ = _consumer(repo, _FakeVlClient([]))
    await consumer._cycle_lock.acquire()  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(CycleInProgressError):
            await consumer.run_once()
    finally:
        consumer._cycle_lock.release()  # pyright: ignore[reportPrivateUsage]


async def test_is_cycle_running_and_cycle_started_at(repo: SqliteRepository) -> None:
    """is_cycle_running reflects lock state; cycle_started_at is None when idle."""
    consumer, *_ = _consumer(repo, _FakeVlClient([]))
    assert not consumer.is_cycle_running()
    assert consumer.cycle_started_at is None

    await consumer._cycle_lock.acquire()  # pyright: ignore[reportPrivateUsage]
    assert consumer.is_cycle_running()
    consumer._cycle_lock.release()  # pyright: ignore[reportPrivateUsage]
    assert not consumer.is_cycle_running()


async def test_run_once_clears_started_at_after_completion(
    repo: SqliteRepository,
) -> None:
    """cycle_started_at is None after a normal cycle completes."""
    consumer, settings, _p, _mw = _consumer(repo, _FakeVlClient([]), ingest_lag_grace_seconds=0)
    await settings.set(WATERMARK_KEY, "1")
    await consumer.run_once()
    assert consumer.cycle_started_at is None


async def test_run_forever_skips_on_cycle_in_progress(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_forever continues past CycleInProgressError (debug-log, no crash)."""
    from structlog.testing import capture_logs  # noqa: PLC0415

    consumer, *_ = _consumer(repo, _FakeVlClient([]), interval_seconds=0)

    calls: dict[str, int] = {"n": 0}

    async def fake_run_once() -> DrainCycleResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise CycleInProgressError(started_at=123)
        raise asyncio.CancelledError

    consumer.run_once = fake_run_once  # type: ignore[method-assign]

    async def noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("homelab_monitor.kernel.logs.drain_consumer.asyncio.sleep", noop_sleep)

    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await consumer.run_forever()

    assert calls["n"] == 2  # noqa: PLR2004
    skip_logs = [e for e in logs if e.get("event") == "drain_consumer.cycle_skipped_in_progress"]
    assert len(skip_logs) == 1
