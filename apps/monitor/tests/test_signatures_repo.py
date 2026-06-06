"""Tests for SignaturesRepository (STAGE-004-028).

Project test conventions:
- Framework: pytest-asyncio (asyncio_mode=auto — bare async def, no decorator)
- DB: tempfile-backed SQLite + alembic head via `repo` fixture (conftest)
- Assertions: direct field checks against persisted state
"""

from __future__ import annotations

from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.signatures_repo import (
    Signature,
    SignatureFilter,
    SignaturesRepository,
)

# ---------------------------------------------------------------------------
# Seeding helper — inserts a row directly via the SqliteRepository
# ---------------------------------------------------------------------------


async def _insert_sig(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    template_hash: str,
    service_key: str,
    template_str: str = "foo <*> bar",
    label: str | None = None,
    status: str = "active",
    first_seen_at: int = 1000,
    last_seen_at: int = 2000,
    total_count: int = 5,
) -> None:
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO log_signatures "
                "  (template_hash, service_key, template_str, label, status, "
                "   first_seen_at, last_seen_at, total_count) "
                "VALUES "
                "  (:h, :s, :tstr, :label, :status, :first, :last, :cnt)"
            ),
            {
                "h": template_hash,
                "s": service_key,
                "tstr": template_str,
                "label": label,
                "status": status,
                "first": first_seen_at,
                "last": last_seen_at,
                "cnt": total_count,
            },
        )


# ===========================================================================
# list — empty filter (all rows + total)
# ===========================================================================


async def test_list_empty_filter_returns_all_rows(repo: SqliteRepository) -> None:
    """list() with no filter returns all rows and correct total."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svcA")
    await _insert_sig(repo, template_hash="h2", service_key="svcB")

    rows, total = await sig_repo.list(filter=SignatureFilter(), limit=100, offset=0)
    assert total == 2  # noqa: PLR2004
    hashes = {r.template_hash for r in rows}
    assert hashes == {"h1", "h2"}


async def test_list_empty_db_returns_empty(repo: SqliteRepository) -> None:
    """list() on empty table returns empty list and total=0."""
    sig_repo = SignaturesRepository(repo)
    rows, total = await sig_repo.list(filter=SignatureFilter(), limit=100, offset=0)
    assert rows == []
    assert total == 0


# ===========================================================================
# list — filter by service
# ===========================================================================


async def test_list_filtered_by_service_returns_right_subset(repo: SqliteRepository) -> None:
    """list(service=...) returns only rows for that service."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svcA")
    await _insert_sig(repo, template_hash="h2", service_key="svcB")
    await _insert_sig(repo, template_hash="h3", service_key="svcA")

    rows, total = await sig_repo.list(filter=SignatureFilter(service="svcA"), limit=100, offset=0)
    assert total == 2  # noqa: PLR2004
    assert all(r.service_key == "svcA" for r in rows)
    hashes = {r.template_hash for r in rows}
    assert hashes == {"h1", "h3"}


# ===========================================================================
# list — filter by status
# ===========================================================================


async def test_list_filtered_by_status_returns_right_subset(repo: SqliteRepository) -> None:
    """list(status=...) returns only rows with that status."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svcA", status="active")
    await _insert_sig(repo, template_hash="h2", service_key="svcA", status="suppressed")
    await _insert_sig(repo, template_hash="h3", service_key="svcA", status="expected")

    rows, total = await sig_repo.list(
        filter=SignatureFilter(status="suppressed"), limit=100, offset=0
    )
    assert total == 1
    assert rows[0].template_hash == "h2"
    assert rows[0].status == "suppressed"


# ===========================================================================
# list — filter by label_q (LIKE substring)
# ===========================================================================


async def test_list_filtered_by_label_q_returns_matching_rows(repo: SqliteRepository) -> None:
    """list(label_q=...) returns rows whose label contains the substring."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svcA", label="my-label")
    await _insert_sig(repo, template_hash="h2", service_key="svcA", label="other-tag")
    await _insert_sig(repo, template_hash="h3", service_key="svcA", label=None)

    rows, total = await sig_repo.list(filter=SignatureFilter(label_q="label"), limit=100, offset=0)
    assert total == 1
    assert rows[0].template_hash == "h1"
    assert rows[0].label == "my-label"


