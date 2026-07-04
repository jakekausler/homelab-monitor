"""Tests for RunbookRunsRepository.stats_per_runbook (STAGE-009-011)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from homelab_monitor.kernel.api.routers.autofix_runs import (
    _derive_outcome,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.autofix.runs_repository import (
    RunbookRunsRepository,
    _derive_status_from_row,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.autofix.types import RunMode
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from tests.kernel.autofix.conftest import insert_run, seed_extra_runbook

# Test data constants
THIRTY_DAY_WINDOW_RUNS = 3


@pytest.mark.asyncio
async def test_no_runbooks_returns_empty_list(repo: SqliteRepository) -> None:
    runs_repo = RunbookRunsRepository(repo)
    now_iso = utc_now_iso()
    rows = await runs_repo.stats_per_runbook(window_start_iso=now_iso)
    assert rows == []


@pytest.mark.asyncio
async def test_runbook_with_zero_runs_returns_row_with_null_stats_and_count_zero(
    repo: SqliteRepository, seed_runbook: str
) -> None:
    runs_repo = RunbookRunsRepository(repo)
    now_iso = utc_now_iso()
    rows = await runs_repo.stats_per_runbook(window_start_iso=now_iso)

    assert len(rows) == 1
    assert rows[0].runbook_id == seed_runbook
    assert rows[0].run_count_30d == 0
    assert rows[0].last_run_at is None
    assert rows[0].last_run_status is None
    assert rows[0].success_rate_30d is None


@pytest.mark.asyncio
async def test_run_count_30d_matches_number_of_runs_in_window(
    repo: SqliteRepository, seed_runbook: str
) -> None:
    runs_repo = RunbookRunsRepository(repo)
    now = datetime.fromisoformat(utc_now_iso())
    window_start = (now - timedelta(days=30)).isoformat()
    within_window = (now - timedelta(days=15)).isoformat()
    outside_window = (now - timedelta(days=35)).isoformat()

    # Insert 3 runs within the window
    for _i in range(3):
        await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.REAL,
            initiated_by="operator",
            started_at=within_window,
        )

    # Insert 1 run outside the window
    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at=outside_window,
    )

    rows = await runs_repo.stats_per_runbook(window_start_iso=window_start)
    assert len(rows) == 1
    assert rows[0].run_count_30d == THIRTY_DAY_WINDOW_RUNS


@pytest.mark.asyncio
async def test_success_rate_30d_null_when_no_real_ended_runs(
    repo: SqliteRepository, seed_runbook: str
) -> None:
    runs_repo = RunbookRunsRepository(repo)
    now = datetime.fromisoformat(utc_now_iso())
    window_start = (now - timedelta(days=30)).isoformat()
    within_window = (now - timedelta(days=15)).isoformat()

    # Insert dry_run (not counted in success rate)
    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="operator",
        started_at=within_window,
    )

    rows = await runs_repo.stats_per_runbook(window_start_iso=window_start)
    assert len(rows) == 1
    assert rows[0].success_rate_30d is None


@pytest.mark.asyncio
async def test_success_rate_30d_zero_when_all_real_runs_failed(
    repo: SqliteRepository, seed_runbook: str
) -> None:
    runs_repo = RunbookRunsRepository(repo)
    now = datetime.fromisoformat(utc_now_iso())
    window_start = (now - timedelta(days=30)).isoformat()
    within_window = (now - timedelta(days=15)).isoformat()

    for _i in range(3):
        await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.REAL,
            initiated_by="operator",
            started_at=within_window,
            ended_at=within_window,
            exit_code=1,
        )

    rows = await runs_repo.stats_per_runbook(window_start_iso=window_start)
    assert len(rows) == 1
    assert rows[0].success_rate_30d == 0.0


@pytest.mark.asyncio
async def test_success_rate_30d_one_when_all_real_runs_succeeded(
    repo: SqliteRepository, seed_runbook: str
) -> None:
    runs_repo = RunbookRunsRepository(repo)
    now = datetime.fromisoformat(utc_now_iso())
    window_start = (now - timedelta(days=30)).isoformat()
    within_window = (now - timedelta(days=15)).isoformat()

    for _i in range(3):
        await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.REAL,
            initiated_by="operator",
            started_at=within_window,
            ended_at=within_window,
            exit_code=0,
        )

    rows = await runs_repo.stats_per_runbook(window_start_iso=window_start)
    assert len(rows) == 1
    assert rows[0].success_rate_30d == 1.0


@pytest.mark.asyncio
async def test_success_rate_30d_partial(repo: SqliteRepository, seed_runbook: str) -> None:
    runs_repo = RunbookRunsRepository(repo)
    now = datetime.fromisoformat(utc_now_iso())
    window_start = (now - timedelta(days=30)).isoformat()
    within_window = (now - timedelta(days=15)).isoformat()

    # 2 success
    for _i in range(2):
        await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.REAL,
            initiated_by="operator",
            started_at=within_window,
            ended_at=within_window,
            exit_code=0,
        )

    # 1 failure
    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at=within_window,
        ended_at=within_window,
        exit_code=1,
    )

    rows = await runs_repo.stats_per_runbook(window_start_iso=window_start)
    assert len(rows) == 1
    assert rows[0].success_rate_30d == pytest.approx(2 / 3)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_last_run_status_success_failure_killed_in_flight_dry_run(
    repo: SqliteRepository,
) -> None:
    runs_repo = RunbookRunsRepository(repo)
    now = datetime.fromisoformat(utc_now_iso())
    window_start = (now - timedelta(days=30)).isoformat()
    within_window = (now - timedelta(days=15)).isoformat()

    # Test success
    success_rb = await seed_extra_runbook(repo)
    await insert_run(
        repo,
        runbook_id=success_rb,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at=within_window,
        ended_at=within_window,
        exit_code=0,
    )

    # Test failure
    failure_rb = await seed_extra_runbook(repo)
    await insert_run(
        repo,
        runbook_id=failure_rb,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at=within_window,
        ended_at=within_window,
        exit_code=1,
    )

    # Test killed
    killed_rb = await seed_extra_runbook(repo)
    await insert_run(
        repo,
        runbook_id=killed_rb,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at=within_window,
        killed_at=within_window,
    )

    # Test in_flight
    inflight_rb = await seed_extra_runbook(repo)
    await insert_run(
        repo,
        runbook_id=inflight_rb,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at=within_window,
    )

    # Test dry_run
    dryrun_rb = await seed_extra_runbook(repo)
    await insert_run(
        repo,
        runbook_id=dryrun_rb,
        mode=RunMode.DRY_RUN,
        initiated_by="operator",
        started_at=within_window,
        ended_at=within_window,
        exit_code=0,
    )

    rows = await runs_repo.stats_per_runbook(window_start_iso=window_start)
    by_id = {r.runbook_id: r for r in rows}

    assert by_id[success_rb].last_run_status == "success"
    assert by_id[failure_rb].last_run_status == "failure"
    assert by_id[killed_rb].last_run_status == "killed"
    assert by_id[inflight_rb].last_run_status == "in_flight"
    assert by_id[dryrun_rb].last_run_status == "dry_run"


@pytest.mark.asyncio
async def test_window_boundary_28d_ago_included_32d_ago_excluded(
    repo: SqliteRepository, seed_runbook: str
) -> None:
    runs_repo = RunbookRunsRepository(repo)
    now = datetime.fromisoformat(utc_now_iso())
    window_start = (now - timedelta(days=30)).isoformat()

    # Insert a run 28 days ago (should be included)
    included_ts = (now - timedelta(days=28)).isoformat()
    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at=included_ts,
    )

    # Insert a run 32 days ago (should be excluded)
    excluded_ts = (now - timedelta(days=32)).isoformat()
    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at=excluded_ts,
    )

    rows = await runs_repo.stats_per_runbook(window_start_iso=window_start)
    assert len(rows) == 1
    assert rows[0].run_count_30d == 1


@pytest.mark.asyncio
async def test_status_matches_router_derivation() -> None:
    """Test that _derive_status_from_row matches the router's logic."""
    # The router uses _derive_outcome; this tests that it matches
    test_cases = [
        # (mode, ended_at, exit_code, killed_at) -> expected_status
        ("real", None, None, None, "in_flight"),
        ("real", "2024-01-01T00:00:00", 0, None, "success"),
        ("real", "2024-01-01T00:00:00", 1, None, "failure"),
        ("real", "2024-01-01T00:00:00", 0, "2024-01-01T00:30:00", "killed"),
        ("real", None, None, "2024-01-01T00:30:00", "killed"),
        ("dry_run", "2024-01-01T00:00:00", 0, None, "dry_run"),
        ("dry_run", "2024-01-01T00:00:00", 1, None, "dry_run"),
        ("dry_run", None, None, None, "in_flight"),
    ]

    for mode, ended_at, exit_code, killed_at, expected in test_cases:
        status = _derive_status_from_row(
            mode=mode,
            ended_at=ended_at,
            exit_code=exit_code,
            killed_at=killed_at,
        )
        outcome = _derive_outcome(
            mode=mode,
            ended_at=ended_at,
            exit_code=exit_code,
            killed_at=killed_at,
        )
        assert status == expected, f"Failed for {(mode, ended_at, exit_code, killed_at)}"
        assert outcome == expected, (
            f"Router _derive_outcome failed for {(mode, ended_at, exit_code, killed_at)}"
        )
        assert status == outcome, f"Lockstep mismatch for {(mode, ended_at, exit_code, killed_at)}"


@pytest.mark.asyncio
async def test_dry_run_killed_at_precedence(repo: SqliteRepository, seed_runbook: str) -> None:
    """A dry_run with killed_at set should derive as 'killed', not 'dry_run'.

    Locks the precedence: killed > dry_run in the outcome derivation.
    Regression: if _derive_outcome/_derive_status_from_row reorder checks,
    this catches the drift.
    """
    runs_repo = RunbookRunsRepository(repo)
    now = datetime.fromisoformat(utc_now_iso())
    window_start = (now - timedelta(days=30)).isoformat()
    within_window = (now - timedelta(days=15)).isoformat()

    await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="operator",
        started_at=within_window,
        ended_at=within_window,
        killed_at=within_window,
        exit_code=None,
    )
    stats = await runs_repo.stats_per_runbook(window_start_iso=window_start)
    row = next((r for r in stats if r.runbook_id == seed_runbook), None)
    assert row is not None
    assert row.last_run_status == "killed"
