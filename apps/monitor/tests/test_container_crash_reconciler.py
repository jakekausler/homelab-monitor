"""Tests for ContainerCrashReconciler (STAGE-004-032).

Uses the `repo` fixture (real in-memory migrated DB), monkeypatch for VL
stubbing, and MemoryRetainingMetricsWriter for metric assertion.

Project test conventions:
- asyncio_mode=auto — bare async def, no @pytest.mark.asyncio decorator
- noqa: PLR2004 for magic number assertions
- pyright: ignore[reportPrivateUsage] for private symbol access
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog

import homelab_monitor.kernel.metrics.container_crash_reconciler as _reconciler_mod
from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.logs.crash_enrichments_repo import CrashEnrichmentsRepository
from homelab_monitor.kernel.logs.log_window_fetcher import LogWindowResult
from homelab_monitor.kernel.logs.models import LogLine
from homelab_monitor.kernel.metrics.container_crash_reconciler import (
    ContainerCrashReconciler,
    _parse_anchor,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, MemoryRetainingMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    repo: SqliteRepository,
    http: httpx.AsyncClient,
    vm: MemoryRetainingMetricsWriter | None = None,
) -> CollectorContext:
    """Minimal CollectorContext for ContainerCrashReconciler."""
    return CollectorContext(
        config=CollectorConfig(name="container_crash_reconciler"),
        db=repo,
        vm=vm or MemoryRetainingMetricsWriter(),
        vl=InMemoryLogsWriter(),
        http=http,
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(
            collector="container_crash_reconciler",
        ),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


def _log_line(msg: str) -> LogLine:
    return LogLine(
        timestamp="2026-06-07T00:00:00Z",
        message=msg,
        stream="s",
        severity="error",
        host=None,
        service=None,
        fields={},
    )


async def _seed_docker_container(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    target_id: str,
    name: str,
    status: str = "running",
    exit_code: int = 0,
    finished_at: str | None = None,
    image: str = "ubuntu:22.04",
    compose_project: str | None = None,
    compose_service: str | None = None,
) -> str:
    """Seed a docker container via the production upsert path."""
    now = utc_now_iso()
    async with repo.transaction() as conn:
        return await TargetsRepository.upsert_docker_container_conn(
            conn,
            container_id=target_id,
            logical_key_kind="name",
            logical_key=name,
            name=name,
            status=status,
            image=image,
            restart_count=0,
            exit_code=exit_code,
            healthcheck=None,
            network_mode="bridge",
            labels={},
            now=now,
            cpu_pct=None,
            mem_mib=None,
            compose_project=compose_project,
            compose_service=compose_service,
            compose_file_path=None,
            finished_at=finished_at,
        )


class _FakeLogWindowFetcher:
    """Stub LogWindowFetcher that returns 2 lines without hitting VictoriaLogs."""

    def __init__(self, vl_client: object) -> None:
        pass

    async def fetch(
        self,
        logs_ql: str,
        anchor_ts: datetime,
        window_before_s: int = 60,
        window_after_s: int = 60,
        limit: int = 200,
    ) -> LogWindowResult:
        return LogWindowResult(
            lines=[_log_line("crash log a"), _log_line("crash log b")],
            truncated=False,
            degraded=False,
            window_start=anchor_ts - timedelta(seconds=window_before_s),
            window_end=anchor_ts + timedelta(seconds=window_after_s),
            queried_at=anchor_ts,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_crashed_container_enriched_and_counter_emitted(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crashed container (exited, exit_code=1) gets enriched; running container is skipped."""
    monkeypatch.setattr(_reconciler_mod, "LogWindowFetcher", _FakeLogWindowFetcher)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    await _seed_docker_container(
        repo,
        target_id="c1",
        name="crashy",
        status="exited",
        exit_code=1,
        finished_at="2026-06-07T00:00:00Z",
    )
    await _seed_docker_container(
        repo,
        target_id="c2",
        name="healthy",
        status="running",
        exit_code=0,
    )

    vm = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http:
        result = await ContainerCrashReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True

    crash_repo = CrashEnrichmentsRepository(repo)
    rows = await crash_repo.list_for_container("crashy")
    assert len(rows) == 1
    assert rows[0].line_count == 2  # noqa: PLR2004

    # Running container must NOT be enriched
    healthy_rows = await crash_repo.list_for_container("healthy")
    assert len(healthy_rows) == 0

    # Counter emitted for the crashed container
    counter_entries = [e for e in vm.recorded if e.name == "homelab_container_crash_total"]
    assert len(counter_entries) == 1
    assert counter_entries[0].value == 1.0
    assert counter_entries[0].labels["container_name"] == "crashy"
    assert counter_entries[0].labels["exit_code"] == "1"

    # Gauge emitted with line_count
    gauge_entries = [e for e in vm.recorded if e.name == "homelab_container_crash_with_log_context"]
    assert len(gauge_entries) == 1
    assert gauge_entries[0].value == 2.0  # noqa: PLR2004


