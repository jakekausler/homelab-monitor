"""Tests for RunbookRunsRepository.list_paged (STAGE-009-011)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.autofix.runs_repository import (
    RunbookRunsRepository,
    RunsFilter,
)
from homelab_monitor.kernel.autofix.types import RunMode
from homelab_monitor.kernel.db.repository import SqliteRepository
from tests.kernel.autofix.conftest import insert_run

# Test data constants
SINCE_UNTIL_RUN_COUNT_3 = 3
UNTIL_ONLY_RUN_COUNT_2 = 2
PAGINATION_LIMIT_2 = 2
PAGINATION_LIMIT_3 = 3
PAGINATION_OFFSET_2 = 2
PAGINATION_OFFSET_4 = 4
PAGINATION_TOTAL_5 = 5
PAGINATION_TOTAL_10 = 10
PAGINATION_REMAINING_1 = 1
ORDERING_COUNT_3 = 3


@pytest.mark.asyncio
async def test_empty_db_returns_empty_list_and_zero_total(repo: SqliteRepository) -> None:
    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(RunsFilter(), limit=100, offset=0)
    assert rows == []
    assert total == 0


@pytest.mark.asyncio
async def test_single_run_no_filters_returns_it_with_total_1(
    repo: SqliteRepository, seed_runbook: str
) -> None:

    run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
    )

    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(RunsFilter(), limit=100, offset=0)

    assert len(rows) == 1
    assert rows[0].id == run_id
    assert total == 1


@pytest.mark.asyncio
async def test_filter_by_runbook_id_excludes_other_runbooks(
    repo: SqliteRepository, seed_runbook: str, another_runbook: str
) -> None:

    run1_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
    )
    _run2_id = await insert_run(
        repo,
        runbook_id=another_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
    )

    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(
        RunsFilter(runbook_id=seed_runbook), limit=100, offset=0
    )

    assert len(rows) == 1
    assert rows[0].id == run1_id
    assert total == 1


@pytest.mark.asyncio
async def test_filter_by_mode_dry_run_and_real(repo: SqliteRepository, seed_runbook: str) -> None:
    runs_repo = RunbookRunsRepository(repo)
    dry_run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="operator",
    )
    real_run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
    )

    dry_rows, dry_total = await runs_repo.list_paged(
        RunsFilter(mode="dry_run"), limit=100, offset=0
    )
    assert len(dry_rows) == 1
    assert dry_rows[0].id == dry_run_id
    assert dry_total == 1

    real_rows, real_total = await runs_repo.list_paged(RunsFilter(mode="real"), limit=100, offset=0)
    assert len(real_rows) == 1
    assert real_rows[0].id == real_run_id
    assert real_total == 1


@pytest.mark.asyncio
async def test_filter_by_initiator_alert_and_operator(
    repo: SqliteRepository, seed_runbook: str
) -> None:
    runs_repo = RunbookRunsRepository(repo)
    alert_run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="alert",
    )
    operator_run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
    )

    alert_rows, alert_total = await runs_repo.list_paged(
        RunsFilter(initiator="alert"), limit=100, offset=0
    )
    assert len(alert_rows) == 1
    assert alert_rows[0].id == alert_run_id
    assert alert_total == 1

    op_rows, op_total = await runs_repo.list_paged(
        RunsFilter(initiator="operator"), limit=100, offset=0
    )
    assert len(op_rows) == 1
    assert op_rows[0].id == operator_run_id
    assert op_total == 1


@pytest.mark.asyncio
async def test_filter_by_since_inclusive_and_until_exclusive(
    repo: SqliteRepository, seed_runbook: str
) -> None:

    run1 = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at="2024-01-01T00:00:00",
    )
    run2 = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at="2024-01-02T00:00:00",
    )
    _run3 = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at="2024-01-03T00:00:00",
    )

    # since inclusive
    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(
        RunsFilter(since="2024-01-01T00:00:00"), limit=100, offset=0
    )
    assert total == SINCE_UNTIL_RUN_COUNT_3

    # until exclusive
    rows, total = await runs_repo.list_paged(
        RunsFilter(until="2024-01-03T00:00:00"), limit=100, offset=0
    )
    assert total == UNTIL_ONLY_RUN_COUNT_2
    assert all(r.id in (run1, run2) for r in rows)

    # both
    rows, total = await runs_repo.list_paged(
        RunsFilter(since="2024-01-02T00:00:00", until="2024-01-03T00:00:00"),
        limit=100,
        offset=0,
    )
    assert total == 1
    assert rows[0].id == run2


@pytest.mark.asyncio
async def test_filter_outcome_in_flight_returns_runs_with_null_ended_and_null_killed(
    repo: SqliteRepository, seed_runbook: str
) -> None:

    in_flight_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        ended_at=None,
        killed_at=None,
    )
    _ended_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        ended_at="2024-01-01T01:00:00",
        exit_code=0,
    )

    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(RunsFilter(outcome="in_flight"), limit=100, offset=0)
    assert len(rows) == 1
    assert rows[0].id == in_flight_id
    assert total == 1


@pytest.mark.asyncio
async def test_filter_outcome_success_requires_mode_real_exit_zero_and_not_killed(
    repo: SqliteRepository, seed_runbook: str
) -> None:

    success_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        ended_at="2024-01-01T01:00:00",
        exit_code=0,
        killed_at=None,
    )
    _non_zero_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        ended_at="2024-01-01T02:00:00",
        exit_code=1,
        killed_at=None,
    )

    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(RunsFilter(outcome="success"), limit=100, offset=0)
    assert len(rows) == 1
    assert rows[0].id == success_id
    assert total == 1


@pytest.mark.asyncio
async def test_filter_outcome_failure_requires_mode_real_exit_nonzero_and_not_killed(
    repo: SqliteRepository, seed_runbook: str
) -> None:

    failure_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        ended_at="2024-01-01T01:00:00",
        exit_code=1,
        killed_at=None,
    )
    _success_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        ended_at="2024-01-01T02:00:00",
        exit_code=0,
        killed_at=None,
    )

    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(RunsFilter(outcome="failure"), limit=100, offset=0)
    assert len(rows) == 1
    assert rows[0].id == failure_id
    assert total == 1


@pytest.mark.asyncio
async def test_filter_outcome_killed_requires_killed_at_set(
    repo: SqliteRepository, seed_runbook: str
) -> None:

    killed_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        killed_at="2024-01-01T01:30:00",
    )
    _not_killed_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        ended_at="2024-01-01T02:00:00",
        exit_code=0,
        killed_at=None,
    )

    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(RunsFilter(outcome="killed"), limit=100, offset=0)
    assert len(rows) == 1
    assert rows[0].id == killed_id
    assert total == 1


@pytest.mark.asyncio
async def test_dry_run_success_case_is_NOT_success_filter_hit(
    repo: SqliteRepository, seed_runbook: str
) -> None:

    _dry_run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.DRY_RUN,
        initiated_by="operator",
        ended_at="2024-01-01T01:00:00",
        exit_code=0,
        killed_at=None,
    )

    # outcome=success filter should NOT match dry_run with exit_code=0
    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(RunsFilter(outcome="success"), limit=100, offset=0)
    assert len(rows) == 0
    assert total == 0


@pytest.mark.asyncio
async def test_pagination_offset_and_limit_correct_slice(
    repo: SqliteRepository, seed_runbook: str
) -> None:

    ids: list[str] = []
    for i in range(5):
        run_id = await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.REAL,
            initiated_by="operator",
            started_at=f"2024-01-0{i + 1}T00:00:00",
        )
        ids.append(run_id)

    # limit 2, offset 0
    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(RunsFilter(), limit=PAGINATION_LIMIT_2, offset=0)
    assert len(rows) == PAGINATION_LIMIT_2
    assert total == PAGINATION_TOTAL_5

    # limit 2, offset 2
    rows, total = await runs_repo.list_paged(
        RunsFilter(), limit=PAGINATION_LIMIT_2, offset=PAGINATION_OFFSET_2
    )
    assert len(rows) == PAGINATION_LIMIT_2
    assert total == PAGINATION_TOTAL_5

    # limit 2, offset 4
    rows, total = await runs_repo.list_paged(
        RunsFilter(), limit=PAGINATION_LIMIT_2, offset=PAGINATION_OFFSET_4
    )
    assert len(rows) == PAGINATION_REMAINING_1
    assert total == PAGINATION_TOTAL_5


@pytest.mark.asyncio
async def test_pagination_total_count_reflects_all_matching_rows_not_page_size(
    repo: SqliteRepository, seed_runbook: str
) -> None:

    for _i in range(PAGINATION_TOTAL_10):
        await insert_run(
            repo,
            runbook_id=seed_runbook,
            mode=RunMode.REAL,
            initiated_by="operator",
        )

    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(RunsFilter(), limit=PAGINATION_LIMIT_3, offset=0)
    assert len(rows) == PAGINATION_LIMIT_3
    assert total == PAGINATION_TOTAL_10


@pytest.mark.asyncio
async def test_ordering_started_at_desc_then_id_desc(
    repo: SqliteRepository, seed_runbook: str
) -> None:

    run1 = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at="2024-01-01T00:00:00",
    )
    run2 = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at="2024-01-02T00:00:00",
    )
    run3 = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
        started_at="2024-01-02T00:00:00",  # same timestamp as run2
    )

    runs_repo = RunbookRunsRepository(repo)
    rows, total = await runs_repo.list_paged(RunsFilter(), limit=100, offset=0)

    assert len(rows) == ORDERING_COUNT_3
    assert rows[0].id in (run2, run3)  # both have later timestamp
    assert rows[2].id == run1  # earliest
    assert total == ORDERING_COUNT_3


@pytest.mark.asyncio
async def test_joined_runbook_path_is_populated(repo: SqliteRepository, seed_runbook: str) -> None:

    _run_id = await insert_run(
        repo,
        runbook_id=seed_runbook,
        mode=RunMode.REAL,
        initiated_by="operator",
    )

    runs_repo = RunbookRunsRepository(repo)
    rows, _total = await runs_repo.list_paged(RunsFilter(), limit=100, offset=0)

    assert len(rows) == 1
    assert rows[0].runbook_path is not None
    assert len(rows[0].runbook_path) > 0
