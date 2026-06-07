"""Tests for SignatureCatalogSync (STAGE-004-028).

Exercises the touched-only upsert logic:
  sig_state[(model_key, template_hash)] = (cluster_size, first_seen_ts, template_str)
  cycle_counts[(model_key, template_hash, severity)] = count

Project test conventions:
- Framework: pytest-asyncio (asyncio_mode=auto — bare async def)
- DB: tempfile-backed SQLite + alembic head via `repo` fixture (conftest)
"""

from __future__ import annotations

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.signature_sync import (
    SignatureCatalogSync,
    _pick_first_seen_severity,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.logs.signatures_repo import SignatureFilter, SignaturesRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sync(repo: SqliteRepository) -> SignatureCatalogSync:
    return SignatureCatalogSync(repo)


def _sig_repo(repo: SqliteRepository) -> SignaturesRepository:
    return SignaturesRepository(repo)


# ===========================================================================
# Insert path: first cycle creates a new row
# ===========================================================================


async def test_insert_path_creates_row_with_correct_fields(repo: SqliteRepository) -> None:
    """Single cycle on a new key creates a row with all expected fields."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    # sig_state value shape: (cluster_size, first_seen_ts, template_str)
    sig_state = {("svcA", "h1"): (5, 1000, "foo <*> bar")}
    cycle_counts = {("svcA", "h1", "info"): 3}
    last_seen_at = 2000

    await sync.sync_cycle(sig_state=sig_state, cycle_counts=cycle_counts, last_seen_at=last_seen_at)

    row = await sig_repo.get("h1", "svcA")
    assert row is not None
    assert row.template_hash == "h1"
    assert row.service_key == "svcA"
    assert row.template_str == "foo <*> bar"
    assert row.label is None
    assert row.status == "active"
    assert row.first_seen_at == 1000  # noqa: PLR2004
    assert row.first_seen_severity == "info"
    assert row.last_seen_at == 2000  # noqa: PLR2004
    assert row.total_count == 3  # noqa: PLR2004


# ===========================================================================
# Accumulate across two cycles
# ===========================================================================


async def test_accumulate_across_two_cycles(repo: SqliteRepository) -> None:
    """Second cycle with delta=4 accumulates total_count and updates last_seen_at."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    sig_state = {("svcA", "h1"): (5, 1000, "foo <*> bar")}

    # Cycle 1: 3 lines
    await sync.sync_cycle(
        sig_state=sig_state,
        cycle_counts={("svcA", "h1", "info"): 3},
        last_seen_at=2000,
    )

    # Cycle 2: 4 more lines, newer timestamp
    await sync.sync_cycle(
        sig_state=sig_state,
        cycle_counts={("svcA", "h1", "info"): 4},
        last_seen_at=5000,
    )

    row = await sig_repo.get("h1", "svcA")
    assert row is not None
    assert row.total_count == 7  # noqa: PLR2004 -- 3 + 4
    assert row.first_seen_at == 1000  # noqa: PLR2004 -- first_seen_at must NOT change on upsert
    assert row.last_seen_at == 5000  # noqa: PLR2004


# ===========================================================================
# Delta sums over multiple severities
# ===========================================================================


async def test_delta_sums_over_multiple_severities(repo: SqliteRepository) -> None:
    """Cycle counts across severities are summed into the delta."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    sig_state = {("svcA", "h1"): (5, 1000, "foo <*> bar")}
    cycle_counts = {
        ("svcA", "h1", "info"): 2,
        ("svcA", "h1", "error"): 3,
    }

    await sync.sync_cycle(sig_state=sig_state, cycle_counts=cycle_counts, last_seen_at=2000)

    row = await sig_repo.get("h1", "svcA")
    assert row is not None
    assert row.total_count == 5  # noqa: PLR2004 -- 2 + 3


# ===========================================================================
# Touched-only: untouched row is unmodified
# ===========================================================================


async def test_untouched_row_is_not_modified(repo: SqliteRepository) -> None:
    """A row not in the current cycle's sig_state is left unchanged."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    # Pre-populate two rows
    await sync.sync_cycle(
        sig_state={
            ("svcA", "h1"): (1, 100, "template A"),
            ("svcA", "h2"): (1, 200, "template B"),
        },
        cycle_counts={("svcA", "h1", "info"): 1, ("svcA", "h2", "info"): 10},
        last_seen_at=500,
    )

    # Second cycle touches only h1 — h2 should be frozen
    await sync.sync_cycle(
        sig_state={("svcA", "h1"): (1, 100, "template A")},
        cycle_counts={("svcA", "h1", "info"): 5},
        last_seen_at=999,
    )

    h2 = await sig_repo.get("h2", "svcA")
    assert h2 is not None
    assert h2.total_count == 10  # noqa: PLR2004 -- unchanged
    assert h2.last_seen_at == 500  # noqa: PLR2004 -- unchanged


# ===========================================================================
# Label / status preserved across sync
# ===========================================================================


async def test_label_and_status_preserved_across_sync(repo: SqliteRepository) -> None:
    """User-set label and status are not overwritten by a subsequent sync."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    # Initial insert
    await sync.sync_cycle(
        sig_state={("svcA", "h1"): (1, 1000, "foo <*> bar")},
        cycle_counts={("svcA", "h1", "info"): 2},
        last_seen_at=2000,
    )

    # Manually set user-owned fields
    await sig_repo.update_label("h1", "svcA", "my-custom-label")
    await sig_repo.set_status("h1", "svcA", "suppressed")

    # Another cycle touching the same key
    await sync.sync_cycle(
        sig_state={("svcA", "h1"): (1, 1000, "foo <*> bar")},
        cycle_counts={("svcA", "h1", "info"): 7},
        last_seen_at=9000,
    )

    row = await sig_repo.get("h1", "svcA")
    assert row is not None
    assert row.label == "my-custom-label"  # preserved
    assert row.status == "suppressed"  # preserved
    assert row.total_count == 9  # noqa: PLR2004 -- 2 + 7 accumulated
    assert row.last_seen_at == 9000  # noqa: PLR2004


# ===========================================================================
# Empty sig_state → no-op
# ===========================================================================


async def test_empty_sig_state_is_a_noop(repo: SqliteRepository) -> None:
    """sync_cycle with empty sig_state creates no rows and does not error."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    await sync.sync_cycle(
        sig_state={},
        cycle_counts={},
        last_seen_at=99999,
    )

    rows, total = await sig_repo.list(filter=SignatureFilter(), limit=100, offset=0)
    assert total == 0
    assert rows == []


