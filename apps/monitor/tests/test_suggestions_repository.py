"""Tests for SuggestionsRepository: anchor + Docker sidecar CRUD.

Tests cover insert, upsert, state transitions, list, pagination, and constraints.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from homelab_monitor.kernel.db.repositories.suggestions_repository import (
    SuggestionsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


@pytest.mark.asyncio
async def test_insert_creates_anchor_and_sidecar_rows(repo: SqliteRepository) -> None:
    """Single insert_or_update_docker_suggestion_conn → both anchor + sidecar rows."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    async with repo.transaction() as conn:
        suggestion_id = await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="abc123",
            container_id="container-xyz",
            container_name="test-container",
            image_ref="nginx:latest",
            labels={"some": "label"},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Verify anchor row.
    anchor = await repo.fetch_one(
        text("SELECT id, kind, deduplication_key, state FROM suggestions WHERE id = :id"),
        {"id": suggestion_id},
    )
    assert anchor is not None
    assert anchor[0] == suggestion_id
    assert anchor[1] == "docker_container_discovered"
    assert anchor[2] == "abc123"
    assert anchor[3] == "pending"

    # Verify sidecar row.
    sidecar = await repo.fetch_one(
        text(
            "SELECT suggestion_id, container_id, container_name "
            "FROM suggestions_docker WHERE suggestion_id = :id"
        ),
        {"id": suggestion_id},
    )
    assert sidecar is not None
    assert sidecar[0] == suggestion_id
    assert sidecar[1] == "container-xyz"
    assert sidecar[2] == "test-container"


@pytest.mark.asyncio
async def test_upsert_updates_existing_row(repo: SqliteRepository) -> None:
    """Second call with same (kind, dedup_key) → UPDATE not INSERT."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # First insert.
    async with repo.transaction() as conn:
        suggestion_id = await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="abc123",
            container_id="container-xyz",
            container_name="test-container",
            image_ref="nginx:latest",
            labels={"some": "label"},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Second upsert with same dedup key but different container_name.
    async with repo.transaction() as conn:
        suggestion_id_2 = await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="abc123",
            container_id="container-xyz",
            container_name="test-container-renamed",
            image_ref="nginx:1.0",
            labels={"another": "label"},
            compose_project="myapp",
            compose_service="web",
            compose_file_path="/test/docker-compose.yml",
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Should return the same ID.
    assert suggestion_id_2 == suggestion_id

    # Verify anchor row — id unchanged, state unchanged.
    anchor = await repo.fetch_one(
        text("SELECT id, state FROM suggestions WHERE id = :id"),
        {"id": suggestion_id},
    )
    assert anchor is not None
    assert anchor[0] == suggestion_id
    assert anchor[1] == "pending"

    # Verify sidecar row was updated.
    sidecar = await repo.fetch_one(
        text(
            "SELECT container_name, compose_project, compose_service "
            "FROM suggestions_docker WHERE suggestion_id = :id"
        ),
        {"id": suggestion_id},
    )
    assert sidecar is not None
    assert sidecar[0] == "test-container-renamed"
    assert sidecar[1] == "myapp"
    assert sidecar[2] == "web"


@pytest.mark.asyncio
async def test_unique_constraint_prevents_duplicate_kind_dedup_key(
    repo: SqliteRepository,
) -> None:
    """UNIQUE(kind, deduplication_key) enforced via direct INSERT."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # Insert via the conn helper.
    async with repo.transaction() as conn:
        await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="abc123",
            container_id="container-xyz",
            container_name="test-container",
            image_ref="nginx:latest",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Attempt direct INSERT with same (kind, dedup_key) → should fail.
    with pytest.raises(IntegrityError):
        async with repo.transaction() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO suggestions
                    (id, kind, deduplication_key, state, created_at, updated_at)
                    VALUES (:id, :kind, :dedup, :state, :created, :updated)
                    """
                ),
                {
                    "id": "another-id",
                    "kind": "docker_container_discovered",
                    "dedup": "abc123",
                    "state": "pending",
                    "created": now,
                    "updated": now,
                },
            )


@pytest.mark.asyncio
async def test_upsert_preserves_terminal_state(repo: SqliteRepository) -> None:
    """After marking ignored, upsert preserves the ignored state."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # Insert.
    async with repo.transaction() as conn:
        suggestion_id = await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="abc123",
            container_id="container-xyz",
            container_name="test-container",
            image_ref="nginx:latest",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Manually mark as ignored.
    await repo.execute(
        text("UPDATE suggestions SET state = :state WHERE id = :id"),
        {"id": suggestion_id, "state": "ignored"},
    )

    # Upsert again.
    async with repo.transaction() as conn:
        await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="abc123",
            container_id="container-xyz",
            container_name="test-container-new",
            image_ref="nginx:2.0",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Verify state is still ignored.
    row = await repo.fetch_one(
        text("SELECT state FROM suggestions WHERE id = :id"),
        {"id": suggestion_id},
    )
    assert row is not None
    assert row[0] == "ignored"


