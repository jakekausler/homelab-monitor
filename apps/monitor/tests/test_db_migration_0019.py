"""Tests for alembic migration 0019: suggestions schema rebuild + suggestions_docker sidecar.

Migration round-trip, idempotency, schema validation, FK CASCADE behavior.
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import ALEMBIC_DIR


@pytest.mark.asyncio
async def test_migration_0019_creates_suggestions_with_new_shape(db_url: str) -> None:
    """After upgrade to head, suggestions table has 6 columns with NOT NULL constraints."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)

    # Upgrade to head (includes migration 0019).
    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            # Use inspect to get column metadata.
            def _get_columns(sync_conn: object) -> list[tuple[str, str, bool]]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return []
                cols = inspector.get_columns("suggestions")
                return [(col["name"], str(col["type"]), not col["nullable"]) for col in cols]

            columns = await conn.run_sync(_get_columns)

        # Verify the 6 expected columns exist.
        col_names = {col[0] for col in columns}
        expected = {
            "id",
            "kind",
            "deduplication_key",
            "state",
            "created_at",
            "updated_at",
        }
        assert col_names == expected, f"Expected columns {expected}, got {col_names}"

        # Verify NOT NULL constraints on key columns.
        col_dict = {col[0]: col[2] for col in columns}
        assert col_dict["id"] is True
        assert col_dict["kind"] is True
        assert col_dict["deduplication_key"] is True
        assert col_dict["state"] is True
        assert col_dict["created_at"] is True
        assert col_dict["updated_at"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0019_creates_suggestions_docker_with_fk_cascade(
    db_url: str,
) -> None:
    """suggestions_docker exists with FK to suggestions.id ON DELETE CASCADE."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            # Verify suggestions_docker exists.
            def _get_columns(sync_conn: object) -> list[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return []
                try:
                    cols = inspector.get_columns("suggestions_docker")
                    return [col["name"] for col in cols]
                except Exception:
                    return []

            docker_cols = await conn.run_sync(_get_columns)
            assert len(docker_cols) > 0, "suggestions_docker table not found"

            # Insert an anchor row.
            await conn.execute(
                text(
                    """
                    INSERT INTO suggestions
                    (id, kind, deduplication_key, state, created_at, updated_at)
                    VALUES (:id, :kind, :dedup, :state, :created, :updated)
                    """
                ),
                {
                    "id": "test-anchor-1",
                    "kind": "docker_container_discovered",
                    "dedup": "abc123",
                    "state": "pending",
                    "created": "2026-01-01T00:00:00Z",
                    "updated": "2026-01-01T00:00:00Z",
                },
            )

            # Insert a sidecar row.
            await conn.execute(
                text(
                    """
                    INSERT INTO suggestions_docker
                    (
                        suggestion_id, container_id, container_name,
                        image_ref, labels_json, detection_reason
                    )
                    VALUES (:sid, :cid, :cn, :ir, :labels, :dr)
                    """
                ),
                {
                    "sid": "test-anchor-1",
                    "cid": "container-xyz",
                    "cn": "test-container",
                    "ir": "nginx:latest",
                    "labels": "{}",
                    "dr": "no_homelab_monitor_label",
                },
            )

            # Verify sidecar exists.
            sidecar = (
                await conn.execute(
                    text("SELECT suggestion_id FROM suggestions_docker WHERE suggestion_id = :id"),
                    {"id": "test-anchor-1"},
                )
            ).first()
            assert sidecar is not None

            # Delete the anchor — sidecar should cascade.
            await conn.execute(
                text("DELETE FROM suggestions WHERE id = :id"),
                {"id": "test-anchor-1"},
            )

            # Verify sidecar is gone.
            sidecar_after = (
                await conn.execute(
                    text("SELECT suggestion_id FROM suggestions_docker WHERE suggestion_id = :id"),
                    {"id": "test-anchor-1"},
                )
            ).first()
            assert sidecar_after is None, "FK CASCADE did not delete sidecar row"

            await conn.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0019_creates_both_indexes(db_url: str) -> None:
    """Both idx_suggestions_state_kind and idx_suggestions_docker_container_id exist."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_index_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                # Get indexes on suggestions table.
                sugg_indexes: set[str] = {
                    idx["name"]
                    for idx in inspector.get_indexes("suggestions") or []  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType,reportUnknownArgumentType]
                }
                # Get indexes on suggestions_docker table.
                docker_indexes: set[str] = {
                    idx["name"]
                    for idx in inspector.get_indexes("suggestions_docker") or []  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
                }
                return sugg_indexes | docker_indexes  # pyright: ignore[reportUnknownVariableType]

            indexes = await conn.run_sync(_get_index_names)

        assert "idx_suggestions_state_kind" in indexes
        assert "idx_suggestions_docker_container_id" in indexes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0019_preserves_legacy_stub_rows(db_url: str) -> None:
    """Pre-0019 stub rows survive upgrade with deduplication_key=id, state='pending'."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)

    # Downgrade to 0018 (before 0019).
    command.upgrade(cfg, "0018")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            # Insert a legacy stub row (3 columns: id, kind, created_at).
            legacy_id = "legacy-stub-1"
            await conn.execute(
                text(
                    """
                    INSERT INTO suggestions (id, kind, created_at)
                    VALUES (:id, :kind, :created)
                    """
                ),
                {
                    "id": legacy_id,
                    "kind": "test_legacy",
                    "created": "2026-01-01T00:00:00Z",
                },
            )
            await conn.commit()
    finally:
        await engine.dispose()

    # Now upgrade to 0019.
    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            # Verify the legacy row exists and has been populated.
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT id, kind, deduplication_key, state, created_at, updated_at
                        FROM suggestions WHERE id = :id
                        """
                    ),
                    {"id": legacy_id},
                )
            ).first()

        assert row is not None
        assert row[0] == legacy_id
        assert row[1] == "test_legacy"
        # deduplication_key should be set to id for legacy rows.
        assert row[2] == legacy_id
        # state should be 'pending'.
        assert row[3] == "pending"
        # created_at preserved, updated_at should match created_at.
        assert row[4] == "2026-01-01T00:00:00Z"
        assert row[5] == "2026-01-01T00:00:00Z"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0019_is_idempotent(db_url: str) -> None:
    """Calling upgrade twice does not error."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(cfg, "head")
    # Second upgrade should be a no-op.
    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            # Verify tables still exist.
            def _get_tables(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return set(inspector.get_table_names() or [])

            tables = await conn.run_sync(_get_tables)

        assert "suggestions" in tables
        assert "suggestions_docker" in tables
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0019_downgrade_restores_stub(db_url: str) -> None:
    """After downgrade to 0018, suggestions has only (id, kind, created_at)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)

    # Upgrade to head.
    command.upgrade(cfg, "head")

    # Downgrade to 0018.
    command.downgrade(cfg, "0018")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_columns_and_tables(sync_conn: object) -> tuple[set[str], bool]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set(), False
                tables = set(inspector.get_table_names() or [])
                # Check suggestions columns.
                sugg_cols: set[str] = {
                    col["name"]
                    for col in inspector.get_columns("suggestions") or []  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType,reportUnknownArgumentType]
                }
                docker_exists = "suggestions_docker" in tables
                return sugg_cols, docker_exists  # pyright: ignore[reportUnknownVariableType]

            cols, docker_exists = await conn.run_sync(_get_columns_and_tables)

        # After downgrade, suggestions should have only 3 columns.
        assert cols == {"id", "kind", "created_at"}
        # suggestions_docker should not exist.
        assert docker_exists is False
    finally:
        await engine.dispose()
