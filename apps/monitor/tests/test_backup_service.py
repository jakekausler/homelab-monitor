"""Tests for :class:`BackupService`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from homelab_monitor.kernel.backup.service import (
    BackupService,
    _dir_size_bytes,  # pyright: ignore[reportPrivateUsage]
    _snapshot_id_from_iso,  # pyright: ignore[reportPrivateUsage]
)


def _make_seed_db(path: Path) -> None:
    """Create a tiny SQLite DB at `path` with one row."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO t (val) VALUES ('hello')")
        conn.commit()
    finally:
        conn.close()


def _make_vm_snapshot_dir(vm_data_dir: Path, name: str) -> Path:
    """Create a fake VM snapshot directory tree under `vm_data_dir/snapshots/<name>/`."""
    target = vm_data_dir / "snapshots" / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "part-0001.bin").write_bytes(b"x" * 128)
    (target / "metadata.json").write_text("{}")
    return target


def _vm_handler_factory(snapshot_name: str) -> httpx.MockTransport:
    """httpx.MockTransport that returns a successful snapshot/create response."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/snapshot/create"
        return httpx.Response(200, json={"status": "ok", "snapshot": snapshot_name})

    return httpx.MockTransport(handler)


def test_snapshot_id_from_iso_strips_separators() -> None:
    """ISO timestamp is converted to YYYYMMDD-HHMMSS form."""
    assert _snapshot_id_from_iso("2026-05-08T08:48:12.123456+00:00") == "20260508-084812"


def test_snapshot_id_from_iso_no_microseconds() -> None:
    assert _snapshot_id_from_iso("2026-05-08T08:48:12+00:00") == "20260508-084812"


def test_dir_size_bytes_missing_path_returns_zero(tmp_path: Path) -> None:
    assert _dir_size_bytes(tmp_path / "does-not-exist") == 0


def test_dir_size_bytes_sums_file_sizes(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x" * 10)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b").write_bytes(b"y" * 20)
    assert _dir_size_bytes(tmp_path) == 30  # noqa: PLR2004


@pytest.mark.asyncio
async def test_run_backup_creates_sqlite_file(tmp_path: Path) -> None:
    """run_backup() produces an SQLite copy with the same data."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"
    snapshot_name = "20260508_084812-12345"
    _make_vm_snapshot_dir(vm_data_dir, snapshot_name)
    transport = _vm_handler_factory(snapshot_name)

    async with httpx.AsyncClient(transport=transport, base_url="http://victoriametrics:8428") as c:
        service = BackupService(
            db_path=src_db,
            vm_url="http://victoriametrics:8428",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup()

    assert result.sqlite_path is not None
    assert Path(result.sqlite_path).is_file()
    # Verify the backup is a working SQLite file containing our row
    conn = sqlite3.connect(result.sqlite_path)
    try:
        rows = list(conn.execute("SELECT val FROM t"))
        assert rows == [("hello",)]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_run_backup_copies_vm_snapshot(tmp_path: Path) -> None:
    """VM snapshot dir is copied (or hardlinked) into backup_root/vm/<id>/."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"
    snapshot_name = "20260508_084812-99"
    _make_vm_snapshot_dir(vm_data_dir, snapshot_name)
    transport = _vm_handler_factory(snapshot_name)

    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        service = BackupService(
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup()

    assert result.vm_snapshot_path is not None
    assert Path(result.vm_snapshot_path).is_dir()
    assert (Path(result.vm_snapshot_path) / "part-0001.bin").is_file()
    assert (Path(result.vm_snapshot_path) / "metadata.json").is_file()


@pytest.mark.asyncio
async def test_run_backup_collects_vm_failure_in_errors(tmp_path: Path) -> None:
    """VM HTTP failure produces an entry in `errors`, sqlite still succeeds."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"status": "error"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        service = BackupService(
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup()

    assert result.sqlite_path is not None  # sqlite succeeded
    assert result.vm_snapshot_path is None  # vm failed
    assert any("vm" in e for e in result.errors)


@pytest.mark.asyncio
async def test_run_backup_vm_status_not_ok(tmp_path: Path) -> None:
    """VM returning 200 with status != 'ok' is treated as an error."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "fail"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        service = BackupService(
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup()

    assert result.vm_snapshot_path is None
    assert any("status='fail'" in e or "fail" in e for e in result.errors)


@pytest.mark.asyncio
async def test_run_backup_vm_missing_snapshot_field(tmp_path: Path) -> None:
    """VM returning ok without a snapshot id raises and is reported."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})  # missing 'snapshot'

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        service = BackupService(
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup()

    assert result.vm_snapshot_path is None
    assert any("snapshot" in e for e in result.errors)


@pytest.mark.asyncio
async def test_run_backup_vm_snapshot_dir_missing(tmp_path: Path) -> None:
    """VM reports a snapshot but the dir does not exist on disk."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"
    transport = _vm_handler_factory("does-not-exist")

    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        service = BackupService(
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup()

    assert result.vm_snapshot_path is None
    assert any("not found" in e or "snapshot" in e for e in result.errors)


def test_apply_retention_keeps_n_newest_sqlite(tmp_path: Path) -> None:
    backup_root = tmp_path / "b"
    backup_root.mkdir()
    (backup_root / "vm").mkdir()
    # Create 5 sqlite files; names sort by suffix
    for ts in (
        "20260101-000000",
        "20260102-000000",
        "20260103-000000",
        "20260104-000000",
        "20260105-000000",
    ):
        (backup_root / f"sqlite-{ts}.sqlite").write_bytes(b"")

    service = BackupService(
        db_path=Path("/dev/null"),
        vm_url="http://x",
        vm_data_dir=tmp_path / "vm",
        backup_root=backup_root,
        http_client=httpx.AsyncClient(),
    )
    deleted = service.apply_retention(keep=2)
    assert deleted["sqlite"] == 3  # noqa: PLR2004
    remaining = sorted(p.name for p in backup_root.glob("sqlite-*.sqlite"))
    assert remaining == ["sqlite-20260104-000000.sqlite", "sqlite-20260105-000000.sqlite"]


def test_apply_retention_keeps_n_newest_vm(tmp_path: Path) -> None:
    backup_root = tmp_path / "b"
    backup_root.mkdir()
    vm_dir = backup_root / "vm"
    vm_dir.mkdir()
    for ts in ("20260101-000000", "20260102-000000", "20260103-000000"):
        d = vm_dir / ts
        d.mkdir()
        (d / "x").write_bytes(b"")

    service = BackupService(
        db_path=Path("/dev/null"),
        vm_url="http://x",
        vm_data_dir=tmp_path / "vm",
        backup_root=backup_root,
        http_client=httpx.AsyncClient(),
    )
    deleted = service.apply_retention(keep=1)
    assert deleted["vm"] == 2  # noqa: PLR2004
    remaining = sorted(p.name for p in vm_dir.iterdir())
    assert remaining == ["20260103-000000"]


def test_apply_retention_rejects_zero(tmp_path: Path) -> None:
    service = BackupService(
        db_path=Path("/dev/null"),
        vm_url="http://x",
        vm_data_dir=tmp_path / "vm",
        backup_root=tmp_path / "b",
        http_client=httpx.AsyncClient(),
    )
    with pytest.raises(ValueError, match="keep must be"):
        service.apply_retention(keep=0)


def test_list_backups_empty_root(tmp_path: Path) -> None:
    service = BackupService(
        db_path=Path("/dev/null"),
        vm_url="http://x",
        vm_data_dir=tmp_path / "vm",
        backup_root=tmp_path / "missing",
        http_client=httpx.AsyncClient(),
    )
    listing = service.list_backups()
    assert listing == {"sqlite": [], "vm": []}


def test_list_backups_lists_existing(tmp_path: Path) -> None:
    backup_root = tmp_path / "b"
    backup_root.mkdir()
    (backup_root / "sqlite-20260101-000000.sqlite").write_bytes(b"")
    (backup_root / "vm").mkdir()
    (backup_root / "vm" / "20260101-000000").mkdir()
    (backup_root / "vm" / "20260101-000000" / "x").write_bytes(b"")

    service = BackupService(
        db_path=Path("/dev/null"),
        vm_url="http://x",
        vm_data_dir=tmp_path / "vm",
        backup_root=backup_root,
        http_client=httpx.AsyncClient(),
    )
    listing = service.list_backups()
    assert listing == {
        "sqlite": ["sqlite-20260101-000000.sqlite"],
        "vm": ["20260101-000000"],
    }
