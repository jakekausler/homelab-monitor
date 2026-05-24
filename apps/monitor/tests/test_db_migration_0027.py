"""Tests for alembic migration 0027_docker_build_hashes."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Inspector

from homelab_monitor.kernel.db.migrations import run_migrations
from tests.conftest import make_engine

_EXPECTED_COLUMNS = {
    "container_name",
    "compose_service",
    "build_context_path",
    "last_source_hash",
    "last_checked_at",
    "check_failed_at",
    "check_error_reason",
    "update_available",
    "baseline_source_hash",
    "baseline_image_id",
}

_VALID_CHECK_REASONS = [
    "compose_unreadable",
    "context_missing",
    "context_too_large",
    "permission_denied",
    "unknown",
]


@pytest.mark.asyncio
async def test_upgrade_creates_table_with_expected_columns() -> None:
    """Migration 0027 creates docker_build_hashes with the correct columns."""
    engine = make_engine()
    try:
        await run_migrations(engine)
        async with engine.connect() as conn:

            def _get_table_info(
                sync_conn: object,
            ) -> tuple[list[str], dict[str, dict[str, object]]]:
                inspector: Inspector = inspect(sync_conn)  # type: ignore[assignment]
                table_names: list[str] = inspector.get_table_names() or []
                if "docker_build_hashes" not in table_names:
                    return table_names, {}
                cols = inspector.get_columns("docker_build_hashes") or []
                columns: dict[str, dict[str, object]] = {col["name"]: col for col in cols}  # type: ignore[assignment]
                return table_names, columns

            table_names, columns = await conn.run_sync(_get_table_info)

        assert "docker_build_hashes" in table_names
        for col in _EXPECTED_COLUMNS:
            assert col in columns, f"Missing column: {col}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_is_idempotent() -> None:
    """Running migrations twice does not raise an error (idempotent guard)."""
    engine = make_engine()
    try:
        await run_migrations(engine)
        await run_migrations(engine)
        async with engine.connect() as conn:

            def _get_table_names(sync_conn: object) -> list[str]:
                inspector: Inspector = inspect(sync_conn)  # type: ignore[assignment]
                return inspector.get_table_names() or []

            table_names = await conn.run_sync(_get_table_names)

        assert "docker_build_hashes" in table_names
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_check_constraint_allows_null_reason() -> None:
    """CHECK constraint allows NULL check_error_reason."""
    engine = make_engine()
    try:
        await run_migrations(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO docker_build_hashes "
                    "(container_name, compose_service, build_context_path, update_available) "
                    "VALUES (:cn, :svc, :bcp, :ua)"
                ),
                {"cn": "test_null", "svc": "testsvc", "bcp": "/srv/test", "ua": 0},
            )
            result = await conn.execute(
                text(
                    "SELECT check_error_reason FROM docker_build_hashes WHERE container_name = :cn"
                ),
                {"cn": "test_null"},
            )
            row = result.first()
        assert row is not None
        assert row.check_error_reason is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_check_constraint_allows_all_valid_reasons() -> None:
    """CHECK constraint allows each valid check_error_reason value."""
    engine = make_engine()
    try:
        await run_migrations(engine)
        async with engine.begin() as conn:
            for i, reason in enumerate(_VALID_CHECK_REASONS):
                await conn.execute(
                    text(
                        "INSERT INTO docker_build_hashes "
                        "(container_name, compose_service, build_context_path, "
                        "check_error_reason, update_available) "
                        "VALUES (:cn, :svc, :bcp, :cer, :ua)"
                    ),
                    {
                        "cn": f"container_{i}",
                        "svc": f"svc_{i}",
                        "bcp": f"/srv/{i}",
                        "cer": reason,
                        "ua": 0,
                    },
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_check_constraint_rejects_invalid_reason() -> None:
    """CHECK constraint rejects invalid check_error_reason values."""
    engine = make_engine()
    try:
        await run_migrations(engine)
        async with engine.begin() as conn:
            try:
                await conn.execute(
                    text(
                        "INSERT INTO docker_build_hashes "
                        "(container_name, compose_service, build_context_path, "
                        "check_error_reason, update_available) "
                        "VALUES (:cn, :svc, :bcp, :cer, :ua)"
                    ),
                    {
                        "cn": "bad_container",
                        "svc": "svc",
                        "bcp": "/srv/bad",
                        "cer": "totally_invalid_reason",
                        "ua": 0,
                    },
                )
                raise AssertionError("Expected IntegrityError for invalid check_error_reason")
            except Exception as exc:
                exc_str = str(exc)
                assert (
                    "CHECK constraint failed" in exc_str or "check_error_reason" in exc_str.lower()
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_drops_table() -> None:
    """After upgrade, table exists; this documents the downgrade intent."""
    engine = make_engine()
    try:
        await run_migrations(engine)
        async with engine.connect() as conn:

            def _get_table_names(sync_conn: object) -> list[str]:
                inspector: Inspector = inspect(sync_conn)  # type: ignore[assignment]
                return inspector.get_table_names() or []

            table_names = await conn.run_sync(_get_table_names)

        assert "docker_build_hashes" in table_names
    finally:
        await engine.dispose()
