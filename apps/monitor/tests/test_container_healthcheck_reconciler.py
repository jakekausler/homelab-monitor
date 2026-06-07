"""Tests for ContainerHealthcheckReconciler (STAGE-004-033).

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
import structlog.testing
from sqlalchemy import text

import homelab_monitor.kernel.metrics.container_healthcheck_reconciler as _reconciler_mod
from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.logs.healthcheck_enrichments_repo import (
    HealthcheckEnrichmentsRepository,
)
from homelab_monitor.kernel.logs.log_window_fetcher import LogWindowResult
from homelab_monitor.kernel.logs.models import LogLine
from homelab_monitor.kernel.metrics.container_healthcheck_reconciler import (
    ContainerHealthcheckReconciler,
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
    """Minimal CollectorContext for ContainerHealthcheckReconciler."""
    return CollectorContext(
        config=CollectorConfig(name="container_healthcheck_reconciler"),
        db=repo,
        vm=vm or MemoryRetainingMetricsWriter(),
        vl=InMemoryLogsWriter(),
        http=http,
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(
            collector="container_healthcheck_reconciler",
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
    healthcheck: str | None = None,
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
            healthcheck=healthcheck,
            network_mode="bridge",
            labels={},
            now=now,
            cpu_pct=None,
            mem_mib=None,
            compose_project=compose_project,
            compose_service=compose_service,
            compose_file_path=None,
            finished_at=None,
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
            lines=[_log_line("hc log a"), _log_line("hc log b")],
            truncated=False,
            degraded=False,
            window_start=anchor_ts - timedelta(seconds=window_before_s),
            window_end=anchor_ts + timedelta(seconds=window_after_s),
            queried_at=anchor_ts,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_unhealthy_container_enriched_and_counter_emitted(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unhealthy container (first-sight stamps) gets enriched; healthy container is skipped."""
    monkeypatch.setattr(_reconciler_mod, "LogWindowFetcher", _FakeLogWindowFetcher)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    # First-sight unhealthy → stamps healthcheck_changed_at with previous=None
    await _seed_docker_container(
        repo,
        target_id="c1",
        name="unhealthy-ctr",
        status="running",
        healthcheck="unhealthy",
    )
    await _seed_docker_container(
        repo,
        target_id="c2",
        name="healthy-ctr",
        status="running",
        healthcheck="healthy",
    )

    vm = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http:
        result = await ContainerHealthcheckReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True

    hc_repo = HealthcheckEnrichmentsRepository(repo)
    rows = await hc_repo.list_for_container("unhealthy-ctr")
    assert len(rows) == 1
    assert rows[0].line_count == 2  # noqa: PLR2004

    # Healthy container must NOT be enriched
    healthy_rows = await hc_repo.list_for_container("healthy-ctr")
    assert len(healthy_rows) == 0

    # Counter emitted for the unhealthy container
    counter_entries = [
        e for e in vm.recorded if e.name == "homelab_container_healthcheck_unhealthy_total"
    ]
    assert len(counter_entries) == 1
    assert counter_entries[0].value == 1.0
    assert counter_entries[0].labels["container_name"] == "unhealthy-ctr"

    # Gauge emitted with line_count
    gauge_entries = [
        e for e in vm.recorded if e.name == "homelab_container_healthcheck_with_log_context"
    ]
    assert len(gauge_entries) == 1
    assert gauge_entries[0].value == 2.0  # noqa: PLR2004


