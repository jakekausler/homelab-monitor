"""Tests for cli/collector.py — collector CLI operations."""

from __future__ import annotations

import argparse

import pytest
from sqlalchemy import text

from homelab_monitor.cli.collector import _cmd_unquarantine  # pyright: ignore[reportPrivateUsage]
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import run_migrations
from homelab_monitor.kernel.db.repository import SqliteRepository


async def _seed_quarantined_collector(repo: SqliteRepository, name: str, count: int = 5) -> None:
    """Helper to seed a quarantined collector in the test DB."""
    async with repo.transaction() as conn:
        # Ensure collector row exists
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO collectors (id, name, created_at) "
                "VALUES (:id, :name, '2026-01-01T00:00:00Z')"
            ),
            {"id": f"{name}-id", "name": name},
        )
        # Mark it as quarantined
        await conn.execute(
            text(
                "UPDATE collectors "
                "SET consecutive_failures = :count, "
                "    quarantined_at = '2026-01-01T00:00:00Z', "
                "    quarantine_reason = 'test quarantine' "
                "WHERE name = :name"
            ),
            {"count": count, "name": name},
        )


async def _is_quarantined(repo: SqliteRepository, name: str) -> bool:
    """Helper to check if a collector is quarantined."""
    async with repo.transaction() as conn:
        result = await conn.execute(
            text("SELECT quarantined_at FROM collectors WHERE name = :name"),
            {"name": name},
        )
        row = result.fetchone()
        return row is not None and row[0] is not None


@pytest.mark.asyncio
async def test_cli_collector_unquarantine_all(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hm collector unquarantine (no arg) clears all quarantined collectors."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)

    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    # Seed 2 quarantined collectors
    await _seed_quarantined_collector(repo, "collector-a")
    await _seed_quarantined_collector(repo, "collector-b")

    # Verify both are quarantined
    assert await _is_quarantined(repo, "collector-a")
    assert await _is_quarantined(repo, "collector-b")

    # Run the CLI command with no argument (clear all)
    rc = await _cmd_unquarantine(None)

    # Should succeed
    assert rc == 0

    # Verify both are now clear
    assert not await _is_quarantined(repo, "collector-a")
    assert not await _is_quarantined(repo, "collector-b")


@pytest.mark.asyncio
async def test_cli_collector_unquarantine_specific(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hm collector unquarantine <name> clears only the specified collector."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)

    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    # Seed 2 quarantined collectors
    await _seed_quarantined_collector(repo, "collector-a")
    await _seed_quarantined_collector(repo, "collector-b")

    # Verify both are quarantined
    assert await _is_quarantined(repo, "collector-a")
    assert await _is_quarantined(repo, "collector-b")

    # Run the CLI command with specific collector name
    rc = await _cmd_unquarantine("collector-a")

    # Should succeed
    assert rc == 0

    # Verify only collector-a is clear, collector-b still quarantined
    assert not await _is_quarantined(repo, "collector-a")
    assert await _is_quarantined(repo, "collector-b")


@pytest.mark.asyncio
async def test_cli_collector_unquarantine_not_quarantined(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hm collector unquarantine <name> fails gracefully if collector not quarantined."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)

    engine = get_engine(url=db_url)
    await run_migrations(engine)
    repo = SqliteRepository(engine)

    # Seed a non-quarantined collector
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO collectors (id, name, created_at) "
                "VALUES ('not-quarantined-id', 'not-quarantined', '2026-01-01T00:00:00Z')"
            )
        )

    # Run the CLI command on non-quarantined collector
    rc = await _cmd_unquarantine("not-quarantined")

    # Should return 1 (error)
    assert rc == 1


@pytest.mark.asyncio
async def test_cli_collector_unquarantine_empty_list(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hm collector unquarantine with no quarantined collectors returns 0."""
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)

    engine = get_engine(url=db_url)
    await run_migrations(engine)

    # Don't seed anything; no collectors are quarantined

    # Run the CLI command with no argument (clear all)
    rc = await _cmd_unquarantine(None)

    # Should succeed (idempotent)
    assert rc == 0


class TestHandle:
    def test_dispatches_unquarantine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_handle routes collector_cmd='unquarantine' to _cmd_unquarantine via asyncio.run."""
        from homelab_monitor.cli import collector as collector_cli  # noqa: PLC0415

        called: list[str | None] = []

        async def fake_unquarantine(name: str | None) -> int:
            called.append(name)
            return 0

        monkeypatch.setattr(collector_cli, "_cmd_unquarantine", fake_unquarantine)
        args = argparse.Namespace(collector_cmd="unquarantine", name="cron-discoverer")
        exit_code = collector_cli._handle(args)  # pyright: ignore[reportPrivateUsage]

        assert exit_code == 0
        assert called == ["cron-discoverer"]

    def test_dispatches_unquarantine_no_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_handle with collector_cmd='unquarantine' and no name attr should pass None."""
        from homelab_monitor.cli import collector as collector_cli  # noqa: PLC0415

        called: list[str | None] = []

        async def fake_unquarantine(name: str | None) -> int:
            called.append(name)
            return 0

        monkeypatch.setattr(collector_cli, "_cmd_unquarantine", fake_unquarantine)
        args = argparse.Namespace(collector_cmd="unquarantine")  # no .name attribute
        exit_code = collector_cli._handle(args)  # pyright: ignore[reportPrivateUsage]

        assert exit_code == 0
        assert called == [None]

    def test_missing_subcommand_returns_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_handle without collector_cmd attr should print usage and return 2."""
        from homelab_monitor.cli import collector as collector_cli  # noqa: PLC0415

        args = argparse.Namespace()  # no collector_cmd attribute
        exit_code = collector_cli._handle(args)  # pyright: ignore[reportPrivateUsage]

        assert exit_code == 2  # noqa: PLR2004
        captured = capsys.readouterr()
        assert "usage: hm collector" in captured.err

    def test_unknown_subcommand_returns_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """_handle with unknown collector_cmd should print usage and return 2."""
        from homelab_monitor.cli import collector as collector_cli  # noqa: PLC0415

        args = argparse.Namespace(collector_cmd="nonexistent")
        exit_code = collector_cli._handle(args)  # pyright: ignore[reportPrivateUsage]

        assert exit_code == 2  # noqa: PLR2004
        captured = capsys.readouterr()
        assert "usage: hm collector" in captured.err
