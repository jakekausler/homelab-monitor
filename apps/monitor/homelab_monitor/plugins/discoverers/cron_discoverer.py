"""STAGE-002-007: cron-discoverer plugin.

In-process Python BaseCollector. Scans /host/etc/crontab, /host/etc/cron.d/*,
and /host/var/spool/cron/crontabs/* on every tick (default 300s), computing
per-line fingerprints and upserting into the `crons` registry.

Self-metrics: piggybacks on `homelab_collector_run_*` emitted by the scheduler
for any BaseCollector. No bespoke metric naming.

Configuration:
- HM_CRON_HOST_ROOT (default `/host`) — root prefix where host files are
  bind-mounted into the container.
- HM_CRON_DISCOVERY_INTERVAL_SECONDS (default 300) — read at module import time
  to override `interval`. The interval is frozen at class definition time (ClassVar).
  For tests that need to vary the interval, monkeypatch `CronDiscoverer.interval`
  directly after class definition, or use environment variable before module import.
- HM_HOST_HOSTNAME — preferred source of the `host` field on each fingerprint.
  Falls back to `socket.gethostname()` with a one-time warning.
"""

from __future__ import annotations

import os
import socket
import time
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

from homelab_monitor.kernel.cron.discovery_types import (
    CronScanError,
    CronScanResult,
    CronSourceKind,
    ParsedCronEntry,
)
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel
from homelab_monitor.plugins.discoverers.cron_parser import parse_cron_file


def _resolve_interval() -> int:
    raw = os.environ.get("HM_CRON_DISCOVERY_INTERVAL_SECONDS", "300")
    try:
        v = int(raw)
        return max(1, v)
    except ValueError:
        return 300


def _resolve_host_root() -> Path:
    return Path(os.environ.get("HM_CRON_HOST_ROOT", "/host"))


_hostname_fallback_warned = False


def _resolve_hostname(log: object) -> str:
    """Return HM_HOST_HOSTNAME if set; otherwise socket.gethostname() (with a one-time warning)."""
    global _hostname_fallback_warned  # noqa: PLW0603
    explicit = os.environ.get("HM_HOST_HOSTNAME", "").strip()
    if explicit:
        return explicit
    fallback = socket.gethostname()
    # structlog log object; use bound logger interface
    if not _hostname_fallback_warned and hasattr(log, "warning"):
        log.warning(  # type: ignore[attr-defined]
            "cron_discoverer.hostname_fallback",
            reason="HM_HOST_HOSTNAME unset; using container hostname",
            fallback=fallback,
        )
        _hostname_fallback_warned = True
    return fallback