async def test_unhealthy_without_stamp_skipped(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unhealthy container with NULL healthcheck_changed_at is NOT enriched."""
    monkeypatch.setattr(_reconciler_mod, "LogWindowFetcher", _FakeLogWindowFetcher)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    # Seed normally (first-sight unhealthy stamps it)
    await _seed_docker_container(
        repo,
        target_id="c1",
        name="unstamped-ctr",
        status="running",
        healthcheck="unhealthy",
    )

    # Now forcefully NULL out the stamp to simulate the "no stamp" case
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "UPDATE targets_docker SET healthcheck_changed_at = NULL "
                "WHERE target_id IN ("
                "  SELECT t.id FROM targets t WHERE t.name = 'unstamped-ctr'"
                ")"
            ),
        )

    vm = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http:
        result = await ContainerHealthcheckReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True

    hc_repo = HealthcheckEnrichmentsRepository(repo)
    rows = await hc_repo.list_for_container("unstamped-ctr")
    assert len(rows) == 0

    counter_entries = [
        e for e in vm.recorded if e.name == "homelab_container_healthcheck_unhealthy_total"
    ]
    assert len(counter_entries) == 0

    # SILENT skip: the container must be filtered out BEFORE _enrich_one runs.
    # Under a dropped-filter regression _enrich_one runs and hits its assert, which
    # is caught by the per-container try/except and logged as a warning.  Asserting
    # no such warning was emitted distinguishes a true filter-skip from the
    # belt-and-suspenders assert backstop.
    #
    # NOTE: structlog.testing.capture_logs() is used ABOVE around the reconciler
    # call.  We assert that no event named
    # "container_healthcheck_reconciler.enrich_container_skipped" was logged.
    #
    # Re-run inside capture_logs to verify:
    vm2 = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http2:
        with structlog.testing.capture_logs() as cap:
            await ContainerHealthcheckReconciler().run(_ctx(repo, http2, vm=vm2))

    skipped_warns = [
        e
        for e in cap
        if e.get("event") == "container_healthcheck_reconciler.enrich_container_skipped"
    ]
    assert skipped_warns == [], "Expected silent filter-skip (no warning logged) but got: " + repr(
        skipped_warns
    )


async def test_second_tick_dedups(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second tick with same healthcheck_changed_at does not re-insert or re-emit counter."""
    monkeypatch.setattr(_reconciler_mod, "LogWindowFetcher", _FakeLogWindowFetcher)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    await _seed_docker_container(
        repo,
        target_id="c1",
        name="unhealthy-ctr",
        status="running",
        healthcheck="unhealthy",
    )

    vm = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http:
        await ContainerHealthcheckReconciler().run(_ctx(repo, http, vm=vm))
        await ContainerHealthcheckReconciler().run(_ctx(repo, http, vm=vm))

    hc_repo = HealthcheckEnrichmentsRepository(repo)
    rows = await hc_repo.list_for_container("unhealthy-ctr")
    assert len(rows) == 1

    counter_entries = [
        e for e in vm.recorded if e.name == "homelab_container_healthcheck_unhealthy_total"
    ]
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
        name="unhealthy-ctr",
        status="running",
        healthcheck="unhealthy",
    )

    vm = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http:
        result = await ContainerHealthcheckReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True

    hc_repo = HealthcheckEnrichmentsRepository(repo)
    rows = await hc_repo.list_for_container("unhealthy-ctr")
    assert len(rows) == 1
    assert rows[0].degraded is True
    assert rows[0].line_count == 0

    counter_entries = [
        e for e in vm.recorded if e.name == "homelab_container_healthcheck_unhealthy_total"
    ]
    assert len(counter_entries) == 1

    gauge_entries = [
        e for e in vm.recorded if e.name == "homelab_container_healthcheck_with_log_context"
    ]
    assert gauge_entries[0].value == 0.0


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
            if "first-unhealthy" in logs_ql:
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
        name="first-unhealthy",
        status="running",
        healthcheck="unhealthy",
    )
    await _seed_docker_container(
        repo,
        target_id="c2",
        name="second-unhealthy",
        status="running",
        healthcheck="unhealthy",
    )

    async with httpx.AsyncClient() as http:
        result = await ContainerHealthcheckReconciler().run(_ctx(repo, http))

    # Tick is still ok (per-container errors are swallowed)
    assert result.ok is True

    hc_repo = HealthcheckEnrichmentsRepository(repo)
    first_rows = await hc_repo.list_for_container("first-unhealthy")
    second_rows = await hc_repo.list_for_container("second-unhealthy")

    assert len(first_rows) == 0  # failed — no row inserted
    assert len(second_rows) == 1  # succeeded despite first failing


