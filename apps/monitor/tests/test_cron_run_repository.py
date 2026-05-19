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


# ---------------------------------------------------------------------------
# New STAGE-002-013 methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_open_bmode_runs_returns_only_logscrape_running(
    repo: SqliteRepository,
) -> None:
    """list_open_bmode_runs returns only source='logscrape' AND state='running' rows."""
    run_repo = CronRunRepository(repo)

    # seed: logscrape+running (should appear)
    await run_repo.insert_run(
        run_id="bmode-running-1",
        cron_fingerprint="fp-bmode",
        source="logscrape",
        started_at="2026-05-19T00:01:00+00:00",
        vl_window_start="2026-05-19T00:01:00+00:00",
    )
    # seed: logscrape+ok (closed — should NOT appear)
    await run_repo.close_run(
        run_id="bmode-closed-1",
        cron_fingerprint="fp-bmode",
        source="logscrape",
        state="ok",
        ended_at="2026-05-19T00:00:30+00:00",
        duration_seconds=30.0,
        exit_code=0,
        vl_window_end="2026-05-19T00:00:30+00:00",
    )
    # seed: wrapper+running (should NOT appear — wrong source)
    await run_repo.insert_run(
        run_id="amode-running-1",
        cron_fingerprint="fp-wrapper",
        source="wrapper",
        started_at="2026-05-19T00:02:00+00:00",
        vl_window_start="2026-05-19T00:02:00+00:00",
    )

    results = await run_repo.list_open_bmode_runs()
    assert len(results) == 1
    assert results[0].run_id == "bmode-running-1"
    assert results[0].source == "logscrape"
    assert results[0].state == "running"


@pytest.mark.asyncio
async def test_list_open_bmode_runs_ordered_by_fingerprint_then_started_at(
    repo: SqliteRepository,
) -> None:
    """list_open_bmode_runs is ordered (cron_fingerprint ASC, started_at ASC)."""
    run_repo = CronRunRepository(repo)

    await run_repo.insert_run(
        run_id="fp-b-run-1",
        cron_fingerprint="fp-b",
        source="logscrape",
        started_at="2026-05-19T00:02:00+00:00",
        vl_window_start="2026-05-19T00:02:00+00:00",
    )
    await run_repo.insert_run(
        run_id="fp-a-run-2",
        cron_fingerprint="fp-a",
        source="logscrape",
        started_at="2026-05-19T00:02:00+00:00",
        vl_window_start="2026-05-19T00:02:00+00:00",
    )
    await run_repo.insert_run(
        run_id="fp-a-run-1",
        cron_fingerprint="fp-a",
        source="logscrape",
        started_at="2026-05-19T00:01:00+00:00",
        vl_window_start="2026-05-19T00:01:00+00:00",
    )

    results = await run_repo.list_open_bmode_runs()
    fps = [r.cron_fingerprint for r in results]
    # fp-a comes before fp-b alphabetically
    assert fps == ["fp-a", "fp-a", "fp-b"]
    # Within fp-a, earlier started_at is first
    fp_a_runs = [r for r in results if r.cron_fingerprint == "fp-a"]
    assert fp_a_runs[0].run_id == "fp-a-run-1"
    assert fp_a_runs[1].run_id == "fp-a-run-2"


@pytest.mark.asyncio
async def test_list_open_bmode_runs_empty(repo: SqliteRepository) -> None:
    """list_open_bmode_runs returns empty list when no B-mode running rows exist."""
    run_repo = CronRunRepository(repo)
    results = await run_repo.list_open_bmode_runs()
    assert results == []


@pytest.mark.asyncio
async def test_find_open_run_by_fingerprint_returns_most_recent(
    repo: SqliteRepository,
) -> None:
    """find_open_run_by_fingerprint returns newest started_at <= at_ts run."""
    run_repo = CronRunRepository(repo)
    fp = "fp-find"

    await run_repo.insert_run(
        run_id="run-older",
        cron_fingerprint=fp,
        source="logscrape",
        started_at="2026-05-19T00:01:00+00:00",
        vl_window_start="2026-05-19T00:01:00+00:00",
    )
    await run_repo.insert_run(
        run_id="run-newer",
        cron_fingerprint=fp,
        source="logscrape",
        started_at="2026-05-19T00:02:00+00:00",
        vl_window_start="2026-05-19T00:02:00+00:00",
    )

    # at_ts after both → returns newer
    result = await run_repo.find_open_run_by_fingerprint(fp, "2026-05-19T00:03:00+00:00")
    assert result is not None
    assert result.run_id == "run-newer"

    # at_ts between them → returns older (newer not yet started)
    result2 = await run_repo.find_open_run_by_fingerprint(fp, "2026-05-19T00:01:30+00:00")
    assert result2 is not None
    assert result2.run_id == "run-older"


