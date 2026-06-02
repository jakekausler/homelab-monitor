"""Tests for ``hm dev seed-saved-queries`` CLI command (STAGE-004-013).

Project test conventions:
- Framework: pytest-asyncio with async tests
- DB: tempfile-backed SQLite + alembic_upgrade_head (via `repo` fixture from conftest)
- Monkeypatching: get_engine() redirected to test DB via `local_dev_env` fixture
- Dispatch: _handle() dispatch path tested via monkeypatch + argparse.Namespace
"""

from __future__ import annotations

import argparse

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.cli import dev as dev_cli
from homelab_monitor.cli.dev import (
    _cmd_seed_saved_queries,  # pyright: ignore[reportPrivateUsage]
    _handle,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.db.repository import SqliteRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEED_NAMES = [
    "nginx errors (last hour)",
    "auth failures (custom range)",
    "infra services overview",
]


async def _count_saved_queries(repo: SqliteRepository) -> int:
    async with repo.engine.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM log_saved_queries"))
        return int(result.scalar() or 0)


async def _count_by_name(repo: SqliteRepository, name: str) -> int:
    async with repo.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM log_saved_queries WHERE name = :n"),
            {"n": name},
        )
        return int(result.scalar() or 0)


# ---------------------------------------------------------------------------
# Fixture: redirect get_engine() to the test DB
# ---------------------------------------------------------------------------


@pytest.fixture
def local_dev_env(repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    def _engine() -> AsyncEngine:
        return repo.engine

    monkeypatch.setattr("homelab_monitor.cli.dev.get_engine", _engine)


# ---------------------------------------------------------------------------
# Dispatch tests (no DB required)
# ---------------------------------------------------------------------------


class TestHandleDispatch:
    def test_dispatch_seed_saved_queries_routes_to_cmd(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_handle dispatches dev_cmd='seed-saved-queries' to _cmd_seed_saved_queries."""
        called: list[bool] = []

        async def fake_seed(*, clear: bool) -> int:
            called.append(clear)
            return 0

        monkeypatch.setattr(dev_cli, "_cmd_seed_saved_queries", fake_seed)
        args = argparse.Namespace(dev_cmd="seed-saved-queries", clear=False)
        rc = _handle(args)
        assert rc == 0
        assert called == [False]

    def test_dispatch_seed_saved_queries_passes_clear_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_handle passes clear=True when args.clear is True."""
        called: list[bool] = []

        async def fake_seed(*, clear: bool) -> int:
            called.append(clear)
            return 0

        monkeypatch.setattr(dev_cli, "_cmd_seed_saved_queries", fake_seed)
        args = argparse.Namespace(dev_cmd="seed-saved-queries", clear=True)
        rc = _handle(args)
        assert rc == 0
        assert called == [True]


# ---------------------------------------------------------------------------
# Integration tests (use real migrated DB via `repo` fixture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_inserts_3_rows(
    repo: SqliteRepository,
    local_dev_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """seed-saved-queries without --clear inserts exactly 3 rows and returns 0."""
    rc = await _cmd_seed_saved_queries(clear=False)

    assert rc == 0
    assert await _count_saved_queries(repo) == 3  # noqa: PLR2004
    captured = capsys.readouterr()
    assert "seeded 3 saved queries" in captured.out


@pytest.mark.asyncio
async def test_seed_inserts_expected_names(
    repo: SqliteRepository,
    local_dev_env: None,
) -> None:
    """seed-saved-queries inserts rows with the three expected names."""
    await _cmd_seed_saved_queries(clear=False)

    for name in SEED_NAMES:
        assert await _count_by_name(repo, name) == 1, f"missing row for name={name!r}"


@pytest.mark.asyncio
async def test_seed_clear_removes_rows_and_returns_0(
    repo: SqliteRepository,
    local_dev_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """seed-saved-queries --clear deletes seed rows and returns 0, inserts nothing."""
    # Pre-populate so there is something to clear.
    await _cmd_seed_saved_queries(clear=False)
    assert await _count_saved_queries(repo) == 3  # noqa: PLR2004

    rc = await _cmd_seed_saved_queries(clear=True)

    assert rc == 0
    assert await _count_saved_queries(repo) == 0
    captured = capsys.readouterr()
    assert "cleared" in captured.out


@pytest.mark.asyncio
async def test_seed_clear_on_empty_db_returns_0(
    repo: SqliteRepository,
    local_dev_env: None,
) -> None:
    """seed-saved-queries --clear on an already-empty table returns 0 without error."""
    rc = await _cmd_seed_saved_queries(clear=True)

    assert rc == 0
    assert await _count_saved_queries(repo) == 0


@pytest.mark.asyncio
async def test_seed_idempotent_on_rerun(
    repo: SqliteRepository,
    local_dev_env: None,
) -> None:
    """Running seed-saved-queries twice keeps exactly 3 rows (delete-then-insert)."""
    await _cmd_seed_saved_queries(clear=False)
    assert await _count_saved_queries(repo) == 3  # noqa: PLR2004

    await _cmd_seed_saved_queries(clear=False)
    assert await _count_saved_queries(repo) == 3  # noqa: PLR2004
