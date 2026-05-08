"""Tests for :class:`BackupService`."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest

from homelab_monitor.kernel.backup.service import (
    BackupService,
    _dir_size_bytes,  # pyright: ignore[reportPrivateUsage]
    _snapshot_id_from_iso,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.db.repository import SqliteRepository


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
async def test_run_backup_creates_sqlite_file(tmp_path: Path, repo: SqliteRepository) -> None:
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
            db=repo,
            db_path=src_db,
            vm_url="http://victoriametrics:8428",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup(who="test")

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
async def test_run_backup_copies_vm_snapshot(tmp_path: Path, repo: SqliteRepository) -> None:
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
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup(who="test")

    assert result.vm_snapshot_path is not None
    assert Path(result.vm_snapshot_path).is_dir()
    assert (Path(result.vm_snapshot_path) / "part-0001.bin").is_file()
    assert (Path(result.vm_snapshot_path) / "metadata.json").is_file()


@pytest.mark.asyncio
async def test_run_backup_collects_vm_failure_in_errors(
    tmp_path: Path, repo: SqliteRepository
) -> None:
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
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup(who="test")

    assert result.sqlite_path is not None  # sqlite succeeded
    assert result.vm_snapshot_path is None  # vm failed
    assert any("vm" in e for e in result.errors)


@pytest.mark.asyncio
async def test_run_backup_vm_status_not_ok(tmp_path: Path, repo: SqliteRepository) -> None:
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
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup(who="test")

    assert result.vm_snapshot_path is None
    assert any("status='fail'" in e or "fail" in e for e in result.errors)


@pytest.mark.asyncio
async def test_run_backup_vm_missing_snapshot_field(tmp_path: Path, repo: SqliteRepository) -> None:
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
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup(who="test")

    assert result.vm_snapshot_path is None
    assert any("snapshot" in e for e in result.errors)


@pytest.mark.asyncio
async def test_run_backup_vm_snapshot_dir_missing(tmp_path: Path, repo: SqliteRepository) -> None:
    """VM reports a snapshot but the dir does not exist on disk."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"
    transport = _vm_handler_factory("does-not-exist")

    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        service = BackupService(
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup(who="test")

    assert result.vm_snapshot_path is None
    assert any("not found" in e or "snapshot" in e for e in result.errors)


async def test_apply_retention_keeps_n_newest_sqlite(
    tmp_path: Path, repo: SqliteRepository
) -> None:
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

    async with httpx.AsyncClient() as client:
        service = BackupService(
            db=repo,
            db_path=Path("/dev/null"),
            vm_url="http://x",
            vm_data_dir=tmp_path / "vm",
            backup_root=backup_root,
            http_client=client,
        )
        deleted = await service.apply_retention(keep=2, who="test")
        assert deleted["sqlite"] == 3  # noqa: PLR2004
        remaining = sorted(p.name for p in backup_root.glob("sqlite-*.sqlite"))
        assert remaining == ["sqlite-20260104-000000.sqlite", "sqlite-20260105-000000.sqlite"]


async def test_apply_retention_keeps_n_newest_vm(tmp_path: Path, repo: SqliteRepository) -> None:
    backup_root = tmp_path / "b"
    backup_root.mkdir()
    vm_dir = backup_root / "vm"
    vm_dir.mkdir()
    for ts in ("20260101-000000", "20260102-000000", "20260103-000000"):
        d = vm_dir / ts
        d.mkdir()
        (d / "x").write_bytes(b"")

    async with httpx.AsyncClient() as client:
        service = BackupService(
            db=repo,
            db_path=Path("/dev/null"),
            vm_url="http://x",
            vm_data_dir=tmp_path / "vm",
            backup_root=backup_root,
            http_client=client,
        )
        deleted = await service.apply_retention(keep=1, who="test")
        assert deleted["vm"] == 2  # noqa: PLR2004
        remaining = sorted(p.name for p in vm_dir.iterdir())
        assert remaining == ["20260103-000000"]


async def test_apply_retention_rejects_zero(tmp_path: Path, repo: SqliteRepository) -> None:
    async with httpx.AsyncClient() as client:
        service = BackupService(
            db=repo,
            db_path=Path("/dev/null"),
            vm_url="http://x",
            vm_data_dir=tmp_path / "vm",
            backup_root=tmp_path / "b",
            http_client=client,
        )
        with pytest.raises(ValueError, match="keep must be"):
            await service.apply_retention(keep=0, who="test")


@pytest.mark.asyncio
async def test_run_backup_collects_sqlite_failure_in_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: SqliteRepository,
) -> None:
    """SQLite backup failure is captured in result.errors with 'sqlite:' prefix."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"
    snapshot_name = "20260508_084812-fail"
    _make_vm_snapshot_dir(vm_data_dir, snapshot_name)
    transport = _vm_handler_factory(snapshot_name)

    def _fake_to_thread(fn: object, *args: object, **kwargs: object) -> object:
        return _raise_runtime_error()

    monkeypatch.setattr(
        "homelab_monitor.kernel.backup.service.asyncio.to_thread",
        _fake_to_thread,
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        service = BackupService(
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup(who="test")

    assert any(e.startswith("sqlite:") for e in result.errors)


async def _raise_runtime_error() -> None:
    raise RuntimeError("disk exploded")


@pytest.mark.asyncio
async def test_backup_sqlite_aborts_on_low_disk(
    tmp_path: Path,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_backup_sqlite raises RuntimeError with 'insufficient disk space' when free space is low."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    class _DiskUsage(NamedTuple):
        total: int
        used: int
        free: int

    def _fake_disk_usage(path: object) -> _DiskUsage:
        return _DiskUsage(total=1000, used=999, free=1)

    monkeypatch.setattr(
        "homelab_monitor.kernel.backup.service.shutil.disk_usage",
        _fake_disk_usage,
    )

    async with httpx.AsyncClient() as c:
        service = BackupService(
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=tmp_path / "vm",
            backup_root=backup_root,
            http_client=c,
        )
        with pytest.raises(RuntimeError, match="insufficient disk space"):
            await service._backup_sqlite("20260508-093000")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_backup_sqlite_cleans_partial_file_on_failure(
    tmp_path: Path,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial sqlite backup file is removed when _sqlite_backup_sync raises."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    snapshot_id = "20260508-093000"
    target_path = backup_root / f"sqlite-{snapshot_id}.sqlite"

    def failing_sync(src: Path, target: Path) -> None:
        # Simulate partial write then failure
        target.write_bytes(b"partial")
        raise RuntimeError("write failed mid-way")

    monkeypatch.setattr(
        "homelab_monitor.kernel.backup.service.BackupService._sqlite_backup_sync",
        staticmethod(failing_sync),
    )

    async with httpx.AsyncClient() as c:
        service = BackupService(
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=tmp_path / "vm",
            backup_root=backup_root,
            http_client=c,
        )
        with pytest.raises(RuntimeError, match="write failed"):
            await service._backup_sqlite(snapshot_id)  # pyright: ignore[reportPrivateUsage]

    assert not target_path.exists()


@pytest.mark.asyncio
async def test_run_backup_rejects_malicious_vm_snapshot_name(
    tmp_path: Path,
    repo: SqliteRepository,
) -> None:
    """VM snapshot name with invalid characters (path traversal) is captured in errors."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "snapshot": "../etc/passwd"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        service = BackupService(
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup(who="test")

    assert any("vm:" in e and "invalid characters" in e for e in result.errors)


