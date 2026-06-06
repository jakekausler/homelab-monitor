"""Tests for AnnotationsRepository (STAGE-004-029).

Project test conventions:
- Framework: pytest-asyncio (asyncio_mode=auto — bare async def, no decorator)
- DB: tempfile-backed SQLite + alembic head via `repo` fixture (conftest)
- Assertions: direct field checks against persisted state
"""

from __future__ import annotations

from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.signature_annotations_repo import (
    Annotation,
    AnnotationsRepository,
)

# ---------------------------------------------------------------------------
# Seeding helper — inserts a parent signature row directly
# ---------------------------------------------------------------------------


async def _insert_sig(
    repo: SqliteRepository,
    *,
    template_hash: str,
    service_key: str,
    template_str: str = "foo <*> bar",
) -> None:
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO log_signatures "
                "  (template_hash, service_key, template_str, label, status, "
                "   first_seen_at, last_seen_at, total_count) "
                "VALUES "
                "  (:h, :s, :tstr, NULL, 'active', 1000, 2000, 5)"
            ),
            {
                "h": template_hash,
                "s": service_key,
                "tstr": template_str,
            },
        )


# ===========================================================================
# create
# ===========================================================================


async def test_create_returns_annotation_with_id_and_timestamp(
    repo: SqliteRepository,
) -> None:
    """create() returns an Annotation with id > 0, note, author, and ISO created_at."""
    await _insert_sig(repo, template_hash="h1", service_key="svc1")
    ann_repo = AnnotationsRepository(repo)

    result = await ann_repo.create(
        template_hash="h1",
        service_key="svc1",
        note="test note",
        author="testuser",
    )

    assert result.id > 0
    assert result.note == "test note"
    assert result.author == "testuser"
    assert "T" in result.created_at  # ISO format
    assert "+00:00" in result.created_at  # UTC offset


# ===========================================================================
# list_for_signature
# ===========================================================================


async def test_list_for_signature_orders_newest_first(repo: SqliteRepository) -> None:
    """list_for_signature returns annotations ordered by created_at DESC, id DESC."""
    await _insert_sig(repo, template_hash="h1", service_key="svc1")
    ann_repo = AnnotationsRepository(repo)

    # Create 3 annotations in sequence
    ann1 = await ann_repo.create(
        template_hash="h1", service_key="svc1", note="first", author="user1"
    )
    ann2 = await ann_repo.create(
        template_hash="h1", service_key="svc1", note="second", author="user2"
    )
    ann3 = await ann_repo.create(
        template_hash="h1", service_key="svc1", note="third", author="user3"
    )

    rows = await ann_repo.list_for_signature("h1", "svc1")
    assert len(rows) == 3  # noqa: PLR2004
    # Newest first (reverse insertion order, with id DESC as tiebreak)
    assert rows[0].id == ann3.id
    assert rows[1].id == ann2.id
    assert rows[2].id == ann1.id


async def test_list_for_signature_empty_returns_empty_list(
    repo: SqliteRepository,
) -> None:
    """list_for_signature on unknown signature returns []."""
    ann_repo = AnnotationsRepository(repo)
    rows = await ann_repo.list_for_signature("nonexistent", "svc")
    assert rows == []


async def test_list_for_signature_scopes_by_composite_key(
    repo: SqliteRepository,
) -> None:
    """list_for_signature returns only annotations for the matching (h, s) pair."""
    await _insert_sig(repo, template_hash="h1", service_key="svc1")
    await _insert_sig(repo, template_hash="h2", service_key="svc2")
    ann_repo = AnnotationsRepository(repo)

    # Add annotations to both signatures
    await ann_repo.create(template_hash="h1", service_key="svc1", note="ann1", author="user1")
    await ann_repo.create(template_hash="h2", service_key="svc2", note="ann2", author="user2")

    # Query h1/svc1 — should only get ann1
    rows = await ann_repo.list_for_signature("h1", "svc1")
    assert len(rows) == 1
    assert rows[0].note == "ann1"


# ===========================================================================
# get
# ===========================================================================


async def test_get_returns_annotation(repo: SqliteRepository) -> None:
    """get(id) returns the matching Annotation."""
    await _insert_sig(repo, template_hash="h1", service_key="svc1")
    ann_repo = AnnotationsRepository(repo)

    created = await ann_repo.create(
        template_hash="h1",
        service_key="svc1",
        note="hello",
        author="testuser",
    )

    fetched = await ann_repo.get(created.id)
    assert fetched is not None
    assert isinstance(fetched, Annotation)
    assert fetched.id == created.id
    assert fetched.note == "hello"
    assert fetched.author == "testuser"


async def test_get_missing_returns_none(repo: SqliteRepository) -> None:
    """get(id) returns None for nonexistent id."""
    ann_repo = AnnotationsRepository(repo)
    result = await ann_repo.get(9999)
    assert result is None


# ===========================================================================
# delete
# ===========================================================================


async def test_delete_hit_returns_true_and_removes_row(
    repo: SqliteRepository,
) -> None:
    """delete(id, h, s) returns True and removes the row."""
    await _insert_sig(repo, template_hash="h1", service_key="svc1")
    ann_repo = AnnotationsRepository(repo)

    created = await ann_repo.create(
        template_hash="h1",
        service_key="svc1",
        note="to delete",
        author="user",
    )

    result = await ann_repo.delete(created.id, "h1", "svc1")
    assert result is True

    # Verify it's gone
    fetched = await ann_repo.get(created.id)
    assert fetched is None


async def test_delete_miss_returns_false(repo: SqliteRepository) -> None:
    """delete(9999, h, s) returns False."""
    ann_repo = AnnotationsRepository(repo)
    result = await ann_repo.delete(9999, "h1", "svc1")
    assert result is False


async def test_delete_cross_signature_returns_false(repo: SqliteRepository) -> None:
    """delete(id, h2, s2) returns False if the annotation belongs to (h1, s1)."""
    await _insert_sig(repo, template_hash="h1", service_key="svc1")
    await _insert_sig(repo, template_hash="h2", service_key="svc2")
    ann_repo = AnnotationsRepository(repo)

    # Create annotation under (h1, svc1)
    created = await ann_repo.create(
        template_hash="h1",
        service_key="svc1",
        note="under h1/svc1",
        author="user",
    )

    # Try to delete with wrong composite key
    result = await ann_repo.delete(created.id, "h2", "svc2")
    assert result is False

    # Verify the annotation still exists
    fetched = await ann_repo.get(created.id)
    assert fetched is not None
    assert fetched.note == "under h1/svc1"