@pytest.mark.asyncio
async def test_find_open_run_by_fingerprint_returns_none_before_both(
    repo: SqliteRepository,
) -> None:
    """find_open_run_by_fingerprint returns None when at_ts is before all runs."""
    run_repo = CronRunRepository(repo)
    fp = "fp-early"

    await run_repo.insert_run(
        run_id="run-1",
        cron_fingerprint=fp,
        source="logscrape",
        started_at="2026-05-19T00:05:00+00:00",
        vl_window_start="2026-05-19T00:05:00+00:00",
    )

    result = await run_repo.find_open_run_by_fingerprint(fp, "2026-05-19T00:01:00+00:00")
    assert result is None


@pytest.mark.asyncio
async def test_find_open_run_by_fingerprint_unknown_fingerprint(
    repo: SqliteRepository,
) -> None:
    """find_open_run_by_fingerprint returns None for unknown fingerprint."""
    run_repo = CronRunRepository(repo)
    result = await run_repo.find_open_run_by_fingerprint("no-such-fp", "2026-05-19T00:00:00+00:00")
    assert result is None


@pytest.mark.asyncio
async def test_find_open_run_by_fingerprint_ignores_closed_runs(
    repo: SqliteRepository,
) -> None:
    """find_open_run_by_fingerprint only matches state='running' rows."""
    run_repo = CronRunRepository(repo)
    fp = "fp-closed"

    # Insert then close
    await run_repo.insert_run(
        run_id="run-closed",
        cron_fingerprint=fp,
        source="logscrape",
        started_at="2026-05-19T00:01:00+00:00",
        vl_window_start="2026-05-19T00:01:00+00:00",
    )
    await run_repo.close_run(
        run_id="run-closed",
        cron_fingerprint=fp,
        source="logscrape",
        state="ok",
        ended_at="2026-05-19T00:02:00+00:00",
        duration_seconds=60.0,
        exit_code=0,
        vl_window_end="2026-05-19T00:02:00+00:00",
    )

    result = await run_repo.find_open_run_by_fingerprint(fp, "2026-05-19T00:03:00+00:00")
    assert result is None


@pytest.mark.asyncio
async def test_list_runs_needing_enrich_excludes_within_grace(
    repo: SqliteRepository,
) -> None:
    """list_runs_needing_enrich excludes runs ended after the grace cutoff."""
    run_repo = CronRunRepository(repo)

    # Closed run ended BEFORE cutoff → should be returned
    await run_repo.close_run(
        run_id="enrich-old",
        cron_fingerprint="fp-enrich",
        source="wrapper",
        state="ok",
        ended_at="2026-05-19T00:00:00+00:00",
        duration_seconds=10.0,
        exit_code=0,
        vl_window_end="2026-05-19T00:00:00+00:00",
    )
    # Closed run ended AFTER cutoff → excluded (within grace window)
    await run_repo.close_run(
        run_id="enrich-new",
        cron_fingerprint="fp-enrich",
        source="wrapper",
        state="ok",
        ended_at="2026-05-19T01:00:00+00:00",
        duration_seconds=10.0,
        exit_code=0,
        vl_window_end="2026-05-19T01:00:00+00:00",
    )

    # grace_cutoff = 2026-05-19T00:30:00 (old run is before it, new run is after it)
    results = await run_repo.list_runs_needing_enrich("2026-05-19T00:30:00+00:00")
    assert len(results) == 1
    assert results[0].run_id == "enrich-old"