@pytest.mark.asyncio
async def test_copy_tree_handles_cp_al_failure(
    tmp_path: Path,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_copy_tree raises RuntimeError with 'cp failed' when cp -al exits non-zero."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "file.bin").write_bytes(b"data")
    target_dir = tmp_path / "target"

    original_run = subprocess.run

    def patched_run(cmd: list[str], **kwargs: object) -> object:  # type: ignore[return]
        if "-al" in cmd:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=cmd,
                stderr="permission denied",
            )
        return original_run(cmd, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("homelab_monitor.kernel.backup.service.subprocess.run", patched_run)

    async with httpx.AsyncClient() as c:
        service = BackupService(
            db=repo,
            db_path=tmp_path / "db.sqlite",
            vm_url="http://x",
            vm_data_dir=tmp_path / "vm",
            backup_root=tmp_path / "backups",
            http_client=c,
        )
        with pytest.raises(RuntimeError, match="cp failed"):
            service._copy_tree(src_dir, target_dir)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_copy_tree_falls_back_to_cp_r_on_cross_filesystem(
    tmp_path: Path,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_copy_tree uses cp -r (not cp -al) when src and target are on different filesystems."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "file.bin").write_bytes(b"data")
    target_dir = tmp_path / "target"

    # Make stat() return different st_dev values to simulate cross-fs scenario
    real_stat = Path.stat

    call_count = {"n": 0}

    def fake_stat(self: Path, *args: object, **kwargs: object) -> object:
        result = real_stat(self)
        call_count["n"] += 1
        # src_dir gets dev=1, target_dir.parent gets dev=2
        if self == src_dir:
            return os.stat_result(
                (  # pyright: ignore[reportReturnType]
                    result.st_mode,
                    result.st_ino,
                    1,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        if self == target_dir.parent:
            return os.stat_result(
                (  # pyright: ignore[reportReturnType]
                    result.st_mode,
                    result.st_ino,
                    2,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)

    invoked_cmds: list[list[str]] = []
    _real_subprocess_run = subprocess.run

    def capturing_run(cmd: list[str], **kwargs: object) -> object:
        invoked_cmds.append(cmd)
        # Actually run cp -r so target_dir gets created properly
        check = kwargs.pop("check", False)
        return _real_subprocess_run(cmd, check=check, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("homelab_monitor.kernel.backup.service.subprocess.run", capturing_run)

    async with httpx.AsyncClient() as c:
        service = BackupService(
            db=repo,
            db_path=tmp_path / "db.sqlite",
            vm_url="http://x",
            vm_data_dir=tmp_path / "vm",
            backup_root=tmp_path / "backups",
            http_client=c,
        )
        service._copy_tree(src_dir, target_dir)  # pyright: ignore[reportPrivateUsage]

    assert invoked_cmds, "subprocess.run was never called"
    assert invoked_cmds[0][1] == "-r", f"Expected cp -r fallback, got: {invoked_cmds[0]}"
    assert "-al" not in invoked_cmds[0]


@pytest.mark.asyncio
async def test_apply_retention_returns_zero_when_backup_root_missing(
    tmp_path: Path,
    repo: SqliteRepository,
) -> None:
    """apply_retention returns {'sqlite': 0, 'vm': 0} when backup_root does not exist."""
    missing_root = tmp_path / "does-not-exist"

    async with httpx.AsyncClient() as c:
        service = BackupService(
            db=repo,
            db_path=Path("/dev/null"),
            vm_url="http://x",
            vm_data_dir=tmp_path / "vm",
            backup_root=missing_root,
            http_client=c,
        )
        deleted = await service.apply_retention(keep=7, who="test")

    assert deleted == {"sqlite": 0, "vm": 0}


def test_list_backups_empty_root(tmp_path: Path, repo: SqliteRepository) -> None:
    async def async_test() -> None:
        async with httpx.AsyncClient() as client:
            service = BackupService(
                db=repo,
                db_path=Path("/dev/null"),
                vm_url="http://x",
                vm_data_dir=tmp_path / "vm",
                backup_root=tmp_path / "missing",
                http_client=client,
            )
            listing = service.list_backups()
            assert listing == {"sqlite": [], "vm": []}

    asyncio.run(async_test())


@pytest.mark.asyncio
async def test_run_backup_audit_requested_failure_is_captured(
    tmp_path: Path,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit insert failure before backup is captured with 'audit_requested:' prefix."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"
    snapshot_name = "20260508_084812-auditfail"
    _make_vm_snapshot_dir(vm_data_dir, snapshot_name)
    transport = _vm_handler_factory(snapshot_name)

    call_count = {"n": 0}
    real_insert = None

    async def _failing_insert(*args: object, **kwargs: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("db locked")
        if real_insert is not None:
            await real_insert(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    import homelab_monitor.kernel.backup.service as backup_service_mod  # noqa: PLC0415

    real_insert = backup_service_mod.insert_audit
    monkeypatch.setattr(backup_service_mod, "insert_audit", _failing_insert)

    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        service = BackupService(
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup(who="test")

    assert any(e.startswith("audit_requested:") for e in result.errors)


@pytest.mark.asyncio
async def test_run_backup_audit_completed_failure_is_logged_only(
    tmp_path: Path,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit insert failure after backup is only logged, not added to result.errors."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"
    snapshot_name = "20260508_084812-auditfail2"
    _make_vm_snapshot_dir(vm_data_dir, snapshot_name)
    transport = _vm_handler_factory(snapshot_name)

    call_count = {"n": 0}
    real_insert = None

    async def _failing_on_second(*args: object, **kwargs: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:  # second call = completed audit  # noqa: PLR2004
            raise RuntimeError("db gone")
        if real_insert is not None:
            await real_insert(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    import homelab_monitor.kernel.backup.service as backup_service_mod  # noqa: PLC0415

    real_insert = backup_service_mod.insert_audit
    monkeypatch.setattr(backup_service_mod, "insert_audit", _failing_on_second)

    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        service = BackupService(
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup(who="test")

    # The backup itself should succeed; audit failure must NOT appear in errors
    assert not any(e.startswith("audit_") for e in result.errors)


@pytest.mark.asyncio
async def test_backup_vm_subprocess_called_process_error(
    tmp_path: Path,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CalledProcessError from cp in _copy_tree is reported with 'vm:' prefix in result.errors."""
    src_db = tmp_path / "src.sqlite"
    _make_seed_db(src_db)
    vm_data_dir = tmp_path / "vm"
    vm_data_dir.mkdir()
    backup_root = tmp_path / "backups"
    snapshot_name = "20260508_084812-cpfail"
    _make_vm_snapshot_dir(vm_data_dir, snapshot_name)
    transport = _vm_handler_factory(snapshot_name)

    def _failing_run(cmd: list[str], **kwargs: object) -> object:
        raise subprocess.CalledProcessError(1, cmd, stderr=b"permission denied")

    monkeypatch.setattr("homelab_monitor.kernel.backup.service.subprocess.run", _failing_run)

    async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
        service = BackupService(
            db=repo,
            db_path=src_db,
            vm_url="http://x",
            vm_data_dir=vm_data_dir,
            backup_root=backup_root,
            http_client=c,
        )
        result = await service.run_backup(who="test")

    assert any(e.startswith("vm:") for e in result.errors)


@pytest.mark.asyncio
async def test_apply_retention_with_vm_backups(
    tmp_path: Path,
    repo: SqliteRepository,
) -> None:
    """apply_retention removes older vm snapshot dirs, keeping only the newest N."""
    backup_root = tmp_path / "b"
    backup_root.mkdir()
    vm_dir = backup_root / "vm"
    vm_dir.mkdir()
    for ts in ("20260101-000000", "20260102-000000", "20260103-000000"):
        d = vm_dir / ts
        d.mkdir()
        (d / "data").write_bytes(b"x")

    async with httpx.AsyncClient() as c:
        service = BackupService(
            db=repo,
            db_path=Path("/dev/null"),
            vm_url="http://x",
            vm_data_dir=tmp_path / "vm",
            backup_root=backup_root,
            http_client=c,
        )
        deleted = await service.apply_retention(keep=1, who="test")

    assert deleted["vm"] == 2  # noqa: PLR2004
    remaining = [p.name for p in vm_dir.iterdir() if p.is_dir()]
    assert remaining == ["20260103-000000"]


@pytest.mark.asyncio
async def test_apply_retention_audit_exception_is_swallowed(
    tmp_path: Path,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit insert failure during apply_retention is swallowed; still returns deleted dict."""
    backup_root = tmp_path / "b"
    backup_root.mkdir()
    (backup_root / "vm").mkdir()
    (backup_root / "sqlite-20260101-000000.sqlite").write_bytes(b"")

    import homelab_monitor.kernel.backup.service as backup_service_mod  # noqa: PLC0415

    async def _always_raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit table gone")

    monkeypatch.setattr(backup_service_mod, "insert_audit", _always_raise)

    async with httpx.AsyncClient() as c:
        service = BackupService(
            db=repo,
            db_path=Path("/dev/null"),
            vm_url="http://x",
            vm_data_dir=tmp_path / "vm",
            backup_root=backup_root,
            http_client=c,
        )
        deleted = await service.apply_retention(keep=5, who="test")

    assert isinstance(deleted, dict)
    assert "sqlite" in deleted
    assert "vm" in deleted


def test_list_backups_with_no_vm_root(tmp_path: Path, repo: SqliteRepository) -> None:
    """list_backups returns empty vm list when backup_root/vm/ doesn't exist."""
    backup_root = tmp_path / "b"
    backup_root.mkdir()
    (backup_root / "sqlite-20260101-000000.sqlite").write_bytes(b"")
    # No vm/ subdir created

    async def async_test() -> None:
        async with httpx.AsyncClient() as client:
            service = BackupService(
                db=repo,
                db_path=Path("/dev/null"),
                vm_url="http://x",
                vm_data_dir=tmp_path / "vm",
                backup_root=backup_root,
                http_client=client,
            )
            listing = service.list_backups()
            assert listing["vm"] == []
            assert listing["sqlite"] == ["sqlite-20260101-000000.sqlite"]

    asyncio.run(async_test())


def test_list_backups_skips_non_dir_entries_in_vm(tmp_path: Path, repo: SqliteRepository) -> None:
    """list_backups skips regular files inside backup_root/vm/."""
    backup_root = tmp_path / "b"
    backup_root.mkdir()
    vm_root = backup_root / "vm"
    vm_root.mkdir()
    (vm_root / ".placeholder").write_bytes(b"")  # regular file, not a dir
    (vm_root / "20260101-000000").mkdir()

    async def async_test() -> None:
        async with httpx.AsyncClient() as client:
            service = BackupService(
                db=repo,
                db_path=Path("/dev/null"),
                vm_url="http://x",
                vm_data_dir=tmp_path / "vm",
                backup_root=backup_root,
                http_client=client,
            )
            listing = service.list_backups()
            assert listing["vm"] == ["20260101-000000"]

    asyncio.run(async_test())


@pytest.mark.asyncio
async def test_apply_retention_with_no_vm_root(tmp_path: Path, repo: SqliteRepository) -> None:
    """apply_retention succeeds when vm/ subdir doesn't exist; deleted['vm'] == 0."""
    backup_root = tmp_path / "b"
    backup_root.mkdir()
    # Two sqlite files, no vm/ dir
    (backup_root / "sqlite-20260101-000000.sqlite").write_bytes(b"")
    (backup_root / "sqlite-20260102-000000.sqlite").write_bytes(b"")

    async with httpx.AsyncClient() as c:
        service = BackupService(
            db=repo,
            db_path=Path("/dev/null"),
            vm_url="http://x",
            vm_data_dir=tmp_path / "vm",
            backup_root=backup_root,
            http_client=c,
        )
        deleted = await service.apply_retention(keep=1, who="test")

    assert deleted["vm"] == 0
    assert deleted["sqlite"] == 1


def test_copy_tree_cross_filesystem_called_process_error(
    tmp_path: Path,
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CalledProcessError on the cross-filesystem cp -r path raises RuntimeError."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "file.bin").write_bytes(b"x")
    target_dir = tmp_path / "target"

    # Patch subprocess.run to always raise CalledProcessError.
    def _failing_run(cmd: list[str], **kwargs: object) -> object:
        raise subprocess.CalledProcessError(1, cmd, stderr="no space left on device")

    monkeypatch.setattr("homelab_monitor.kernel.backup.service.subprocess.run", _failing_run)

    # Force same_fs=False by making Path.stat return different st_dev values
    # ONLY for src_dir and target_dir.parent. All other paths fall through to
    # the real stat so internal pathlib calls (e.g. mkdir's is_dir check) work.
    _real_stat = Path.stat

    def _mock_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        real = _real_stat(self, follow_symlinks=follow_symlinks)
        if self == src_dir:
            return os.stat_result(
                (
                    real.st_mode,
                    real.st_ino,
                    10,
                    real.st_nlink,
                    real.st_uid,
                    real.st_gid,
                    real.st_size,
                    real.st_atime,
                    real.st_mtime,
                    real.st_ctime,
                )
            )
        if self == target_dir.parent:
            return os.stat_result(
                (
                    real.st_mode,
                    real.st_ino,
                    99,
                    real.st_nlink,
                    real.st_uid,
                    real.st_gid,
                    real.st_size,
                    real.st_atime,
                    real.st_mtime,
                    real.st_ctime,
                )
            )
        return real

    monkeypatch.setattr(Path, "stat", _mock_stat)

    async def _run() -> None:
        async with httpx.AsyncClient() as c:
            service = BackupService(
                db=repo,
                db_path=Path("/dev/null"),
                vm_url="http://x",
                vm_data_dir=tmp_path / "vm",
                backup_root=tmp_path / "backups",
                http_client=c,
            )
            with pytest.raises(RuntimeError, match="cp failed"):
                service._copy_tree(src_dir, target_dir)  # pyright: ignore[reportPrivateUsage]

    asyncio.run(_run())


def test_list_backups_lists_existing(tmp_path: Path, repo: SqliteRepository) -> None:
    backup_root = tmp_path / "b"
    backup_root.mkdir()
    (backup_root / "sqlite-20260101-000000.sqlite").write_bytes(b"")
    (backup_root / "vm").mkdir()
    (backup_root / "vm" / "20260101-000000").mkdir()
    (backup_root / "vm" / "20260101-000000" / "x").write_bytes(b"")

    async def async_test() -> None:
        async with httpx.AsyncClient() as client:
            service = BackupService(
                db=repo,
                db_path=Path("/dev/null"),
                vm_url="http://x",
                vm_data_dir=tmp_path / "vm",
                backup_root=backup_root,
                http_client=client,
            )
            listing = service.list_backups()
            assert listing == {
                "sqlite": ["sqlite-20260101-000000.sqlite"],
                "vm": ["20260101-000000"],
            }

    asyncio.run(async_test())
