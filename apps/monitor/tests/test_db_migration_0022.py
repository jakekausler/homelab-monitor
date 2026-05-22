"""Tests for alembic migration 0022: add compose columns + restart_count_24h_cached to targets_docker.

Round-trip, nullable columns, existing-row preservation, downgrade schema restoration,
index preservation after downgrade.
"""  # noqa: E501

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
async def test_migration_0022_adds_four_columns(db_url: str) -> None:
    """After upgrade to head, targets_docker has compose_project, compose_service,
    compose_file_path, restart_count_24h_cached columns."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_columns(sync_conn: object) -> dict[str, bool]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return {}
                return {
                    col["name"]: col["nullable"] for col in inspector.get_columns("targets_docker")
                }

            cols = await conn.run_sync(_get_columns)

        for col in (
            "compose_project",
            "compose_service",
            "compose_file_path",
            "restart_count_24h_cached",
        ):
            assert col in cols, f"column {col!r} not found after upgrade"
            assert cols[col] is True, f"column {col!r} must be nullable"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0022_existing_rows_have_null_new_columns(db_url: str) -> None:
    """Rows seeded at 0021 have NULL values for all four new columns after upgrade to 0022."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "0021")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            # Insert a minimal targets row first (required by FK).
            await conn.execute(
                text(
                    "INSERT INTO targets "
                    "(id, kind, name, labels, status, first_seen, last_seen, created_at) "
                    "VALUES ('tgt-0022-seed', 'docker_container', 'seedcontainer', '{}', "
                    "        'running', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
                    "        '2026-01-01T00:00:00Z')"
                )
            )
            # Insert sidecar row.
            await conn.execute(
                text(
                    "INSERT INTO targets_docker "
                    "(target_id, container_id, restart_count, exit_code) "
                    "VALUES ('tgt-0022-seed', 'cid-seed', 0, 0)"
                )
            )
            await conn.commit()
    finally:
        await engine.dispose()

    command.upgrade(cfg, "head")

    engine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT compose_project, compose_service, compose_file_path, "
                        "       restart_count_24h_cached "
                        "FROM targets_docker WHERE target_id = 'tgt-0022-seed'"
                    )
                )
            ).first()

        assert row is not None, "seeded row not found after upgrade"
        assert row[0] is None, f"compose_project should be NULL, got {row[0]!r}"
        assert row[1] is None, f"compose_service should be NULL, got {row[1]!r}"
        assert row[2] is None, f"compose_file_path should be NULL, got {row[2]!r}"
        assert row[3] is None, f"restart_count_24h_cached should be NULL, got {row[3]!r}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0022_downgrade_removes_four_columns(db_url: str) -> None:
    """After downgrade to 0021, all four added columns are absent from targets_docker."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0021")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_col_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {col["name"] for col in inspector.get_columns("targets_docker")}

            cols = await conn.run_sync(_get_col_names)

        for col in (
            "compose_project",
            "compose_service",
            "compose_file_path",
            "restart_count_24h_cached",
        ):
            assert col not in cols, f"column {col!r} should be absent after downgrade"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0022_downgrade_preserves_prior_indexes(db_url: str) -> None:
    """After downgrade to 0021, idx_targets_docker_container_id and
    idx_targets_docker_previous_container_id still exist."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0021")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_index_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return set()
                return {
                    idx["name"]
                    for idx in inspector.get_indexes("targets_docker") or []  # pyright: ignore[reportUnknownVariableType,reportUnknownMemberType]
                }

            indexes = await conn.run_sync(_get_index_names)

        assert "idx_targets_docker_container_id" in indexes
        assert "idx_targets_docker_previous_container_id" in indexes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0022_round_trip(db_url: str) -> None:
    """upgrade → downgrade → upgrade leaves the four columns present and nullable."""
    cfg = _make_cfg(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0021")
    command.upgrade(cfg, "head")

    engine: AsyncEngine = get_engine(url=db_url)
    try:
        async with engine.connect() as conn:

            def _get_columns(sync_conn: object) -> dict[str, bool]:
                inspector = inspect(sync_conn)
                if inspector is None:
                    return {}
                return {
                    col["name"]: col["nullable"] for col in inspector.get_columns("targets_docker")
                }

            cols = await conn.run_sync(_get_columns)

        for col in (
            "compose_project",
            "compose_service",
            "compose_file_path",
            "restart_count_24h_cached",
        ):
            assert col in cols, f"column {col!r} missing after round-trip"
            assert cols[col] is True, f"column {col!r} must be nullable after round-trip"
    finally:
        await engine.dispose()