@pytest.mark.asyncio
async def test_list_runs_needing_enrich_excludes_running_rows(
    repo: SqliteRepository,
) -> None:
    """list_runs_needing_enrich excludes still-running rows (state='running')."""
    run_repo = CronRunRepository(repo)

    await run_repo.insert_run(
        run_id="still-running",
        cron_fingerprint="fp-r",
        source="logscrape",
        started_at="2026-05-19T00:00:00+00:00",
        vl_window_start="2026-05-19T00:00:00+00:00",
    )

    results = await run_repo.list_runs_needing_enrich("2026-05-19T01:00:00+00:00")
    assert all(r.run_id != "still-running" for r in results)


@pytest.mark.asyncio
async def test_list_runs_needing_enrich_excludes_already_enriched(
    repo: SqliteRepository,
) -> None:
    """list_runs_needing_enrich excludes runs that already have enriched_at set."""
    run_repo = CronRunRepository(repo)

    await run_repo.close_run(
        run_id="already-enriched",
        cron_fingerprint="fp-e",
        source="wrapper",
        state="ok",
        ended_at="2026-05-19T00:00:00+00:00",
        duration_seconds=5.0,
        exit_code=0,
        vl_window_end="2026-05-19T00:00:00+00:00",
    )
    # Write enrichment so enriched_at is set
    await run_repo.set_enrichment(
        run_id="already-enriched",
        line_count=3,
        byte_count=100,
        content_digest="abc123",
        enriched_at=utc_now_iso(),
    )

    results = await run_repo.list_runs_needing_enrich("2026-05-19T01:00:00+00:00")
    assert all(r.run_id != "already-enriched" for r in results)


@pytest.mark.asyncio
async def test_finalize_bmode_run_sets_all_fields(repo: SqliteRepository) -> None:
    """finalize_bmode_run sets state/ended_at/duration_seconds/vl_window_end."""
    run_repo = CronRunRepository(repo)
    run_id = "finalize-me"

    await run_repo.insert_run(
        run_id=run_id,
        cron_fingerprint="fp-fin",
        source="logscrape",
        started_at="2026-05-19T00:00:00+00:00",
        vl_window_start="2026-05-19T00:00:00+00:00",
    )

    ended_at = "2026-05-19T00:05:00+00:00"
    await run_repo.finalize_bmode_run(
        run_id=run_id,
        state="unknown",
        ended_at=ended_at,
        duration_seconds=300.0,
    )

    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.state == "unknown"
    assert row.ended_at == ended_at
    assert row.duration_seconds == 300.0  # noqa: PLR2004
    assert row.vl_window_end == ended_at


@pytest.mark.asyncio
async def test_finalize_bmode_run_is_idempotent(repo: SqliteRepository) -> None:
    """finalize_bmode_run called twice on the same run is a no-op the second time."""
    run_repo = CronRunRepository(repo)
    run_id = "finalize-idem"

    await run_repo.insert_run(
        run_id=run_id,
        cron_fingerprint="fp-idem",
        source="logscrape",
        started_at="2026-05-19T00:00:00+00:00",
        vl_window_start="2026-05-19T00:00:00+00:00",
    )
    ended_at = "2026-05-19T00:05:00+00:00"
    await run_repo.finalize_bmode_run(
        run_id=run_id,
        state="unknown",
        ended_at=ended_at,
        duration_seconds=300.0,
    )
    # Second call: the state='running' guard in the UPDATE makes this a no-op
    await run_repo.finalize_bmode_run(
        run_id=run_id,
        state="ok",  # different state — should be ignored
        ended_at="2026-05-19T01:00:00+00:00",
        duration_seconds=3600.0,
    )

    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.state == "unknown"  # first call wins
    assert row.ended_at == ended_at


@pytest.mark.asyncio
async def test_set_overlapping_sets_flag(repo: SqliteRepository) -> None:
    """set_overlapping flips overlapping to True."""
    run_repo = CronRunRepository(repo)
    run_id = "overlap-test"

    await run_repo.insert_run(
        run_id=run_id,
        cron_fingerprint="fp-ov",
        source="logscrape",
        started_at="2026-05-19T00:00:00+00:00",
        vl_window_start="2026-05-19T00:00:00+00:00",
    )

    # Before: overlapping is False
    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.overlapping is False

    await run_repo.set_overlapping(run_id)

    row2 = await run_repo.get_run(run_id)
    assert row2 is not None
    assert row2.overlapping is True