async def test_list_label_q_no_matches_returns_empty(repo: SqliteRepository) -> None:
    """list(label_q=...) with no matching label returns empty list and total=0."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svcA", label="other")

    rows, total = await sig_repo.list(
        filter=SignatureFilter(label_q="nomatch"), limit=100, offset=0
    )
    assert rows == []
    assert total == 0


# ===========================================================================
# list — combined filters
# ===========================================================================


async def test_list_combined_filters(repo: SqliteRepository) -> None:
    """list(service=, status=, label_q=) intersects all three conditions."""
    sig_repo = SignaturesRepository(repo)
    # Matches all three
    await _insert_sig(
        repo,
        template_hash="h1",
        service_key="svcA",
        status="suppressed",
        label="mine",
    )
    # Wrong service
    await _insert_sig(
        repo,
        template_hash="h2",
        service_key="svcB",
        status="suppressed",
        label="mine",
    )
    # Wrong status
    await _insert_sig(
        repo,
        template_hash="h3",
        service_key="svcA",
        status="active",
        label="mine",
    )
    # Wrong label
    await _insert_sig(
        repo,
        template_hash="h4",
        service_key="svcA",
        status="suppressed",
        label="other",
    )

    rows, total = await sig_repo.list(
        filter=SignatureFilter(service="svcA", status="suppressed", label_q="mine"),
        limit=100,
        offset=0,
    )
    assert total == 1
    assert rows[0].template_hash == "h1"


# ===========================================================================
# list — pagination
# ===========================================================================


async def test_list_pagination_slices_correctly(repo: SqliteRepository) -> None:
    """limit/offset slices rows; total reflects the FULL count not just the page."""
    sig_repo = SignaturesRepository(repo)
    for i in range(5):
        await _insert_sig(
            repo,
            template_hash=f"h{i}",
            service_key="svc",
            last_seen_at=1000 + i,
        )

    rows, total = await sig_repo.list(
        filter=SignatureFilter(),
        limit=2,
        offset=0,
    )
    assert total == 5  # noqa: PLR2004 -- full count
    assert len(rows) == 2  # noqa: PLR2004

    rows2, total2 = await sig_repo.list(
        filter=SignatureFilter(),
        limit=2,
        offset=2,
    )
    assert total2 == 5  # noqa: PLR2004 -- same full count
    assert len(rows2) == 2  # noqa: PLR2004
    # Pages should not overlap
    page1_hashes = {r.template_hash for r in rows}
    page2_hashes = {r.template_hash for r in rows2}
    assert page1_hashes.isdisjoint(page2_hashes)


async def test_list_pagination_offset_beyond_total_returns_empty(repo: SqliteRepository) -> None:
    """Offset beyond total returns empty rows but correct total."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svc")

    rows, total = await sig_repo.list(
        filter=SignatureFilter(),
        limit=10,
        offset=100,
    )
    assert total == 1
    assert rows == []


# ===========================================================================
# list — sort
# ===========================================================================


async def test_list_sort_descending_default(repo: SqliteRepository) -> None:
    """Default sort is last_seen_at DESC — newest row first."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="old", service_key="svc", last_seen_at=1000)
    await _insert_sig(repo, template_hash="new", service_key="svc", last_seen_at=9000)

    rows, _ = await sig_repo.list(filter=SignatureFilter(), limit=100, offset=0, descending=True)
    assert rows[0].template_hash == "new"
    assert rows[1].template_hash == "old"


async def test_list_sort_ascending(repo: SqliteRepository) -> None:
    """descending=False returns oldest row first."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="old", service_key="svc", last_seen_at=1000)
    await _insert_sig(repo, template_hash="new", service_key="svc", last_seen_at=9000)

    rows, _ = await sig_repo.list(filter=SignatureFilter(), limit=100, offset=0, descending=False)
    assert rows[0].template_hash == "old"
    assert rows[1].template_hash == "new"


async def test_list_unknown_sort_key_falls_back_to_last_seen_at(
    repo: SqliteRepository,
) -> None:
    """Unknown sort key falls back to last_seen_at DESC default."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="old", service_key="svc", last_seen_at=1000)
    await _insert_sig(repo, template_hash="new", service_key="svc", last_seen_at=9000)

    rows, _ = await sig_repo.list(
        filter=SignatureFilter(), limit=100, offset=0, sort="nonexistent_column"
    )
    # Falls back to last_seen_at DESC: newest first
    assert rows[0].template_hash == "new"


# ===========================================================================
# get — hit and miss
# ===========================================================================


async def test_get_returns_matching_row(repo: SqliteRepository) -> None:
    """get(h, s) returns the matching Signature row with correct fields."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(
        repo,
        template_hash="abc123",
        service_key="svcA",
        template_str="error connecting to <*>",
        label="network-errors",
        status="suppressed",
        first_seen_at=500,
        last_seen_at=9999,
        total_count=42,
    )

    result = await sig_repo.get("abc123", "svcA")
    assert result is not None
    assert isinstance(result, Signature)
    assert result.template_hash == "abc123"
    assert result.service_key == "svcA"
    assert result.template_str == "error connecting to <*>"
    assert result.label == "network-errors"
    assert result.status == "suppressed"
    assert result.first_seen_at == 500  # noqa: PLR2004
    assert result.last_seen_at == 9999  # noqa: PLR2004
    assert result.total_count == 42  # noqa: PLR2004