# ===========================================================================
# Multiple keys in same cycle
# ===========================================================================


async def test_multiple_keys_in_one_cycle(repo: SqliteRepository) -> None:
    """Multiple sig_state keys are all inserted in one sync_cycle call."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    await sync.sync_cycle(
        sig_state={
            ("svcA", "hash1"): (1, 1000, "template one"),
            ("svcB", "hash2"): (2, 2000, "template two"),
        },
        cycle_counts={
            ("svcA", "hash1", "info"): 4,
            ("svcB", "hash2", "warn"): 6,
        },
        last_seen_at=5000,
    )

    row1 = await sig_repo.get("hash1", "svcA")
    row2 = await sig_repo.get("hash2", "svcB")

    assert row1 is not None
    assert row1.total_count == 4  # noqa: PLR2004
    assert row1.template_str == "template one"

    assert row2 is not None
    assert row2.total_count == 6  # noqa: PLR2004
    assert row2.template_str == "template two"
    assert row2.first_seen_at == 2000  # noqa: PLR2004


# ===========================================================================
# first_seen_severity persistence (STAGE-004-035)
# ===========================================================================


async def test_insert_persists_single_severity(repo: SqliteRepository) -> None:
    """INSERT path persists first_seen_severity from the single severity in cycle_counts."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    sig_state = {("svcA", "h1"): (1, 1000, "foo <*> bar")}
    cycle_counts = {("svcA", "h1", "warning"): 2}

    await sync.sync_cycle(sig_state=sig_state, cycle_counts=cycle_counts, last_seen_at=2000)

    row = await sig_repo.get("h1", "svcA")
    assert row is not None
    assert row.first_seen_severity == "warning"