@pytest.mark.asyncio
async def test_set_enrichment_sets_fields(repo: SqliteRepository) -> None:
    """set_enrichment writes line_count/byte_count/content_digest/enriched_at."""
    run_repo = CronRunRepository(repo)
    run_id = "enrich-write"

    await run_repo.close_run(
        run_id=run_id,
        cron_fingerprint="fp-ew",
        source="wrapper",
        state="ok",
        ended_at="2026-05-19T00:01:00+00:00",
        duration_seconds=10.0,
        exit_code=0,
        vl_window_end="2026-05-19T00:01:00+00:00",
    )

    enriched_at = utc_now_iso()
    await run_repo.set_enrichment(
        run_id=run_id,
        line_count=42,
        byte_count=8192,
        content_digest="deadbeef" * 8,
        enriched_at=enriched_at,
    )

    row = await run_repo.get_run(run_id)
    assert row is not None
    assert row.line_count == 42  # noqa: PLR2004
    assert row.byte_count == 8192  # noqa: PLR2004
    assert row.content_digest == "deadbeef" * 8
    assert row.enriched_at == enriched_at
    # anomaly_flags must remain untouched at the empty default
    assert row.anomaly_flags == ""


@pytest.mark.asyncio
async def test_prune_runs_by_age_deletes_old_rows(repo: SqliteRepository) -> None:
    """prune_runs age bound: rows with started_at before cutoff are deleted."""
    run_repo = CronRunRepository(repo)
    fp = "fp-prune-age"

    # Old row: before cutoff
    await run_repo.close_run(
        run_id="old-row",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at="2026-05-01T00:00:00+00:00",
        duration_seconds=None,
        exit_code=0,
        vl_window_end="2026-05-01T00:00:00+00:00",
    )
    # New row: after cutoff (started_at derived = ended_at when duration=None)
    await run_repo.close_run(
        run_id="new-row",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at="2026-05-19T00:00:00+00:00",
        duration_seconds=None,
        exit_code=0,
        vl_window_end="2026-05-19T00:00:00+00:00",
    )

    deleted = await run_repo.prune_runs(
        retention_cutoff="2026-05-10T00:00:00+00:00",
        max_rows_per_cron=100_000,
    )

    assert deleted >= 1
    assert await run_repo.get_run("old-row") is None
    assert await run_repo.get_run("new-row") is not None


@pytest.mark.asyncio
async def test_prune_runs_by_count_keeps_newest(repo: SqliteRepository) -> None:
    """prune_runs count bound: only the newest max_rows_per_cron rows are kept."""
    run_repo = CronRunRepository(repo)
    fp = "fp-prune-count"

    # Seed 5 rows with distinct started_at (derived via duration=None → ended_at)
    run_ids: list[str] = []
    for i in range(5):
        rid = f"count-run-{i}"
        run_ids.append(rid)
        started = f"2026-05-19T00:00:{i:02d}+00:00"
        await run_repo.close_run(
            run_id=rid,
            cron_fingerprint=fp,
            source="wrapper",
            state="ok",
            ended_at=started,
            duration_seconds=None,
            exit_code=0,
            vl_window_end=started,
        )

    # prune to keep only newest 3
    deleted = await run_repo.prune_runs(
        retention_cutoff="2020-01-01T00:00:00+00:00",  # nothing older than this
        max_rows_per_cron=3,
    )

    assert deleted == 2  # noqa: PLR2004
    # Newest 3 (indices 2,3,4) must still exist
    assert await run_repo.get_run("count-run-4") is not None
    assert await run_repo.get_run("count-run-3") is not None
    assert await run_repo.get_run("count-run-2") is not None
    # Oldest 2 (indices 0,1) must be deleted
    assert await run_repo.get_run("count-run-0") is None
    assert await run_repo.get_run("count-run-1") is None