async def test_get_missing_returns_none(repo: SqliteRepository) -> None:
    """get() returns None when no row exists for the composite key."""
    sig_repo = SignaturesRepository(repo)
    result = await sig_repo.get("nonexistent", "svcX")
    assert result is None


async def test_get_different_service_key_returns_none(repo: SqliteRepository) -> None:
    """Same template_hash but different service_key → None (composite PK)."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svcA")
    result = await sig_repo.get("h1", "svcB")
    assert result is None


# ===========================================================================
# update_label
# ===========================================================================


async def test_update_label_sets_label(repo: SqliteRepository) -> None:
    """update_label() persists the new label and returns the updated row."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svc", label=None)

    updated = await sig_repo.update_label("h1", "svc", "new-label")
    assert updated is not None
    assert updated.label == "new-label"

    # Verify persistence: re-fetch independently
    refetched = await sig_repo.get("h1", "svc")
    assert refetched is not None
    assert refetched.label == "new-label"


async def test_update_label_clears_label(repo: SqliteRepository) -> None:
    """update_label(None) clears an existing label back to None."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svc", label="existing")

    updated = await sig_repo.update_label("h1", "svc", None)
    assert updated is not None
    assert updated.label is None

    refetched = await sig_repo.get("h1", "svc")
    assert refetched is not None
    assert refetched.label is None


async def test_update_label_missing_key_returns_none(repo: SqliteRepository) -> None:
    """update_label() returns None when the composite key does not exist."""
    sig_repo = SignaturesRepository(repo)
    result = await sig_repo.update_label("nosuchkey", "svc", "whatever")
    assert result is None


# ===========================================================================
# set_status
# ===========================================================================


async def test_set_status_active_to_suppressed(repo: SqliteRepository) -> None:
    """set_status() persists the new status and returns the updated row."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svc", status="active")

    updated = await sig_repo.set_status("h1", "svc", "suppressed")
    assert updated is not None
    assert updated.status == "suppressed"

    refetched = await sig_repo.get("h1", "svc")
    assert refetched is not None
    assert refetched.status == "suppressed"


async def test_set_status_suppressed_to_expected(repo: SqliteRepository) -> None:
    """set_status() can transition from suppressed to expected."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svc", status="suppressed")

    updated = await sig_repo.set_status("h1", "svc", "expected")
    assert updated is not None
    assert updated.status == "expected"


async def test_set_status_missing_key_returns_none(repo: SqliteRepository) -> None:
    """set_status() returns None when the composite key does not exist."""
    sig_repo = SignaturesRepository(repo)
    result = await sig_repo.set_status("nosuchkey", "svc", "active")
    assert result is None


async def test_set_status_does_not_affect_label(repo: SqliteRepository) -> None:
    """set_status() does not modify the label column."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(repo, template_hash="h1", service_key="svc", label="keep-me", status="active")

    updated = await sig_repo.set_status("h1", "svc", "suppressed")
    assert updated is not None
    assert updated.label == "keep-me"


# ===========================================================================
# list — sort by alternative columns
# ===========================================================================


async def test_list_sort_by_total_count_descending(repo: SqliteRepository) -> None:
    """sort='total_count', descending=True returns highest count first."""
    sig_repo = SignaturesRepository(repo)
    await _insert_sig(
        repo, template_hash="low", service_key="svc", total_count=1, last_seen_at=9000
    )
    await _insert_sig(
        repo,
        template_hash="high",
        service_key="svc",
        total_count=100,
        last_seen_at=1000,
    )

    rows, _ = await sig_repo.list(
        filter=SignatureFilter(), limit=100, offset=0, sort="total_count", descending=True
    )
    assert rows[0].template_hash == "high"
    assert rows[1].template_hash == "low"