class CronDiscoverer(BaseCollector):
    """Discovers crons on the host's filesystem and upserts the registry."""

    name: ClassVar[str] = "cron-discoverer"
    interval: ClassVar[timedelta] = timedelta(seconds=_resolve_interval())
    timeout: ClassVar[timedelta] = timedelta(seconds=60)
    concurrency_group: ClassVar[str] = "discovery"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    # Set by lifespan.py at registration time so the API endpoint can call .scan() directly.
    cron_repo: CronRepo | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Scheduled tick — invokes scan() and returns CollectorResult."""
        start = time.monotonic()
        if self.cron_repo is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["cron_repo not wired"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )
        scan_result = await self.scan(self.cron_repo, log=ctx.log)
        return CollectorResult(
            ok=not scan_result.partial,
            metrics_emitted=0,
            errors=[e.error for e in scan_result.errors],
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    async def scan(self, cron_repo: CronRepo, *, log: object) -> CronScanResult:  # noqa: PLR0912, PLR0915
        """Perform one discovery scan. Idempotent; called by both the scheduler
        tick and POST /api/crons/discover-now.

        Returns CronScanResult — consumed by STAGE-002-007A for soft-delete reconciliation.
        """
        host_root = _resolve_host_root()
        hostname = _resolve_hostname(log)
        now = utc_now_iso()

        all_entries: list[ParsedCronEntry] = []
        all_errors: list[CronScanError] = []
        partial = False

        # 1. /host/etc/crontab (single file)
        crontab_path = host_root / "etc" / "crontab"
        entries, errors, file_partial = self._scan_one_file(
            container_path=crontab_path,
            host_source_path="/etc/crontab",
            source_kind=CronSourceKind.SYSTEM_WITH_USER_FIELD,
            host=hostname,
        )
        all_entries.extend(entries)
        all_errors.extend(errors)
        partial = partial or file_partial

        # 2. /host/etc/cron.d/* (glob; skip dotfiles)
        cron_d = host_root / "etc" / "cron.d"
        if cron_d.is_dir():
            try:
                for child in sorted(cron_d.iterdir()):
                    if child.name.startswith("."):
                        continue
                    if not child.is_file():
                        continue
                    entries, errors, file_partial = self._scan_one_file(
                        container_path=child,
                        host_source_path=f"/etc/cron.d/{child.name}",
                        source_kind=CronSourceKind.SYSTEM_WITH_USER_FIELD,
                        host=hostname,
                    )
                    all_entries.extend(entries)
                    all_errors.extend(errors)
                    partial = partial or file_partial
            except OSError as exc:
                all_errors.append(CronScanError(host_source_path="/etc/cron.d", error=str(exc)))
                partial = True

        # 3. /host/var/spool/cron/crontabs/* (filename = user)
        spool = host_root / "var" / "spool" / "cron" / "crontabs"
        if spool.is_dir():
            try:
                for child in sorted(spool.iterdir()):
                    if child.name.startswith("."):
                        continue
                    if not child.is_file():
                        continue
                    user = child.name
                    entries, errors, file_partial = self._scan_one_file(
                        container_path=child,
                        host_source_path=f"crontab:{user}",
                        source_kind=CronSourceKind.USER_CRONTAB,
                        host=hostname,
                    )
                    all_entries.extend(entries)
                    all_errors.extend(errors)
                    partial = partial or file_partial
            except OSError as exc:
                all_errors.append(
                    CronScanError(host_source_path="/var/spool/cron/crontabs", error=str(exc))
                )
                partial = True

        # 4. Upsert each parsed entry.
        found_fingerprints: set[str] = set()
        inserted_count = 0
        updated_count = 0
        bump_only_count = 0
        for entry in all_entries:
            try:
                record, inserted, updated_non_bump = await cron_repo.upsert_discovered(
                    host=entry.host,
                    source_path=entry.host_source_path,
                    schedule=entry.schedule,
                    command=entry.command,
                    now=now,
                )
                found_fingerprints.add(record.fingerprint)
                if inserted:
                    inserted_count += 1
                elif updated_non_bump:
                    updated_count += 1
                else:
                    bump_only_count += 1
            except Exception as exc:
                all_errors.append(
                    CronScanError(
                        host_source_path=entry.host_source_path, error=f"upsert failed: {exc}"
                    )
                )
                partial = True

        if hasattr(log, "info"):
            log.info(  # type: ignore[attr-defined]
                "cron_discoverer.scan_complete",
                inserted=inserted_count,
                updated=updated_count,
                bump_only=bump_only_count,
                errors=len(all_errors),
                partial=partial,
            )

        return CronScanResult(
            found_fingerprints=frozenset(found_fingerprints),
            partial=partial,
            errors=all_errors,
            inserted_count=inserted_count,
            updated_count=updated_count,
            bump_only_count=bump_only_count,
        )

    def _scan_one_file(
        self,
        *,
        container_path: Path,
        host_source_path: str,
        source_kind: CronSourceKind,
        host: str,
    ) -> tuple[list[ParsedCronEntry], list[CronScanError], bool]:
        """Read one crontab-format file. Returns (entries, errors, partial_flag).

        `partial_flag=True` if a file-level read error OR per-line parser
        error occurred. File-not-found is NOT an error for /etc/crontab —
        missing optional source files are normal.
        """
        if not container_path.exists():
            # Missing optional file is not an error; just empty.
            return [], [], False
        try:
            content = container_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError) as exc:
            return [], [CronScanError(host_source_path=host_source_path, error=str(exc))], True
        entries, errors = parse_cron_file(
            content=content,
            source_kind=source_kind,
            host=host,
            host_source_path=host_source_path,
        )
        return entries, errors, bool(errors)


__all__ = ["CronDiscoverer"]