@pytest.mark.asyncio
async def test_mark_container_gone_transitions_state(repo: SqliteRepository) -> None:
    """mark_container_gone_conn transitions pending → container_gone."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # Insert pending suggestion.
    async with repo.transaction() as conn:
        suggestion_id = await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="abc123",
            container_id="container-xyz",
            container_name="test-container",
            image_ref="nginx:latest",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Mark container gone.
    async with repo.transaction() as conn:
        affected = await sugg_repo.mark_container_gone_conn(
            conn, container_id="container-xyz", now=now
        )
    assert affected == 1

    # Verify state is container_gone.
    row = await repo.fetch_one(
        text("SELECT state FROM suggestions WHERE id = :id"),
        {"id": suggestion_id},
    )
    assert row is not None
    assert row[0] == "container_gone"


@pytest.mark.asyncio
async def test_re_upsert_resurrects_container_gone_to_pending(repo: SqliteRepository) -> None:
    """When container_gone suggestion is re-upserted, state resets to pending."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # Insert and mark as container_gone.
    async with repo.transaction() as conn:
        suggestion_id = await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="abc123",
            container_id="container-xyz",
            container_name="test-container",
            image_ref="nginx:latest",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Mark container gone.
    async with repo.transaction() as conn:
        await sugg_repo.mark_container_gone_conn(conn, container_id="container-xyz", now=now)

    # Verify state is container_gone.
    row = await repo.fetch_one(
        text("SELECT state FROM suggestions WHERE id = :id"),
        {"id": suggestion_id},
    )
    assert row is not None
    assert row[0] == "container_gone"

    # Re-upsert the same logical container.
    async with repo.transaction() as conn:
        await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="abc123",
            container_id="container-xyz-new",
            container_name="test-container",
            image_ref="nginx:latest",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Verify state resurrected to pending.
    row = await repo.fetch_one(
        text("SELECT state FROM suggestions WHERE id = :id"),
        {"id": suggestion_id},
    )
    assert row is not None
    assert row[0] == "pending"


@pytest.mark.asyncio
async def test_mark_container_gone_skips_terminal_states(
    repo: SqliteRepository,
) -> None:
    """mark_container_gone on already-ignored suggestion returns 0."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # Insert and mark as ignored.
    async with repo.transaction() as conn:
        suggestion_id = await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="abc123",
            container_id="container-xyz",
            container_name="test-container",
            image_ref="nginx:latest",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    await repo.execute(
        text("UPDATE suggestions SET state = :state WHERE id = :id"),
        {"id": suggestion_id, "state": "ignored"},
    )

    # Try to mark gone.
    async with repo.transaction() as conn:
        affected = await sugg_repo.mark_container_gone_conn(
            conn, container_id="container-xyz", now=now
        )
    assert affected == 0

    # Verify state is still ignored.
    row = await repo.fetch_one(
        text("SELECT state FROM suggestions WHERE id = :id"),
        {"id": suggestion_id},
    )
    assert row is not None
    assert row[0] == "ignored"


@pytest.mark.asyncio
async def test_list_pending_excludes_container_gone(repo: SqliteRepository) -> None:
    """list_pending_docker_suggestions(status='pending') filters out container_gone."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # Insert 3 rows with different states.
    async with repo.transaction() as conn:
        for i, state_override in enumerate(["pending", "ignored", "container_gone"]):
            sid = await sugg_repo.insert_or_update_docker_suggestion_conn(
                conn,
                kind="docker_container_discovered",
                deduplication_key=f"dedup-{i}",
                container_id=f"container-{i}",
                container_name=f"container-{i}",
                image_ref="nginx:latest",
                labels={},
                compose_project=None,
                compose_service=None,
                compose_file_path=None,
                detection_reason="no_homelab_monitor_label",
                now=now,
            )
            if state_override != "pending":
                await conn.execute(
                    text("UPDATE suggestions SET state = :state WHERE id = :id"),
                    {"id": sid, "state": state_override},
                )

    # List pending.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(
        status="pending", limit=50, cursor=None
    )

    assert len(rows) == 1
    assert rows[0].state == "pending"
    assert rows[0].deduplication_key == "dedup-0"