async def test_insert_picks_highest_rank_severity(repo: SqliteRepository) -> None:
    """INSERT path picks highest-ranked severity when multiple present in cycle."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    sig_state = {("svcA", "h1"): (1, 1000, "foo <*> bar")}
    # info + error in same cycle -> error wins (higher rank)
    cycle_counts = {
        ("svcA", "h1", "info"): 3,
        ("svcA", "h1", "error"): 1,
    }

    await sync.sync_cycle(sig_state=sig_state, cycle_counts=cycle_counts, last_seen_at=2000)

    row = await sig_repo.get("h1", "svcA")
    assert row is not None
    assert row.first_seen_severity == "error"


async def test_insert_picks_highest_rank_among_three_severities(repo: SqliteRepository) -> None:
    """INSERT with critical, error, warning -> critical wins (highest rank)."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    sig_state = {("svcA", "h1"): (1, 1000, "foo <*> bar")}
    cycle_counts = {
        ("svcA", "h1", "warning"): 2,
        ("svcA", "h1", "critical"): 1,
        ("svcA", "h1", "error"): 3,
    }

    await sync.sync_cycle(sig_state=sig_state, cycle_counts=cycle_counts, last_seen_at=2000)

    row = await sig_repo.get("h1", "svcA")
    assert row is not None
    assert row.first_seen_severity == "critical"


async def test_update_preserves_first_seen_severity(repo: SqliteRepository) -> None:
    """UPDATE path does not touch first_seen_severity (preserved from first cycle)."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    # Cycle 1: insert with error
    await sync.sync_cycle(
        sig_state={("svcA", "h1"): (1, 1000, "foo <*> bar")},
        cycle_counts={("svcA", "h1", "error"): 2},
        last_seen_at=2000,
    )

    # Cycle 2: touch same key with only info (lower rank)
    await sync.sync_cycle(
        sig_state={("svcA", "h1"): (1, 1000, "foo <*> bar")},
        cycle_counts={("svcA", "h1", "info"): 4},
        last_seen_at=5000,
    )

    row = await sig_repo.get("h1", "svcA")
    assert row is not None
    # first_seen_severity must STILL be "error" from cycle 1 (not overwritten to "info")
    assert row.first_seen_severity == "error"
    assert row.total_count == 6  # noqa: PLR2004 -- 2 + 4


async def test_insert_with_unknown_severity(repo: SqliteRepository) -> None:
    """INSERT with unknown/unlisted severity still persists it."""
    sync = _sync(repo)
    sig_repo = _sig_repo(repo)

    sig_state = {("svcA", "h1"): (1, 1000, "foo <*> bar")}
    cycle_counts = {("svcA", "h1", "unknown"): 1}

    await sync.sync_cycle(sig_state=sig_state, cycle_counts=cycle_counts, last_seen_at=2000)

    row = await sig_repo.get("h1", "svcA")
    assert row is not None
    assert row.first_seen_severity == "unknown"


async def test_severity_ranking_deterministic(repo: SqliteRepository) -> None:
    """Severity ranking is deterministic: same severities always pick the same winner."""
    # Test the ranking helper directly
    result1 = _pick_first_seen_severity(["info", "critical", "error"])
    result2 = _pick_first_seen_severity(["critical", "info", "error"])
    result3 = _pick_first_seen_severity(["error", "critical", "info"])
    assert result1 == result2 == result3 == "critical"

    # Test with unknown severities (they sort after all ranked ones)
    result4 = _pick_first_seen_severity(["foo", "bar"])
    assert result4 == "bar"  # alphabetical tiebreak
