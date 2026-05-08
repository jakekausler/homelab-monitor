"""BackupService: orchestrates SQLite + VictoriaMetrics backups with retention.

Backup strategy (per spec §6.5 + STAGE-001-015A Design):

- SQLite: uses sqlite3.Connection.backup() API (online consistent backup).
  Source DB is opened read-only; target file is created at `backup_root/sqlite-<id>.sqlite`.
  Run in a thread (asyncio.to_thread) since sqlite3 is sync.

- VictoriaMetrics: POST to `{vm_url}/snapshot/create` to ask VM to make a
  hard-linked snapshot inside its own /storage/snapshots/<id>/. Then we copy
  that tree (already hard-linked inside VM's storage) into our own
  `backup_root/vm/<id>/`. Hardlinks across the same filesystem use `cp -al`;
  if VM's data dir and our backup dir are on different filesystems we fall
  back to `cp -r` and log a warning.

- Retention: keep the N newest backups per component (sqlite + vm dirs).

Auto-shrink hook (STAGE-001-015A v1): metric + audit only. See
`apps/monitor/homelab_monitor/plugins/collectors/builtin/self_disk.py`.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.db.time import utc_now_iso


@dataclass(slots=True)
class BackupResult:
    """Result of one BackupService.run_backup() call."""

    snapshot_id: str
    sqlite_path: str | None
    vm_snapshot_path: str | None
    started_at: str
    ended_at: str
    size_bytes: int
    errors: list[str] = field(default_factory=lambda: [])


def _snapshot_id_from_iso(iso: str) -> str:
    """Convert an ISO-8601 UTC timestamp to a filesystem-safe snapshot ID.

    e.g. ``2026-05-08T08:48:12.123456+00:00`` -> ``20260508-084812``.
    """
    # Strip timezone, microseconds, and separators
    base = iso.split("+", 1)[0].split(".", 1)[0]  # 2026-05-08T08:48:12
    return base.replace("-", "").replace(":", "").replace("T", "-")


def _dir_size_bytes(path: Path) -> int:
    """Sum file sizes in path recursively. Returns 0 if path is missing."""
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:  # pragma: no cover -- defensive: race with deletion
                continue
    return total


class BackupService:
    """Orchestrates SQLite + VictoriaMetrics backups with retention.

    Constructor args:
        db_path: filesystem path to the SQLite DB file (the source).
        vm_url: VictoriaMetrics base URL (e.g., http://victoriametrics:8428).
        vm_data_dir: filesystem path where VM stores its data (we read snapshots
            from `vm_data_dir/snapshots/<id>/`); bind-mounted RO into monitor.
        backup_root: filesystem path where backups are written (sqlite-*.sqlite
            files at root, VM trees in `vm/<id>/`).
        http_client: shared `httpx.AsyncClient` from app.state.

    NOTE: db_path may be derived from a `sqlite+aiosqlite:///<path>` URL by the
    lifespan caller — BackupService treats it as an opaque filesystem path.
    """

    def __init__(  # 5 deps is the minimum; further bundling adds indirection
        self,
        db_path: Path,
        vm_url: str,
        vm_data_dir: Path,
        backup_root: Path,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._db_path = Path(db_path)
        self._vm_url = vm_url.rstrip("/")
        self._vm_data_dir = Path(vm_data_dir)
        self._backup_root = Path(backup_root)
        self._http = http_client
        self._log: BoundLogger = structlog.stdlib.get_logger().bind(component="backup")

    async def run_backup(self) -> BackupResult:
        """Run a full SQLite + VM backup. Best-effort: failures collected in `errors`."""
        started_at = utc_now_iso()
        snapshot_id = _snapshot_id_from_iso(started_at)
        errors: list[str] = []

        self._backup_root.mkdir(parents=True, exist_ok=True)
        (self._backup_root / "vm").mkdir(parents=True, exist_ok=True)

        sqlite_path: Path | None = None
        try:
            sqlite_path = await self._backup_sqlite(snapshot_id)
        except Exception as exc:
            errors.append(f"sqlite: {exc}")
            self._log.warning("backup.sqlite_failed", error=str(exc))

        vm_snapshot_path: Path | None = None
        try:
            vm_snapshot_path = await self._backup_vm(snapshot_id)
        except Exception as exc:
            errors.append(f"vm: {exc}")
            self._log.warning("backup.vm_failed", error=str(exc))

        size_bytes = 0
        if sqlite_path is not None and sqlite_path.exists():
            try:
                size_bytes += sqlite_path.stat().st_size
            except OSError as exc:  # pragma: no cover -- defensive: race with deletion
                errors.append(f"sqlite_size: {exc}")
        if vm_snapshot_path is not None and vm_snapshot_path.exists():
            size_bytes += _dir_size_bytes(vm_snapshot_path)

        ended_at = utc_now_iso()
        return BackupResult(
            snapshot_id=snapshot_id,
            sqlite_path=str(sqlite_path) if sqlite_path is not None else None,
            vm_snapshot_path=str(vm_snapshot_path) if vm_snapshot_path is not None else None,
            started_at=started_at,
            ended_at=ended_at,
            size_bytes=size_bytes,
            errors=errors,
        )

    async def _backup_sqlite(self, snapshot_id: str) -> Path:
        """Use sqlite3.Connection.backup() (online API). Runs in a thread."""
        target = self._backup_root / f"sqlite-{snapshot_id}.sqlite"
        await asyncio.to_thread(self._sqlite_backup_sync, self._db_path, target)
        return target

    @staticmethod
    def _sqlite_backup_sync(src: Path, target: Path) -> None:
        """Synchronous helper invoked via asyncio.to_thread; both connections close in finally."""
        src_conn = sqlite3.connect(str(src))
        try:
            dst_conn = sqlite3.connect(str(target))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

    async def _backup_vm(self, snapshot_id: str) -> Path:
        """Ask VM to make a snapshot, then hardlink-copy that snapshot into our backup root."""
        resp = await self._http.post(f"{self._vm_url}/snapshot/create")
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "ok":
            msg = f"VM snapshot/create returned status={body.get('status')!r}"
            raise RuntimeError(msg)
        vm_snapshot_name = body.get("snapshot")
        if not isinstance(vm_snapshot_name, str) or not vm_snapshot_name:
            msg = "VM snapshot/create response missing 'snapshot' field"
            raise RuntimeError(msg)

        src_dir = self._vm_data_dir / "snapshots" / vm_snapshot_name
        if not src_dir.is_dir():
            msg = f"VM snapshot dir not found: {src_dir}"
            raise FileNotFoundError(msg)

        target_dir = self._backup_root / "vm" / snapshot_id
        await asyncio.to_thread(self._copy_tree, src_dir, target_dir)
        return target_dir

    def _copy_tree(self, src_dir: Path, target_dir: Path) -> None:
        """Hardlink-copy if same filesystem; deep copy with warning otherwise."""
        target_dir.mkdir(parents=True, exist_ok=False)
        try:
            same_fs = src_dir.stat().st_dev == target_dir.stat().st_dev
        except OSError:  # pragma: no cover -- both paths exist by construction
            same_fs = False

        if same_fs:
            # `cp -al` = archive + hardlinks. Trailing /. copies CONTENTS into target_dir.
            subprocess.run(
                ["cp", "-al", f"{src_dir}/.", str(target_dir)],
                check=True,
                capture_output=True,
            )
        else:
            self._log.warning(
                "backup.vm_cross_filesystem_fallback",
                src=str(src_dir),
                target=str(target_dir),
            )
            subprocess.run(
                ["cp", "-r", f"{src_dir}/.", str(target_dir)],
                check=True,
                capture_output=True,
            )

    def list_backups(self) -> dict[str, list[str]]:
        """List existing backups: returns {'sqlite': [...], 'vm': [...]}."""
        sqlite_files: list[str] = []
        vm_dirs: list[str] = []
        if self._backup_root.exists():
            for p in sorted(self._backup_root.glob("sqlite-*.sqlite")):
                sqlite_files.append(p.name)
            vm_root = self._backup_root / "vm"
            if vm_root.exists():
                for p in sorted(vm_root.iterdir()):
                    if p.is_dir():
                        vm_dirs.append(p.name)
        return {"sqlite": sqlite_files, "vm": vm_dirs}

    def apply_retention(self, *, keep: int) -> dict[str, int]:
        """Remove all but the N newest backups per component. Returns counts deleted."""
        if keep < 1:
            msg = f"keep must be >= 1, got {keep}"
            raise ValueError(msg)

        deleted = {"sqlite": 0, "vm": 0}
        if not self._backup_root.exists():
            return deleted

        sqlite_files = sorted(
            self._backup_root.glob("sqlite-*.sqlite"),
            key=lambda p: p.name,
            reverse=True,
        )
        for stale in sqlite_files[keep:]:
            stale.unlink(missing_ok=True)
            deleted["sqlite"] += 1

        vm_root = self._backup_root / "vm"
        if vm_root.exists():
            vm_dirs = sorted(
                (p for p in vm_root.iterdir() if p.is_dir()),
                key=lambda p: p.name,
                reverse=True,
            )
            for stale_dir in vm_dirs[keep:]:
                shutil.rmtree(stale_dir, ignore_errors=False)
                deleted["vm"] += 1

        return deleted


__all__ = ["BackupResult", "BackupService"]