@pytest.mark.asyncio
async def test_list_all_includes_every_state(repo: SqliteRepository) -> None:
    """list_pending_docker_suggestions(status='all') returns all rows."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # Insert 3 rows.
    async with repo.transaction() as conn:
        for i, state_override in enumerate(["pending", "ignored", "container_gone"]):
            sid = await sugg_repo.insert_or_update_docker_suggestion_conn(
                conn,
                kind="docker_container_discovered",
                deduplication_key=f"dedup-{i}",
                container_id=f"container-{i}",
                container_name=f"container-{i}",
                image_ref="nginx:latest",
                labels={},
                compose_project=None,
                compose_service=None,
                compose_file_path=None,
                detection_reason="no_homelab_monitor_label",
                now=now,
            )
            if state_override != "pending":
                await conn.execute(
                    text("UPDATE suggestions SET state = :state WHERE id = :id"),
                    {"id": sid, "state": state_override},
                )

    # List all.
    rows, _ = await sugg_repo.list_pending_docker_suggestions(status="all", limit=50, cursor=None)

    assert len(rows) == 3  # noqa: PLR2004
    states = {row.state for row in rows}
    assert states == {"pending", "ignored", "container_gone"}


@pytest.mark.asyncio
async def test_list_returns_label_collision_kind(repo: SqliteRepository) -> None:
    """Both docker_container_discovered and docker_label_collision kinds surface."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # Insert one of each kind.
    async with repo.transaction() as conn:
        await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="dedup-1",
            container_id="container-1",
            container_name="container-1",
            image_ref="nginx:latest",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )
        await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_label_collision",
            deduplication_key="dedup-2",
            container_id="container-2",
            container_name="container-2",
            image_ref="nginx:latest",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="label_collision",
            now=now,
        )

    # List pending (status=pending returns both if both are pending).
    rows, _ = await sugg_repo.list_pending_docker_suggestions(
        status="pending", limit=50, cursor=None
    )

    assert len(rows) == 2  # noqa: PLR2004
    kinds = {row.kind for row in rows}
    assert kinds == {"docker_container_discovered", "docker_label_collision"}


@pytest.mark.asyncio
async def test_cursor_pagination_strict_tuple_compare(repo: SqliteRepository) -> None:
    """Pagination with cursor=created_at|id works across pages."""
    sugg_repo = SuggestionsRepository(repo)

    # Insert 120 rows with monotonically increasing created_at.
    async with repo.transaction() as conn:
        for i in range(120):
            # Increment seconds by 1 per row.
            created_at = (
                f"2026-01-01T00:02:{i % 60:02d}Z"
                if i < 60  # noqa: PLR2004
                else f"2026-01-01T00:03:{(i - 60) % 60:02d}Z"
            )
            await sugg_repo.insert_or_update_docker_suggestion_conn(
                conn,
                kind="docker_container_discovered",
                deduplication_key=f"dedup-{i}",
                container_id=f"container-{i}",
                container_name=f"container-{i}",
                image_ref="nginx:latest",
                labels={},
                compose_project=None,
                compose_service=None,
                compose_file_path=None,
                detection_reason="no_homelab_monitor_label",
                now=created_at,
            )

    # First page (limit=50).
    page1, cursor1 = await sugg_repo.list_pending_docker_suggestions(
        status="all", limit=50, cursor=None
    )
    assert len(page1) == 50  # noqa: PLR2004
    assert cursor1 is not None

    # Second page (limit=50).
    page2, cursor2 = await sugg_repo.list_pending_docker_suggestions(
        status="all", limit=50, cursor=cursor1
    )
    assert len(page2) == 50  # noqa: PLR2004
    assert cursor2 is not None

    # Third page (remaining 20).
    page3, cursor3 = await sugg_repo.list_pending_docker_suggestions(
        status="all", limit=50, cursor=cursor2
    )
    assert len(page3) == 20  # noqa: PLR2004
    assert cursor3 is None  # No more pages.

    # Verify no overlap between pages.
    ids_1 = {row.id for row in page1}
    ids_2 = {row.id for row in page2}
    ids_3 = {row.id for row in page3}
    assert len(ids_1 & ids_2) == 0
    assert len(ids_2 & ids_3) == 0


