"""Tests for PluginLoader.load_subprocess_plugins and persist_to_db."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.loader import PluginLoader


def _write_manifest(plugin_dir: Path, manifest_data: dict[str, Any]) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(manifest_data))
    run_sh = plugin_dir / "run.sh"
    run_sh.write_text('#!/usr/bin/env bash\necho \'{"type":"result","ok":true}\'\n')
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _valid_manifest(name: str = "p1") -> dict[str, Any]:
    return {
        "manifest": 1,
        "name": name,
        "command": ["./run.sh"],
        "interval": "60s",
        "timeout": "5s",
    }


def test_load_subprocess_plugins_walks_dir_and_registers(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "plugin-a", _valid_manifest("plugin-a"))
    _write_manifest(tmp_path / "nested" / "plugin-b", _valid_manifest("plugin-b"))
    loader = PluginLoader()
    count = loader.load_subprocess_plugins(tmp_path)
    assert count == 2  # noqa: PLR2004
    names = {lc.config.name for lc in loader._loaded}  # pyright: ignore[reportPrivateUsage]
    assert names == {"plugin-a", "plugin-b"}


def test_load_subprocess_plugins_skips_invalid_manifests_and_continues(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path / "good", _valid_manifest("good"))
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "plugin.yaml").write_text("not: a valid: yaml: structure: at: all\n")
    loader = PluginLoader()
    count = loader.load_subprocess_plugins(tmp_path)
    assert count == 1


def test_load_subprocess_plugins_returns_count_of_loaded(tmp_path: Path) -> None:
    for i in range(3):
        _write_manifest(tmp_path / f"plug{i}", _valid_manifest(f"plug{i}"))
    loader = PluginLoader()
    assert loader.load_subprocess_plugins(tmp_path) == 3  # noqa: PLR2004


def test_load_subprocess_plugins_no_dir_returns_zero(tmp_path: Path) -> None:
    loader = PluginLoader()
    assert loader.load_subprocess_plugins(tmp_path / "does-not-exist") == 0


@pytest.mark.asyncio
async def test_persist_to_db_inserts_collector_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    repo = SqliteRepository(engine)

    # Bootstrap schema
    async with repo.transaction() as conn:
        await conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS collectors (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                config TEXT,
                created_at TEXT NOT NULL,
                quarantined_at TEXT,
                failure_count INTEGER DEFAULT 0
            )
            """)
        )

    _write_manifest(tmp_path / "plug", _valid_manifest("plug"))
    loader = PluginLoader()
    loader.load_subprocess_plugins(tmp_path)
    await loader.persist_to_db(repo)

    async with repo.transaction() as conn:
        rows = (await conn.execute(text("SELECT name FROM collectors"))).fetchall()
    names = {r[0] for r in rows}
    assert "plug" in names


@pytest.mark.asyncio
async def test_persist_to_db_idempotent_on_repeat(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    repo = SqliteRepository(engine)

    async with repo.transaction() as conn:
        await conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS collectors (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                config TEXT,
                created_at TEXT NOT NULL,
                quarantined_at TEXT,
                failure_count INTEGER DEFAULT 0
            )
            """)
        )

    _write_manifest(tmp_path / "plug", _valid_manifest("plug"))
    loader = PluginLoader()
    loader.load_subprocess_plugins(tmp_path)
    await loader.persist_to_db(repo)
    await loader.persist_to_db(repo)  # second call must not duplicate

    async with repo.transaction() as conn:
        rows = (await conn.execute(text("SELECT name FROM collectors"))).fetchall()
    names = [r[0] for r in rows]
    assert names.count("plug") == 1