@pytest.mark.asyncio
async def test_prune_runs_both_bounds_combined(repo: SqliteRepository) -> None:
    """prune_runs applies age then count; both passes contribute to deleted count."""
    run_repo = CronRunRepository(repo)
    fp = "fp-prune-both"

    # One very old row (age-pruned)
    await run_repo.close_run(
        run_id="ancient",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at="2020-01-01T00:00:00+00:00",
        duration_seconds=None,
        exit_code=0,
        vl_window_end="2020-01-01T00:00:00+00:00",
    )
    # 4 newer rows (not age-pruned, but 2 should be count-pruned with max_rows=2)
    for i in range(4):
        started = f"2026-05-19T00:00:{i:02d}+00:00"
        await run_repo.close_run(
            run_id=f"newer-{i}",
            cron_fingerprint=fp,
            source="wrapper",
            state="ok",
            ended_at=started,
            duration_seconds=None,
            exit_code=0,
            vl_window_end=started,
        )

    deleted = await run_repo.prune_runs(
        retention_cutoff="2026-01-01T00:00:00+00:00",
        max_rows_per_cron=2,
    )

    # 1 age-pruned + 2 count-pruned = 3
    assert deleted == 3  # noqa: PLR2004
    assert await run_repo.get_run("ancient") is None
    assert await run_repo.get_run("newer-0") is None
    assert await run_repo.get_run("newer-1") is None
    assert await run_repo.get_run("newer-2") is not None
    assert await run_repo.get_run("newer-3") is not None


@pytest.mark.asyncio
async def test_prune_runs_returns_zero_when_nothing_to_prune(
    repo: SqliteRepository,
) -> None:
    """prune_runs returns 0 when no rows match the prune criteria."""
    run_repo = CronRunRepository(repo)

    await run_repo.close_run(
        run_id="safe-run",
        cron_fingerprint="fp-safe",
        source="wrapper",
        state="ok",
        ended_at="2026-05-19T00:00:00+00:00",
        duration_seconds=None,
        exit_code=0,
        vl_window_end="2026-05-19T00:00:00+00:00",
    )

    deleted = await run_repo.prune_runs(
        retention_cutoff="2020-01-01T00:00:00+00:00",  # nothing older than this
        max_rows_per_cron=100_000,
    )

    assert deleted == 0


# ---------------------------------------------------------------------------
# STAGE-002-014: Compound cursor helpers
# ---------------------------------------------------------------------------


def test_encode_decode_cursor_round_trip() -> None:
    """_decode_runs_cursor(_encode_runs_cursor(s, r)) returns original (s, r)."""
    from homelab_monitor.kernel.cron.run_repository import (  # noqa: PLC0415
        _decode_runs_cursor,  # pyright: ignore[reportPrivateUsage]
        _encode_runs_cursor,  # pyright: ignore[reportPrivateUsage]
    )

    started_at = "2026-05-19T12:34:56.123456+00:00"
    run_id = "abc-def-123"
    encoded = _encode_runs_cursor(started_at, run_id)
    decoded_s, decoded_r = _decode_runs_cursor(encoded)
    assert decoded_s == started_at
    assert decoded_r == run_id


def test_decode_cursor_rejects_invalid_base64() -> None:
    """Malformed base64 raises InvalidCursorError."""
    from homelab_monitor.kernel.cron.run_repository import (  # noqa: PLC0415
        InvalidCursorError,
        _decode_runs_cursor,  # pyright: ignore[reportPrivateUsage]
    )

    with pytest.raises(InvalidCursorError):
        _decode_runs_cursor("!!!not-base64!!!")


def test_decode_cursor_rejects_malformed_json() -> None:
    """Valid base64 of invalid JSON raises InvalidCursorError."""
    import base64  # noqa: PLC0415

    from homelab_monitor.kernel.cron.run_repository import (  # noqa: PLC0415
        InvalidCursorError,
        _decode_runs_cursor,  # pyright: ignore[reportPrivateUsage]
    )

    bad = base64.urlsafe_b64encode(b"not-json").rstrip(b"=").decode()
    with pytest.raises(InvalidCursorError):
        _decode_runs_cursor(bad)


def test_decode_cursor_rejects_missing_keys() -> None:
    """JSON object missing 's'/'r' keys raises InvalidCursorError."""
    import base64  # noqa: PLC0415
    import json  # noqa: PLC0415

    from homelab_monitor.kernel.cron.run_repository import (  # noqa: PLC0415
        InvalidCursorError,
        _decode_runs_cursor,  # pyright: ignore[reportPrivateUsage]
    )

    raw = json.dumps({"x": "foo"}).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    with pytest.raises(InvalidCursorError):
        _decode_runs_cursor(encoded)