async def test_prune_runs(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old healthcheck rows are pruned; recent ones survive."""
    monkeypatch.setattr(_reconciler_mod, "LogWindowFetcher", _FakeLogWindowFetcher)
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")
    monkeypatch.setenv("HOMELAB_MONITOR_HEALTHCHECK_ENRICHMENT_RETENTION_DAYS", "7")

    now = datetime.now(UTC)
    old_changed_at = (now - timedelta(days=40)).isoformat()

    # Insert an old incident row directly (bypassing the reconciler)
    hc_repo = HealthcheckEnrichmentsRepository(repo)
    old_incident_id = str(uuid.uuid4())
    await hc_repo.insert(
        incident_id=old_incident_id,
        logical_key="old-container",
        container_name="old-container",
        container_id=None,
        previous_healthcheck=None,
        new_state="unhealthy",
        healthcheck_changed_at=old_changed_at,
        image_name=None,
        compose_project=None,
        compose_service=None,
        lines=[_log_line("old hc")],
        truncated=False,
        degraded=False,
        window_start=old_changed_at,
        window_end=old_changed_at,
    )

    # Seed a recently unhealthy container so the ENRICH phase runs
    await _seed_docker_container(
        repo,
        target_id="c1",
        name="new-unhealthy",
        status="running",
        healthcheck="unhealthy",
    )

    async with httpx.AsyncClient() as http:
        result = await ContainerHealthcheckReconciler().run(_ctx(repo, http))

    assert result.ok is True

    # Old row must be pruned
    assert await hc_repo.get(old_incident_id) is None

    # Recent row must survive
    recent_rows = await hc_repo.list_for_container("new-unhealthy")
    assert len(recent_rows) == 1


async def test_no_unhealthy_containers_noop(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No unhealthy containers → result.ok True, no incident rows, no metrics emitted."""
    monkeypatch.setenv("HOMELAB_MONITOR_VL_URL", "http://vl-test:9428")

    await _seed_docker_container(
        repo,
        target_id="c1",
        name="healthy-ctr",
        status="running",
        healthcheck="healthy",
    )

    vm = MemoryRetainingMetricsWriter()
    async with httpx.AsyncClient() as http:
        result = await ContainerHealthcheckReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True

    hc_repo = HealthcheckEnrichmentsRepository(repo)
    rows = await hc_repo.list_for_container("healthy-ctr")
    assert len(rows) == 0

    counter_entries = [
        e for e in vm.recorded if e.name == "homelab_container_healthcheck_unhealthy_total"
    ]
    assert len(counter_entries) == 0


# ---------------------------------------------------------------------------
# _parse_anchor unit tests
# ---------------------------------------------------------------------------


def test_parse_anchor_valid_iso_returns_parsed() -> None:
    """_parse_anchor with a valid ISO timestamp returns a tz-aware datetime."""
    now = datetime.now(UTC)
    result = _parse_anchor("2026-06-07T00:00:00Z", now)
    assert result.year == 2026  # noqa: PLR2004
    assert result.month == 6  # noqa: PLR2004
    assert result.day == 7  # noqa: PLR2004
    assert result.tzinfo is not None


def test_parse_anchor_naive_datetime_gets_utc() -> None:
    """_parse_anchor with a naive ISO string (no tz) attaches UTC."""
    now = datetime.now(UTC)
    result = _parse_anchor("2026-06-07T00:00:00", now)
    assert result.tzinfo is not None


def test_parse_anchor_garbage_returns_now() -> None:
    """_parse_anchor with unparseable string returns now."""
    now = datetime.now(UTC)
    result = _parse_anchor("garbage", now)
    assert result is now


async def test_enrich_phase_exception_recorded_in_errors(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception in _enrich phase is caught and recorded in result.errors."""

    async def _raising_enrich(self: object, *args: object, **kwargs: object) -> int:  # pyright: ignore[reportPrivateUsage]
        raise RuntimeError("enrich boom")

    monkeypatch.setattr(ContainerHealthcheckReconciler, "_enrich", _raising_enrich)  # pyright: ignore[reportPrivateUsage]

    async with httpx.AsyncClient() as http:
        result = await ContainerHealthcheckReconciler().run(_ctx(repo, http))

    assert result.ok is False
    assert any("enrich" in e and "boom" in e for e in result.errors)


async def test_prune_phase_exception_recorded_in_errors(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception in _prune phase is caught and recorded in result.errors."""

    async def _raising_prune(self: object, *args: object, **kwargs: object) -> None:  # pyright: ignore[reportPrivateUsage]
        raise RuntimeError("prune boom")

    monkeypatch.setattr(ContainerHealthcheckReconciler, "_prune", _raising_prune)  # pyright: ignore[reportPrivateUsage]

    async with httpx.AsyncClient() as http:
        result = await ContainerHealthcheckReconciler().run(_ctx(repo, http))

    assert result.ok is False
    assert any("prune" in e and "boom" in e for e in result.errors)