@pytest.mark.asyncio
async def test_invalid_status_raises_value_error(repo: SqliteRepository) -> None:
    """Calling list_pending with status='bogus' raises ValueError."""
    sugg_repo = SuggestionsRepository(repo)

    with pytest.raises(ValueError, match="bogus"):
        await sugg_repo.list_pending_docker_suggestions(
            status="bogus",
            limit=50,
            cursor=None,  # pyright: ignore[reportCallIssue]
        )


@pytest.mark.asyncio
async def test_invalid_cursor_raises_value_error(repo: SqliteRepository) -> None:
    """Cursor without '|' or with empty parts raises ValueError."""
    sugg_repo = SuggestionsRepository(repo)

    with pytest.raises(ValueError):
        await sugg_repo.list_pending_docker_suggestions(
            status="pending", limit=50, cursor="no-pipe"
        )

    with pytest.raises(ValueError):
        await sugg_repo.list_pending_docker_suggestions(
            status="pending", limit=50, cursor="|empty-parts"
        )


@pytest.mark.asyncio
async def test_insert_stores_compose_file_path(repo: SqliteRepository) -> None:
    """compose_file_path is persisted and retrieved correctly."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    async with repo.transaction() as conn:
        sid = await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="cfp-test",
            container_id="container-cfp",
            container_name="cfp-container",
            image_ref="nginx:latest",
            labels={},
            compose_project="myproject",
            compose_service="web",
            compose_file_path="/storage/docker/compose/docker-compose.yml",
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Verify in sidecar table.
    row = await repo.fetch_one(
        text("SELECT compose_file_path FROM suggestions_docker WHERE suggestion_id = :id"),
        {"id": sid},
    )
    assert row is not None
    assert row[0] == "/storage/docker/compose/docker-compose.yml"

    # Verify it appears in list_pending_docker_suggestions.
    items, _ = await sugg_repo.list_pending_docker_suggestions(status="pending", limit=50)
    assert len(items) == 1
    assert items[0].compose_file_path == "/storage/docker/compose/docker-compose.yml"


@pytest.mark.asyncio
async def test_set_state_conn_invalid_state_raises_value_error(
    repo: SqliteRepository,
) -> None:
    """Calling set_state_conn with invalid state raises ValueError."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # Create a suggestion first.
    async with repo.transaction() as conn:
        suggestion_id = await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="state-test",
            container_id="container-state",
            container_name="state-container",
            image_ref="nginx:latest",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Try to set to an invalid state.
    async with repo.transaction() as conn:
        with pytest.raises(ValueError, match="invalid suggestion state"):
            await SuggestionsRepository.set_state_conn(
                conn,
                suggestion_id=suggestion_id,
                new_state="invalid_state",
                now=utc_now_iso(),
            )


@pytest.mark.asyncio
async def test_set_state_conn_updates_state(repo: SqliteRepository) -> None:
    """Calling set_state_conn with valid state updates the row and returns True."""
    sugg_repo = SuggestionsRepository(repo)
    now = utc_now_iso()

    # Create a suggestion.
    async with repo.transaction() as conn:
        suggestion_id = await sugg_repo.insert_or_update_docker_suggestion_conn(
            conn,
            kind="docker_container_discovered",
            deduplication_key="state-update-test",
            container_id="container-update",
            container_name="update-container",
            image_ref="nginx:latest",
            labels={},
            compose_project=None,
            compose_service=None,
            compose_file_path=None,
            detection_reason="no_homelab_monitor_label",
            now=now,
        )

    # Update state to accepted (a valid state).
    async with repo.transaction() as conn:
        result = await SuggestionsRepository.set_state_conn(
            conn,
            suggestion_id=suggestion_id,
            new_state="accepted",
            now=utc_now_iso(),
        )
        assert result is True

    # Verify the state was updated.
    row = await repo.fetch_one(
        text("SELECT state FROM suggestions WHERE id = :id"),
        {"id": suggestion_id},
    )
    assert row is not None
    assert row[0] == "accepted"