def test_decode_cursor_rejects_wrong_value_types() -> None:
    """'s' or 'r' values that are not strings raise InvalidCursorError."""
    import base64  # noqa: PLC0415
    import json  # noqa: PLC0415

    from homelab_monitor.kernel.cron.run_repository import (  # noqa: PLC0415
        InvalidCursorError,
        _decode_runs_cursor,  # pyright: ignore[reportPrivateUsage]
    )

    raw = json.dumps({"s": 123, "r": "run-id"}).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    with pytest.raises(InvalidCursorError):
        _decode_runs_cursor(encoded)


def test_decode_cursor_rejects_non_object_json() -> None:
    """JSON array (not object) raises InvalidCursorError."""
    import base64  # noqa: PLC0415
    import json  # noqa: PLC0415

    from homelab_monitor.kernel.cron.run_repository import (  # noqa: PLC0415
        InvalidCursorError,
        _decode_runs_cursor,  # pyright: ignore[reportPrivateUsage]
    )

    raw = json.dumps(["s", "r"]).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    with pytest.raises(InvalidCursorError):
        _decode_runs_cursor(encoded)


# ---------------------------------------------------------------------------
# STAGE-002-014: Compound cursor regression — same started_at, different run_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_compound_cursor_does_not_drop_same_started_at(
    repo: SqliteRepository,
) -> None:
    """Two runs with identical started_at but different run_ids must not straddle
    a page boundary — the compound (started_at, run_id) keyset must return both.

    This is the STAGE-002-011 review carry-forward regression test.
    """
    run_repo = CronRunRepository(repo)
    fp = "fp-cursor-regression"
    shared_ts = "2026-05-19T10:00:00+00:00"

    # Insert two runs with IDENTICAL started_at (simulates lost-/start UPSERT path)
    # run-Z has a higher run_id lexicographically → comes first in DESC order
    await run_repo.insert_run(
        run_id="run-Z",
        cron_fingerprint=fp,
        source="wrapper",
        started_at=shared_ts,
        vl_window_start=shared_ts,
    )
    await run_repo.insert_run(
        run_id="run-A",
        cron_fingerprint=fp,
        source="wrapper",
        started_at=shared_ts,
        vl_window_start=shared_ts,
    )

    # Page 1: limit=1 → should return run-Z (DESC by run_id)
    page1 = await run_repo.list_runs(cron_fingerprint=fp, limit=1)
    assert len(page1.items) == 1
    assert page1.items[0].run_id == "run-Z"
    assert page1.next_cursor is not None

    # Page 2: using the cursor → must return run-A (not dropped)
    page2 = await run_repo.list_runs(cron_fingerprint=fp, limit=1, cursor=page1.next_cursor)
    assert len(page2.items) == 1
    assert page2.items[0].run_id == "run-A"


# ---------------------------------------------------------------------------
# STAGE-002-014: list_runs state_filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_state_filter_running(repo: SqliteRepository) -> None:
    """state_filter='running' returns only running rows."""
    run_repo = CronRunRepository(repo)
    fp = "fp-sf-running"
    now = "2026-05-19T00:00:00+00:00"

    await run_repo.insert_run(
        run_id="r-running",
        cron_fingerprint=fp,
        source="wrapper",
        started_at=now,
        vl_window_start=now,
    )
    await run_repo.close_run(
        run_id="r-ok",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at=now,
        duration_seconds=1.0,
        exit_code=0,
        vl_window_end=now,
    )

    page = await run_repo.list_runs(cron_fingerprint=fp, limit=10, state_filter="running")
    assert all(r.state == "running" for r in page.items)
    run_ids = {r.run_id for r in page.items}
    assert "r-running" in run_ids
    assert "r-ok" not in run_ids


@pytest.mark.asyncio
async def test_list_runs_state_filter_ok(repo: SqliteRepository) -> None:
    """state_filter='ok' returns only ok rows."""
    run_repo = CronRunRepository(repo)
    fp = "fp-sf-ok"
    now = "2026-05-19T00:00:00+00:00"

    await run_repo.insert_run(
        run_id="r-running2",
        cron_fingerprint=fp,
        source="wrapper",
        started_at=now,
        vl_window_start=now,
    )
    await run_repo.close_run(
        run_id="r-ok2",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at=now,
        duration_seconds=1.0,
        exit_code=0,
        vl_window_end=now,
    )
    await run_repo.close_run(
        run_id="r-fail2",
        cron_fingerprint=fp,
        source="wrapper",
        state="fail",
        ended_at=now,
        duration_seconds=1.0,
        exit_code=1,
        vl_window_end=now,
    )

    page = await run_repo.list_runs(cron_fingerprint=fp, limit=10, state_filter="ok")
    assert all(r.state == "ok" for r in page.items)
    assert any(r.run_id == "r-ok2" for r in page.items)


