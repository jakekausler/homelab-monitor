"""Unit tests for CronRunRepository (no HTTP layer)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.cron.run_repository import (
    CronRunRepository,
    _derive_started_at,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


@pytest.mark.asyncio
async def test_insert_run_creates_running_row(repo: SqliteRepository) -> None:
    """insert_run creates a row with state='running' and correct defaults."""
    run_repo = CronRunRepository(repo)
    run_id = "test-run-1"
    fingerprint = "fp1"
    now = utc_now_iso()

    await run_repo.insert_run(
        run_id=run_id,
        cron_fingerprint=fingerprint,
        source="wrapper",
        started_at=now,
        vl_window_start=now,
    )

    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.run_id == run_id
    assert row.cron_fingerprint == fingerprint
    assert row.source == "wrapper"
    assert row.state == "running"
    assert row.started_at == now
    assert row.ended_at is None
    assert row.duration_seconds is None
    assert row.exit_code is None
    assert row.vl_window_start == now
    assert row.vl_window_end is None
    assert row.overlapping is False
    assert row.anomaly_flags == ""


@pytest.mark.asyncio
async def test_get_run_returns_none_for_unknown(repo: SqliteRepository) -> None:
    """get_run returns None for a non-existent run_id."""
    run_repo = CronRunRepository(repo)
    row = await run_repo.get_run("nope")
    assert row is None


@pytest.mark.asyncio
async def test_insert_run_is_idempotent_on_duplicate_run_id(
    repo: SqliteRepository,
) -> None:
    """Inserting the same run_id twice keeps the first row (INSERT OR IGNORE)."""
    run_repo = CronRunRepository(repo)
    run_id = "dup-run"
    fingerprint = "fp2"
    now1 = utc_now_iso()

    await run_repo.insert_run(
        run_id=run_id,
        cron_fingerprint=fingerprint,
        source="wrapper",
        started_at=now1,
        vl_window_start=now1,
    )

    # Second insert with different started_at
    now2_str = "2026-05-19T00:00:30+00:00"
    await run_repo.insert_run(
        run_id=run_id,
        cron_fingerprint=fingerprint,
        source="wrapper",
        started_at=now2_str,
        vl_window_start=now2_str,
    )

    # Should still have the first insert's started_at
    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.state == "running"
    assert row.started_at == now1  # unchanged from first insert


@pytest.mark.asyncio
async def test_close_run_updates_existing_start_row(
    repo: SqliteRepository,
) -> None:
    """close_run updates an existing /start row."""
    run_repo = CronRunRepository(repo)
    run_id = "close-test-1"
    fingerprint = "fp3"
    now_start = utc_now_iso()

    # First, insert a running row
    await run_repo.insert_run(
        run_id=run_id,
        cron_fingerprint=fingerprint,
        source="wrapper",
        started_at=now_start,
        vl_window_start=now_start,
    )

    # Then close it
    now_end = utc_now_iso()
    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=fingerprint,
        source="wrapper",
        state="ok",
        ended_at=now_end,
        duration_seconds=3.5,
        exit_code=0,
        vl_window_end=now_end,
    )

    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.state == "ok"
    assert row.ended_at == now_end
    assert row.duration_seconds == 3.5  # noqa: PLR2004
    assert row.exit_code == 0
    assert row.vl_window_end == now_end
    # started_at and vl_window_start unchanged
    assert row.started_at == now_start
    assert row.vl_window_start == now_start


@pytest.mark.asyncio
async def test_close_run_lost_start_inserts_closed_row(
    repo: SqliteRepository,
) -> None:
    """close_run without a prior /start inserts a closed row with derived started_at."""
    run_repo = CronRunRepository(repo)
    run_id = "lost-start-1"
    fingerprint = "fp4"
    ended_at = "2026-05-19T00:00:10+00:00"
    duration_seconds = 5.0

    # No prior insert_run — directly close
    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=fingerprint,
        source="wrapper",
        state="fail",
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        exit_code=42,
        vl_window_end=ended_at,
    )

    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.state == "fail"
    assert row.exit_code == 42  # noqa: PLR2004
    assert row.ended_at == ended_at
    assert row.vl_window_end == ended_at

    # started_at and vl_window_start should be derived: ended_at - 5s
    expected_start = "2026-05-19T00:00:05+00:00"
    assert row.started_at == expected_start
    assert row.vl_window_start == expected_start


@pytest.mark.asyncio
async def test_close_run_lost_start_with_null_duration_falls_back_to_ended_at(
    repo: SqliteRepository,
) -> None:
    """close_run without duration falls back started_at = ended_at."""
    run_repo = CronRunRepository(repo)
    run_id = "lost-start-nodur"
    fingerprint = "fp5"
    ended_at = "2026-05-19T00:00:15+00:00"

    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=fingerprint,
        source="wrapper",
        state="ok",
        ended_at=ended_at,
        duration_seconds=None,
        exit_code=0,
        vl_window_end=ended_at,
    )

    row = await run_repo.get_run(run_id)
    assert row is not None
    # When duration is None, derived started_at = ended_at
    assert row.started_at == ended_at
    assert row.vl_window_start == ended_at


@pytest.mark.asyncio
async def test_close_run_ok_path(repo: SqliteRepository) -> None:
    """close_run with state='ok' sets the correct fields."""
    run_repo = CronRunRepository(repo)
    run_id = "ok-run"
    fingerprint = "fp6"
    now_start = utc_now_iso()

    await run_repo.insert_run(
        run_id=run_id,
        cron_fingerprint=fingerprint,
        source="wrapper",
        started_at=now_start,
        vl_window_start=now_start,
    )

    now_end = utc_now_iso()
    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint=fingerprint,
        source="wrapper",
        state="ok",
        ended_at=now_end,
        duration_seconds=1.0,
        exit_code=0,
        vl_window_end=now_end,
    )

    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.state == "ok"
    assert row.exit_code == 0


@pytest.mark.asyncio
async def test_list_runs_returns_newest_first(repo: SqliteRepository) -> None:
    """list_runs returns runs in started_at DESC order."""
    run_repo = CronRunRepository(repo)
    fingerprint = "fp7"

    # Insert 3 closed runs with ascending started_at
    runs = [
        ("run-a", "2026-05-19T00:00:00+00:00"),
        ("run-b", "2026-05-19T00:00:05+00:00"),
        ("run-c", "2026-05-19T00:00:10+00:00"),
    ]
    for run_id, started_at in runs:
        await run_repo.close_run(
            run_id=run_id,
            cron_fingerprint=fingerprint,
            source="wrapper",
            state="ok",
            ended_at=started_at,
            duration_seconds=None,
            exit_code=0,
            vl_window_end=started_at,
        )

    page = await run_repo.list_runs(cron_fingerprint=fingerprint, limit=10)
    assert len(page.items) == 3  # noqa: PLR2004
    assert page.next_cursor is None
    # Newest first (DESC)
    assert page.items[0].run_id == "run-c"
    assert page.items[1].run_id == "run-b"
    assert page.items[2].run_id == "run-a"


@pytest.mark.asyncio
async def test_list_runs_paginates_with_cursor(repo: SqliteRepository) -> None:
    """list_runs pagination with cursor."""
    run_repo = CronRunRepository(repo)
    fingerprint = "fp8"

    # Insert 5 runs with distinct started_at
    for i in range(5):
        started_at = f"2026-05-19T00:00:{i:02d}+00:00"
        await run_repo.close_run(
            run_id=f"run-{i}",
            cron_fingerprint=fingerprint,
            source="wrapper",
            state="ok",
            ended_at=started_at,
            duration_seconds=None,
            exit_code=0,
            vl_window_end=started_at,
        )

    # First page: limit=2
    page1 = await run_repo.list_runs(cron_fingerprint=fingerprint, limit=2)
    assert len(page1.items) == 2  # noqa: PLR2004
    assert page1.next_cursor is not None
    assert page1.items[0].run_id == "run-4"  # newest
    assert page1.items[1].run_id == "run-3"

    # Second page
    page2 = await run_repo.list_runs(
        cron_fingerprint=fingerprint, limit=2, cursor=page1.next_cursor
    )
    assert len(page2.items) == 2  # noqa: PLR2004
    assert page2.next_cursor is not None
    assert page2.items[0].run_id == "run-2"
    assert page2.items[1].run_id == "run-1"

    # Third page
    page3 = await run_repo.list_runs(
        cron_fingerprint=fingerprint, limit=2, cursor=page2.next_cursor
    )
    assert len(page3.items) == 1
    assert page3.next_cursor is None
    assert page3.items[0].run_id == "run-0"


@pytest.mark.asyncio
async def test_list_runs_state_filter(repo: SqliteRepository) -> None:
    """list_runs with state_filter returns only matching rows."""
    run_repo = CronRunRepository(repo)
    fingerprint = "fp9"

    # Insert mix of ok and fail
    for i in range(3):
        state = "ok" if i % 2 == 0 else "fail"
        started_at = f"2026-05-19T00:00:{i:02d}+00:00"
        await run_repo.close_run(
            run_id=f"run-{state}-{i}",
            cron_fingerprint=fingerprint,
            source="wrapper",
            state=state,
            ended_at=started_at,
            duration_seconds=None,
            exit_code=0 if state == "ok" else 1,
            vl_window_end=started_at,
        )

    # Filter by fail
    fail_page = await run_repo.list_runs(
        cron_fingerprint=fingerprint, limit=10, state_filter="fail"
    )
    assert len(fail_page.items) == 1
    assert fail_page.items[0].state == "fail"

    # Filter by ok
    ok_page = await run_repo.list_runs(cron_fingerprint=fingerprint, limit=10, state_filter="ok")
    assert len(ok_page.items) == 2  # noqa: PLR2004
    assert all(item.state == "ok" for item in ok_page.items)


@pytest.mark.asyncio
async def test_list_runs_empty_for_unknown_fingerprint(
    repo: SqliteRepository,
) -> None:
    """list_runs for unknown fingerprint returns empty page."""
    run_repo = CronRunRepository(repo)
    page = await run_repo.list_runs(cron_fingerprint="unknown-fp", limit=10)
    assert len(page.items) == 0
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_list_runs_isolates_by_fingerprint(repo: SqliteRepository) -> None:
    """list_runs returns only rows for the requested fingerprint."""
    run_repo = CronRunRepository(repo)
    fp_alpha = "fp-alpha"
    fp_beta = "fp-beta"

    # Insert 2 runs under fp-alpha
    for i in range(2):
        started_at = f"2026-05-19T00:01:{i:02d}+00:00"
        await run_repo.close_run(
            run_id=f"alpha-run-{i}",
            cron_fingerprint=fp_alpha,
            source="wrapper",
            state="ok",
            ended_at=started_at,
            duration_seconds=None,
            exit_code=0,
            vl_window_end=started_at,
        )

    # Insert 2 runs under fp-beta
    for i in range(2):
        started_at = f"2026-05-19T00:02:{i:02d}+00:00"
        await run_repo.close_run(
            run_id=f"beta-run-{i}",
            cron_fingerprint=fp_beta,
            source="wrapper",
            state="ok",
            ended_at=started_at,
            duration_seconds=None,
            exit_code=0,
            vl_window_end=started_at,
        )

    # fp-alpha query returns only fp-alpha rows
    alpha_page = await run_repo.list_runs(cron_fingerprint=fp_alpha, limit=10)
    assert len(alpha_page.items) == 2  # noqa: PLR2004
    assert all(item.cron_fingerprint == fp_alpha for item in alpha_page.items)

    # fp-beta query returns only fp-beta rows
    beta_page = await run_repo.list_runs(cron_fingerprint=fp_beta, limit=10)
    assert len(beta_page.items) == 2  # noqa: PLR2004
    assert all(item.cron_fingerprint == fp_beta for item in beta_page.items)


# Test helper function _derive_started_at
def test_derive_started_at_with_duration() -> None:
    """_derive_started_at calculates started = ended - duration."""
    ended_at = "2026-05-19T00:00:10+00:00"
    duration = 5.0
    result = _derive_started_at(ended_at, duration)
    expected = "2026-05-19T00:00:05+00:00"
    assert result == expected


def test_derive_started_at_without_duration() -> None:
    """_derive_started_at falls back to ended_at when duration is None."""
    ended_at = "2026-05-19T00:00:10+00:00"
    result = _derive_started_at(ended_at, None)
    assert result == ended_at
