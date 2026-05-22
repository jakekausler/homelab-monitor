"""Tests for alembic migration 0020: add compose_file_path to suggestions_docker.

Round-trip, nullable column, existing-row preservation, downgrade schema restoration,
index preservation, idempotency.
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import ALEMBIC_DIR


def _make_cfg(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.mark.asyncio
async def test_migration_0020_adds_compose_file_path_column(db_url: str) -> None:
    """After upgrade to head, suggestions_docker has compose_file_path column (nullable)."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_columns(sync_conn: object) -> dict[str, bool]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return {}
                cols = inspector.get_columns("suggestions_docker")
                return {col["name"]: col["nullable"] for col in cols}

            cols = await conn.run_sync(_get_columns)

        assert "compose_file_path" in cols, "compose_file_path column not found"
        assert cols["compose_file_path"] is True, "compose_file_path must be nullable"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0020_existing_rows_have_null_compose_file_path(db_url: str) -> None:
    """Existing rows seeded at 0019 have NULL compose_file_path after upgrade to 0020."""
    cfg = _make_cfg(db_url)
    # Upgrade only to 0019 so we can seed rows before 0020 runs.
    command.upgrade(cfg, "0019")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO suggestions "
                    "(id, kind, deduplication_key, state, created_at, updated_at) "
                    "VALUES (:id, :kind, :dedup, :state, :created, :updated)"
                ),
                {
                    "id": "anchor-0020-test",
                    "kind": "docker_container_discovered",
                    "dedup": "container-abc",
                    "state": "pending",
                    "created": "2026-01-01T00:00:00Z",
                    "updated": "2026-01-01T00:00:00Z",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO suggestions_docker "
                    "(suggestion_id, container_id, container_name, image_ref, "
                    " labels_json, detection_reason) "
                    "VALUES (:sid, :cid, :cn, :ir, :lj, :dr)"
                ),
                {
                    "sid": "anchor-0020-test",
                    "cid": "container-abc",
                    "cn": "mycontainer",
                    "ir": "nginx:latest",
                    "lj": "{}",
                    "dr": "no_homelab_monitor_label",
                },
            )
            await conn.commit()
    finally:
        await engine.dispose()

    # Now upgrade through 0020.
    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT compose_file_path FROM suggestions_docker "
                        "WHERE suggestion_id = :sid"
                    ),
                    {"sid": "anchor-0020-test"},
                )
            ).first()

        assert row is not None, "seeded sidecar row not found after upgrade"
        assert row[0] is None, f"expected NULL compose_file_path, got {row[0]!r}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0020_downgrade_removes_compose_file_path(db_url: str) -> None:
    """After downgrade to 0019, compose_file_path column no longer exists."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0019")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_col_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {col["name"] for col in inspector.get_columns("suggestions_docker")}

            cols = await conn.run_sync(_get_col_names)

        assert "compose_file_path" not in cols, "compose_file_path should be absent after downgrade"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0020_downgrade_preserves_idx_suggestions_docker_container_id(
    db_url: str,
) -> None:
    """After downgrade to 0019, idx_suggestions_docker_container_id index still exists."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0019")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_index_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {
                    idx["name"]
                    for idx in inspector.get_indexes("suggestions_docker") or []  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
                }

            indexes = await conn.run_sync(_get_index_names)

        assert "idx_suggestions_docker_container_id" in indexes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0020_round_trip(db_url: str) -> None:
    """upgrade → downgrade → upgrade leaves compose_file_path column present and nullable."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0019")
    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_columns(sync_conn: object) -> dict[str, bool]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return {}
                return {
                    col["name"]: col["nullable"]
                    for col in inspector.get_columns("suggestions_docker")
                }

            cols = await conn.run_sync(_get_columns)

        assert "compose_file_path" in cols
        assert cols["compose_file_path"] is True
    finally:
        await engine.dispose()