@pytest.mark.asyncio
async def test_list_runs_state_filter_fail(repo: SqliteRepository) -> None:
    """state_filter='fail' returns only fail rows."""
    run_repo = CronRunRepository(repo)
    fp = "fp-sf-fail"
    now = "2026-05-19T00:00:00+00:00"

    await run_repo.close_run(
        run_id="r-ok3",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at=now,
        duration_seconds=1.0,
        exit_code=0,
        vl_window_end=now,
    )
    await run_repo.close_run(
        run_id="r-fail3",
        cron_fingerprint=fp,
        source="wrapper",
        state="fail",
        ended_at=now,
        duration_seconds=1.0,
        exit_code=1,
        vl_window_end=now,
    )

    page = await run_repo.list_runs(cron_fingerprint=fp, limit=10, state_filter="fail")
    assert all(r.state == "fail" for r in page.items)
    assert any(r.run_id == "r-fail3" for r in page.items)


@pytest.mark.asyncio
async def test_list_runs_state_filter_unknown(repo: SqliteRepository) -> None:
    """state_filter='unknown' returns only unknown rows."""
    run_repo = CronRunRepository(repo)
    fp = "fp-sf-unknown"
    now = "2026-05-19T00:00:00+00:00"

    await run_repo.close_run(
        run_id="r-unk",
        cron_fingerprint=fp,
        source="wrapper",
        state="unknown",
        ended_at=now,
        duration_seconds=1.0,
        exit_code=None,
        vl_window_end=now,
    )
    await run_repo.close_run(
        run_id="r-ok4",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at=now,
        duration_seconds=1.0,
        exit_code=0,
        vl_window_end=now,
    )

    page = await run_repo.list_runs(cron_fingerprint=fp, limit=10, state_filter="unknown")
    assert all(r.state == "unknown" for r in page.items)
    assert any(r.run_id == "r-unk" for r in page.items)


# ---------------------------------------------------------------------------
# STAGE-002-014: list_recent_completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_recent_completed_excludes_running_state(
    repo: SqliteRepository,
) -> None:
    """list_recent_completed never returns state='running' rows."""
    run_repo = CronRunRepository(repo)
    fp = "fp-lrc"
    now = "2026-05-19T00:00:00+00:00"

    await run_repo.insert_run(
        run_id="lrc-running",
        cron_fingerprint=fp,
        source="wrapper",
        started_at=now,
        vl_window_start=now,
    )
    await run_repo.close_run(
        run_id="lrc-ok",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at=now,
        duration_seconds=1.0,
        exit_code=0,
        vl_window_end=now,
    )

    results = await run_repo.list_recent_completed(
        cron_fingerprint=fp,
        limit=10,
        exclude_run_id="other",
    )
    assert all(r.state != "running" for r in results)
    assert any(r.run_id == "lrc-ok" for r in results)
    assert not any(r.run_id == "lrc-running" for r in results)


@pytest.mark.asyncio
async def test_list_recent_completed_respects_exclude_run_id(
    repo: SqliteRepository,
) -> None:
    """list_recent_completed excludes the specified run_id."""
    run_repo = CronRunRepository(repo)
    fp = "fp-lrc-excl"
    now = "2026-05-19T00:00:00+00:00"

    await run_repo.close_run(
        run_id="lrc-target",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at=now,
        duration_seconds=1.0,
        exit_code=0,
        vl_window_end=now,
    )
    await run_repo.close_run(
        run_id="lrc-other",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at=now,
        duration_seconds=1.0,
        exit_code=0,
        vl_window_end=now,
    )

    results = await run_repo.list_recent_completed(
        cron_fingerprint=fp,
        limit=10,
        exclude_run_id="lrc-target",
    )
    ids = {r.run_id for r in results}
    assert "lrc-target" not in ids
    assert "lrc-other" in ids