async def test_second_tick_dedups(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second tick with same finished_at does not re-insert or re-emit counter."""
    monkeypatch.setattr(_reconciler_mod, "LogWindowFetcher", _FakeLogWindowFetcher)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    await _seed_docker_container(
        repo,
        target_id="c1",
        name="crashy",
        status="exited",
        exit_code=1,
        finished_at="2026-06-07T00:00:00Z",
    )

    vm = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http:
        await ContainerCrashReconciler().run(_ctx(repo, http, vm=vm))
        await ContainerCrashReconciler().run(_ctx(repo, http, vm=vm))

    crash_repo = CrashEnrichmentsRepository(repo)
    rows = await crash_repo.list_for_container("crashy")
    assert len(rows) == 1

    counter_entries = [e for e in vm.recorded if e.name == "homelab_container_crash_total"]
    assert len(counter_entries) == 1


async def test_degraded_vl_still_inserts_and_emits(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degraded VL result still inserts the row with degraded=True and emits counter."""

    class _DegradedFetcher:
        def __init__(self, vl_client: object) -> None:
            pass

        async def fetch(
            self,
            logs_ql: str,
            anchor_ts: datetime,
            window_before_s: int = 60,
            window_after_s: int = 60,
            limit: int = 200,
        ) -> LogWindowResult:
            return LogWindowResult(
                lines=[],
                truncated=False,
                degraded=True,
                window_start=anchor_ts - timedelta(seconds=window_before_s),
                window_end=anchor_ts + timedelta(seconds=window_after_s),
                queried_at=anchor_ts,
            )

    monkeypatch.setattr(_reconciler_mod, "LogWindowFetcher", _DegradedFetcher)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    await _seed_docker_container(
        repo,
        target_id="c1",
        name="crashy",
        status="exited",
        exit_code=1,
        finished_at="2026-06-07T00:00:00Z",
    )

    vm = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http:
        result = await ContainerCrashReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True

    crash_repo = CrashEnrichmentsRepository(repo)
    rows = await crash_repo.list_for_container("crashy")
    assert len(rows) == 1
    assert rows[0].degraded is True
    assert rows[0].line_count == 0

    counter_entries = [e for e in vm.recorded if e.name == "homelab_container_crash_total"]
    assert len(counter_entries) == 1

    gauge_entries = [e for e in vm.recorded if e.name == "homelab_container_crash_with_log_context"]
    assert gauge_entries[0].value == 0.0


async def test_missing_finished_at_uses_stable_unknown_key(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container with finished_at=None uses stable 'unknown:<container_id>' dedup key.

    The finished_at column in the crash row is set to 'unknown:<container_id>'
    rather than a wall-clock ISO string so that the same no-FinishedAt container
    deduplicated on a second tick instead of generating a new row each time.
    """
    monkeypatch.setattr(_reconciler_mod, "LogWindowFetcher", _FakeLogWindowFetcher)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    await _seed_docker_container(
        repo,
        target_id="c1",
        name="no-fa",
        status="exited",
        exit_code=1,
        finished_at=None,
    )

    async with httpx.AsyncClient() as http:
        result = await ContainerCrashReconciler().run(_ctx(repo, http))

    assert result.ok is True

    crash_repo = CrashEnrichmentsRepository(repo)
    rows = await crash_repo.list_for_container("no-fa")
    assert len(rows) == 1
    # The container_id seeded above is "c1"; dedup key = "unknown:c1"
    assert rows[0].finished_at == "unknown:c1"


async def test_missing_finished_at_dedups_on_second_tick(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second tick with same no-FinishedAt container does NOT re-insert or re-emit.

    Proves the stable 'unknown:<container_id>' key dedups across ticks, which is
    the core purpose of the fix: previously a wall-clock now() key caused an alert
    storm (one new row and one counter increment per tick).
    """
    monkeypatch.setattr(_reconciler_mod, "LogWindowFetcher", _FakeLogWindowFetcher)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    await _seed_docker_container(
        repo,
        target_id="c1",
        name="no-fa",
        status="exited",
        exit_code=1,
        finished_at=None,
    )

    vm = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http:
        await ContainerCrashReconciler().run(_ctx(repo, http, vm=vm))
        await ContainerCrashReconciler().run(_ctx(repo, http, vm=vm))

    crash_repo = CrashEnrichmentsRepository(repo)
    rows = await crash_repo.list_for_container("no-fa")
    assert len(rows) == 1  # only one row despite two ticks

    counter_entries = [e for e in vm.recorded if e.name == "homelab_container_crash_total"]
    assert len(counter_entries) == 1  # counter emitted exactly once


async def test_per_container_isolation(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One container raising during fetch doesn't stop other containers from being enriched."""

    class _SelectiveFetcher:
        def __init__(self, vl_client: object) -> None:
            pass

        async def fetch(
            self,
            logs_ql: str,
            anchor_ts: datetime,
            window_before_s: int = 60,
            window_after_s: int = 60,
            limit: int = 200,
        ) -> LogWindowResult:
            if "first-crash" in logs_ql:
                raise RuntimeError("simulated VL failure for first container")
            return LogWindowResult(
                lines=[_log_line("second ok")],
                truncated=False,
                degraded=False,
                window_start=anchor_ts - timedelta(seconds=window_before_s),
                window_end=anchor_ts + timedelta(seconds=window_after_s),
                queried_at=anchor_ts,
            )

    monkeypatch.setattr(_reconciler_mod, "LogWindowFetcher", _SelectiveFetcher)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    await _seed_docker_container(
        repo,
        target_id="c1",
        name="first-crash",
        status="exited",
        exit_code=1,
        finished_at="2026-06-07T00:00:00Z",
    )
    await _seed_docker_container(
        repo,
        target_id="c2",
        name="second-crash",
        status="exited",
        exit_code=2,
        finished_at="2026-06-07T00:00:01Z",
    )

    async with httpx.AsyncClient() as http:
        result = await ContainerCrashReconciler().run(_ctx(repo, http))

    # Tick is still ok (per-container errors are swallowed)
    assert result.ok is True

    crash_repo = CrashEnrichmentsRepository(repo)
    first_rows = await crash_repo.list_for_container("first-crash")
    second_rows = await crash_repo.list_for_container("second-crash")

    assert len(first_rows) == 0  # failed — no row inserted
    assert len(second_rows) == 1  # succeeded despite first failing


async def test_prune_runs(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old crash rows are pruned; recent ones survive."""
    monkeypatch.setattr(_reconciler_mod, "LogWindowFetcher", _FakeLogWindowFetcher)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_CRASH_ENRICHMENT_RETENTION_DAYS", "7")

    now = datetime.now(UTC)
    old_fa = (now - timedelta(days=40)).isoformat()

    # Insert an old crash row directly (bypassing the reconciler)
    crash_repo = CrashEnrichmentsRepository(repo)
    old_crash_id = str(uuid.uuid4())
    await crash_repo.insert(
        crash_id=old_crash_id,
        logical_key="old-container",
        container_name="old-container",
        container_id=None,
        exit_code=1,
        finished_at=old_fa,
        image_name=None,
        compose_project=None,
        compose_service=None,
        lines=[_log_line("old crash")],
        truncated=False,
        degraded=False,
        window_start=old_fa,
        window_end=old_fa,
    )

    # Seed a recently crashed container so the ENRICH phase runs
    await _seed_docker_container(
        repo,
        target_id="c1",
        name="new-crashy",
        status="exited",
        exit_code=1,
        finished_at="2026-06-07T00:00:00Z",
    )

    async with httpx.AsyncClient() as http:
        result = await ContainerCrashReconciler().run(_ctx(repo, http))

    assert result.ok is True

    # Old row must be pruned
    assert await crash_repo.get(old_crash_id) is None

    # Recent row must survive
    recent_rows = await crash_repo.list_for_container("new-crashy")
    assert len(recent_rows) == 1


async def test_no_crashed_containers_noop(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No crashed containers → result.ok True, no crash rows, no metrics emitted."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    await _seed_docker_container(
        repo,
        target_id="c1",
        name="healthy",
        status="running",
        exit_code=0,
    )

    vm = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http:
        result = await ContainerCrashReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True

    crash_repo = CrashEnrichmentsRepository(repo)
    rows = await crash_repo.list_for_container("healthy")
    assert len(rows) == 0

    counter_entries = [e for e in vm.recorded if e.name == "homelab_container_crash_total"]
    assert len(counter_entries) == 0


# ---------------------------------------------------------------------------
# _parse_anchor unit tests
# ---------------------------------------------------------------------------


def test_parse_anchor_zero_sentinel() -> None:
    """_parse_anchor with zero sentinel '0001-01-01T00:00:00Z' returns (now, False)."""
    now = datetime.now(UTC)
    anchor, real = _parse_anchor("0001-01-01T00:00:00Z", now)
    assert anchor is now
    assert real is False


def test_parse_anchor_none_returns_now() -> None:
    """_parse_anchor with None returns (now, False)."""
    now = datetime.now(UTC)
    anchor, real = _parse_anchor(None, now)
    assert anchor is now
    assert real is False


def test_parse_anchor_valid_iso_returns_parsed() -> None:
    """_parse_anchor with a valid ISO timestamp returns (parsed_datetime, True)."""
    now = datetime.now(UTC)
    anchor, real = _parse_anchor("2026-06-07T00:00:00Z", now)
    assert real is True
    assert anchor.year == 2026  # noqa: PLR2004
    assert anchor.month == 6  # noqa: PLR2004
    assert anchor.day == 7  # noqa: PLR2004
    assert anchor.tzinfo is not None


def test_parse_anchor_garbage_returns_now() -> None:
    """_parse_anchor with unparseable string returns (now, False)."""
    now = datetime.now(UTC)
    anchor, real = _parse_anchor("garbage", now)
    assert anchor is now
    assert real is False


def test_parse_anchor_empty_string_returns_now() -> None:
    """_parse_anchor with empty string returns (now, False)."""
    now = datetime.now(UTC)
    anchor, real = _parse_anchor("", now)
    assert anchor is now
    assert real is False


async def test_enrich_phase_exception_recorded_in_errors(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception in _enrich phase is caught and recorded in result.errors (lines 92-93)."""

    async def _raising_enrich(self: object, *args: object, **kwargs: object) -> int:  # pyright: ignore[reportPrivateUsage]
        raise RuntimeError("enrich boom")

    monkeypatch.setattr(ContainerCrashReconciler, "_enrich", _raising_enrich)  # pyright: ignore[reportPrivateUsage]

    async with httpx.AsyncClient() as http:
        result = await ContainerCrashReconciler().run(_ctx(repo, http))

    assert result.ok is False
    assert any("enrich" in e and "boom" in e for e in result.errors)


async def test_prune_phase_exception_recorded_in_errors(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception in _prune phase is caught and recorded in result.errors (lines 97-98)."""

    async def _raising_prune(self: object, *args: object, **kwargs: object) -> None:  # pyright: ignore[reportPrivateUsage]
        raise RuntimeError("prune boom")

    monkeypatch.setattr(ContainerCrashReconciler, "_prune", _raising_prune)  # pyright: ignore[reportPrivateUsage]

    async with httpx.AsyncClient() as http:
        result = await ContainerCrashReconciler().run(_ctx(repo, http))

    assert result.ok is False
    assert any("prune" in e and "boom" in e for e in result.errors)
