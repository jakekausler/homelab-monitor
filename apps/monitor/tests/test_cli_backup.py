"""Tests for the ``hm backup`` CLI subcommand."""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from homelab_monitor.cli.main import main


def _seed_sqlite(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


def _setup_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot_name: str) -> Path:
    """Configure env vars for the backup CLI to use tmp_path."""
    db_path = tmp_path / "src.sqlite"
    _seed_sqlite(db_path)
    monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://victoriametrics:8428")
    vm_data = tmp_path / "vm-data"
    snap_dir = vm_data / "snapshots" / snapshot_name
    snap_dir.mkdir(parents=True)
    (snap_dir / "part-0001.bin").write_bytes(b"x")
    monkeypatch.setenv("HOMELAB_MONITOR_VM_DATA_DIR", str(vm_data))
    backup_root = tmp_path / "backups"
    monkeypatch.setenv("HOMELAB_MONITOR_BACKUP_ROOT", str(backup_root))
    return backup_root


def _patch_httpx_mock(snapshot_name: str) -> AbstractContextManager[object]:
    """Swap `httpx.AsyncClient` with a MockTransport-backed client."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "snapshot": snapshot_name})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport  # type: ignore[index]
        return real_async_client(*args, **kwargs)  # type: ignore[arg-type]

    return patch("homelab_monitor.cli.backup.httpx.AsyncClient", factory)


def test_backup_run_creates_files(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`hm backup run` produces SQLite + VM files and prints a JSON summary."""
    snapshot_name = "20260508_084813-cli"
    backup_root = _setup_env(monkeypatch, tmp_path, snapshot_name)
    with _patch_httpx_mock(snapshot_name):
        rc = main(["backup", "run"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    summary = json.loads(captured.out)
    assert summary["sqlite_path"]
    assert summary["vm_snapshot_path"]
    assert summary["errors"] == []
    assert (backup_root / Path(summary["sqlite_path"]).name).is_file()


def test_backup_list_prints_existing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`hm backup list` enumerates files in backup_root."""
    backup_root = _setup_env(monkeypatch, tmp_path, "irrelevant")
    backup_root.mkdir(parents=True, exist_ok=True)
    (backup_root / "sqlite-20260101-000000.sqlite").write_bytes(b"")
    (backup_root / "vm").mkdir()
    (backup_root / "vm" / "20260101-000000").mkdir()

    rc = main(["backup", "list"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    listing = json.loads(captured.out)
    assert "sqlite-20260101-000000.sqlite" in listing["sqlite"]
    assert "20260101-000000" in listing["vm"]


def test_backup_retention_deletes_old(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`hm backup retention --keep 1` keeps only the newest."""
    backup_root = _setup_env(monkeypatch, tmp_path, "irrelevant")
    backup_root.mkdir(parents=True, exist_ok=True)
    (backup_root / "sqlite-20260101-000000.sqlite").write_bytes(b"")
    (backup_root / "sqlite-20260102-000000.sqlite").write_bytes(b"")
    (backup_root / "vm").mkdir()

    rc = main(["backup", "retention", "--keep", "1"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    deleted = json.loads(captured.out)
    assert deleted["sqlite"] == 1
    remaining = sorted(p.name for p in backup_root.glob("sqlite-*.sqlite"))
    assert remaining == ["sqlite-20260102-000000.sqlite"]


def test_backup_no_subcommand_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    """`hm backup` with no subcommand exits 2 and prints usage."""
    rc = main(["backup"])
    captured = capsys.readouterr()
    assert rc == 2  # noqa: PLR2004
    assert "usage" in captured.err