@pytest.mark.asyncio
async def test_list_recent_completed_respects_limit(
    repo: SqliteRepository,
) -> None:
    """list_recent_completed returns at most `limit` rows."""
    run_repo = CronRunRepository(repo)
    fp = "fp-lrc-limit"
    for i in range(5):
        ts = f"2026-05-19T00:00:0{i}+00:00"
        await run_repo.close_run(
            run_id=f"lrc-lim-{i}",
            cron_fingerprint=fp,
            source="wrapper",
            state="ok",
            ended_at=ts,
            duration_seconds=1.0,
            exit_code=0,
            vl_window_end=ts,
        )

    results = await run_repo.list_recent_completed(
        cron_fingerprint=fp,
        limit=3,
        exclude_run_id="other",
    )
    assert len(results) == 3  # noqa: PLR2004


@pytest.mark.asyncio
async def test_list_recent_completed_ordering_newest_first(
    repo: SqliteRepository,
) -> None:
    """list_recent_completed returns rows in (started_at DESC, run_id DESC) order."""
    run_repo = CronRunRepository(repo)
    fp = "fp-lrc-order"

    ts_a = "2026-05-19T00:00:01+00:00"
    ts_b = "2026-05-19T00:00:02+00:00"
    await run_repo.close_run(
        run_id="lrc-a",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at=ts_a,
        duration_seconds=1.0,
        exit_code=0,
        vl_window_end=ts_a,
    )
    await run_repo.close_run(
        run_id="lrc-b",
        cron_fingerprint=fp,
        source="wrapper",
        state="ok",
        ended_at=ts_b,
        duration_seconds=1.0,
        exit_code=0,
        vl_window_end=ts_b,
    )

    results = await run_repo.list_recent_completed(
        cron_fingerprint=fp,
        limit=10,
        exclude_run_id="other",
    )
    assert results[0].run_id == "lrc-b"  # newer first
    assert results[1].run_id == "lrc-a"


# ---------------------------------------------------------------------------
# STAGE-002-014: set_anomaly_flags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_anomaly_flags_writes_flags(repo: SqliteRepository) -> None:
    """set_anomaly_flags persists the given flags string."""
    run_repo = CronRunRepository(repo)
    fp = "fp-flags"
    now = "2026-05-19T00:00:00+00:00"

    await run_repo.insert_run(
        run_id="flags-run",
        cron_fingerprint=fp,
        source="wrapper",
        started_at=now,
        vl_window_start=now,
    )
    await run_repo.set_anomaly_flags(
        run_id="flags-run",
        anomaly_flags="duration_outlier,new_failure",
    )

    row = await run_repo.get_run("flags-run")
    assert row is not None
    assert row.anomaly_flags == "duration_outlier,new_failure"


@pytest.mark.asyncio
async def test_set_anomaly_flags_allows_empty_string(repo: SqliteRepository) -> None:
    """set_anomaly_flags with empty string is valid (no anomalies)."""
    run_repo = CronRunRepository(repo)
    fp = "fp-flags-empty"
    now = "2026-05-19T00:00:00+00:00"

    await run_repo.insert_run(
        run_id="flags-empty-run",
        cron_fingerprint=fp,
        source="wrapper",
        started_at=now,
        vl_window_start=now,
    )
    await run_repo.set_anomaly_flags(run_id="flags-empty-run", anomaly_flags="")

    row = await run_repo.get_run("flags-empty-run")
    assert row is not None
    assert row.anomaly_flags == ""


@pytest.mark.asyncio
async def test_set_anomaly_flags_is_idempotent(repo: SqliteRepository) -> None:
    """Calling set_anomaly_flags twice keeps the last value (UPSERT idempotent)."""
    run_repo = CronRunRepository(repo)
    fp = "fp-flags-idem"
    now = "2026-05-19T00:00:00+00:00"

    await run_repo.insert_run(
        run_id="flags-idem-run",
        cron_fingerprint=fp,
        source="wrapper",
        started_at=now,
        vl_window_start=now,
    )
    await run_repo.set_anomaly_flags(run_id="flags-idem-run", anomaly_flags="duration_outlier")
    await run_repo.set_anomaly_flags(run_id="flags-idem-run", anomaly_flags="new_failure")

    row = await run_repo.get_run("flags-idem-run")
    assert row is not None
    assert row.anomaly_flags == "new_failure"
