"""STAGE-002-007: cron-discoverer plugin.

In-process Python BaseCollector. Scans /host/etc/crontab, /host/etc/cron.d/*,
and the crontab-snapshot directory on every tick (default 300s), computing
per-line fingerprints and upserting into the `crons` registry.

Self-metrics: piggybacks on `homelab_collector_run_*` emitted by the scheduler
for any BaseCollector. No bespoke metric naming.

Configuration:
- HM_CRON_HOST_ROOT (default `/host`) — root prefix where host files are
  bind-mounted into the container.
- HM_CRON_SNAPSHOT_DIR (default `/host-crontab-snapshot`) — container path of
  the host crontab-snapshot directory (Option B fix, STAGE-002-009). The host
  script hm-crontab-snapshot writes one file per user here (filename = username,
  content = that user's raw `crontab -l` output). The discoverer reads the
  snapshot instead of the 0600 spool files (which keep their cron-required mode
  permanently unmodified).
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


def resolve_snapshot_dir() -> Path:
    """Container path of the host crontab-snapshot directory (Option B fix).

    The host script `hm-crontab-snapshot` writes one file per user (filename =
    username) containing that user's raw `crontab -l` output. The host snapshot
    dir `/var/lib/homelab-monitor/crontab-snapshot` is bind-mounted into the
    container at this path. Default `/host-crontab-snapshot`.
    """
    return Path(os.environ.get("HM_CRON_SNAPSHOT_DIR", "/host-crontab-snapshot"))


_hostname_fallback_warned = False


def resolve_hostname() -> str:
    """Return HM_HOST_HOSTNAME if set+non-empty, else socket.gethostname().
    Pure (no logging) — for callers that just need the value."""
    explicit = os.environ.get("HM_HOST_HOSTNAME", "").strip()
    return explicit or socket.gethostname()


def _resolve_hostname(log: object) -> str:
    """Return HM_HOST_HOSTNAME if set; otherwise socket.gethostname() (with a one-time warning)."""
    global _hostname_fallback_warned  # noqa: PLW0603
    hostname = resolve_hostname()
    # structlog log object; use bound logger interface
    if not _hostname_fallback_warned and not os.environ.get("HM_HOST_HOSTNAME", "").strip():
        if hasattr(log, "warning"):
            log.warning(  # type: ignore[attr-defined]
                "cron_discoverer.hostname_fallback",
                reason="HM_HOST_HOSTNAME unset; using container hostname",
                fallback=hostname,
            )
        _hostname_fallback_warned = True
    return hostname


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
        try:
            soft_deleted, restored = await self.cron_repo.reconcile_soft_deletes(
                host=scan_result.host,
                clean_paths=scan_result.clean_source_paths,
                found_by_path=scan_result.found_by_source_path,
                now=utc_now_iso(),
            )
        except Exception as exc:
            soft_deleted, restored = 0, 0
            if hasattr(ctx.log, "warning"):
                ctx.log.warning(  # type: ignore[attr-defined]
                    "cron_discoverer.reconcile_failed", error=str(exc)
                )
        if hasattr(ctx.log, "info"):
            ctx.log.info(  # type: ignore[attr-defined]
                "cron_discoverer.reconcile_complete",
                soft_deleted=soft_deleted,
                restored=restored,
            )
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

        Returns CronScanResult — consumed by STAGE-002-007A for soft-delete
        reconciliation. The caller (run() or the discover-now endpoint) is
        responsible for invoking cron_repo.reconcile_soft_deletes() with the
        per-source-file fields on the result.
        """
        host_root = _resolve_host_root()
        hostname = _resolve_hostname(log)
        now = utc_now_iso()

        all_entries: list[ParsedCronEntry] = []
        all_errors: list[CronScanError] = []
        partial = False
        clean_source_paths: set[str] = set()
        unreachable_prefixes: set[str] = set()
        # Paths that did NOT cleanly inspect this scan — whether the file-level
        # read failed (PermissionError / OSError) OR the file had a per-line
        # parse error. DISTINCT from unreachable_prefixes (whole-directory
        # iterdir failures). Such a path must NEVER be in clean_source_paths;
        # if it were, reconciliation would soft-delete its DB rows on an
        # incomplete observation (STAGE-002-007A data-corruption bugfix).
        unreadable_paths: set[str] = set()

        # 1. /host/etc/crontab (single file). The host path is always "known":
        #    if the file is absent it is still a clean (empty) inspection.
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
        if not errors:
            clean_source_paths.add("/etc/crontab")
        else:
            unreadable_paths.add("/etc/crontab")

        # 2. /host/etc/cron.d/* (glob; skip dotfiles)
        cron_d = host_root / "etc" / "cron.d"
        if cron_d.is_dir():
            try:
                for child in sorted(cron_d.iterdir()):
                    if child.name.startswith("."):
                        continue
                    if not child.is_file():
                        continue
                    host_path = f"/etc/cron.d/{child.name}"
                    entries, errors, file_partial = self._scan_one_file(
                        container_path=child,
                        host_source_path=host_path,
                        source_kind=CronSourceKind.SYSTEM_WITH_USER_FIELD,
                        host=hostname,
                    )
                    all_entries.extend(entries)
                    all_errors.extend(errors)
                    partial = partial or file_partial
                    if not errors:
                        clean_source_paths.add(host_path)
                    else:
                        unreadable_paths.add(host_path)
            except OSError as exc:
                all_errors.append(CronScanError(host_source_path="/etc/cron.d", error=str(exc)))
                partial = True
                unreachable_prefixes.add("/etc/cron.d")

        # 3. Crontab snapshot directory (Option B fix). The host-side
        #    hm-crontab-snapshot script writes one file per user here
        #    (filename = username; content = raw `crontab -l` output). The
        #    discoverer reads the SNAPSHOT instead of the 0600 spool files —
        #    the snapshot is root-generated + world-readable, so the non-root
        #    container can read it without breaking vixie-cron with an ACL.
        #    source_path stays "crontab:<user>" so fingerprints are unchanged.
        snapshot_dir = resolve_snapshot_dir()
        if snapshot_dir.is_dir():
            try:
                for child in sorted(snapshot_dir.iterdir()):
                    if child.name.startswith("."):
                        continue
                    if not child.is_file():
                        continue
                    user = child.name
                    host_path = f"crontab:{user}"
                    entries, errors, file_partial = self._scan_one_file(
                        container_path=child,
                        host_source_path=host_path,
                        source_kind=CronSourceKind.USER_CRONTAB,
                        host=hostname,
                    )
                    all_entries.extend(entries)
                    all_errors.extend(errors)
                    partial = partial or file_partial
                    if not errors:
                        clean_source_paths.add(host_path)
                    else:
                        unreadable_paths.add(host_path)
            except OSError as exc:
                all_errors.append(
                    CronScanError(host_source_path="/var/spool/cron/crontabs", error=str(exc))
                )
                partial = True
                unreachable_prefixes.add("/var/spool/cron/crontabs")

        # 4. Upsert each parsed entry. (MUST run before reconciliation — the
        #    caller reconciles AFTER scan() returns.)
        found_fingerprints: set[str] = set()
        found_by_source_path: dict[str, set[str]] = {}
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
                    is_wrapped=entry.is_wrapped,
                    now=now,
                )
                found_fingerprints.add(record.fingerprint)
                found_by_source_path.setdefault(entry.host_source_path, set()).add(
                    record.fingerprint
                )
                if inserted:
                    inserted_count += 1
                elif updated_non_bump:
                    updated_count += 1
                else:
                    bump_only_count += 1
            except Exception as exc:
                all_errors.append(
                    CronScanError(
                        host_source_path=entry.host_source_path,
                        error=f"upsert failed: {exc}",
                    )
                )
                partial = True
                # An upsert failure means we did NOT cleanly process this file's
                # contents — drop it from clean_source_paths so reconciliation
                # does not soft-delete its sibling rows on a half-applied scan.
                clean_source_paths.discard(entry.host_source_path)

        if hasattr(log, "info"):
            log.info(  # type: ignore[attr-defined]
                "cron_discoverer.scan_complete",
                inserted=inserted_count,
                updated=updated_count,
                bump_only=bump_only_count,
                errors=len(all_errors),
                partial=partial,
                clean_paths=len(clean_source_paths),
                unreadable_paths=len(unreadable_paths),
            )

        # D3: a known cron.d / spool file the operator deleted is absent from
        # iterdir, so it is not yet in clean_source_paths. Pull in every
        # source_path the DB has for this host that lives under a cleanly
        # iterated prefix — those rows are soft-delete candidates this cycle.
        known_db_paths: frozenset[str]
        try:
            known_db_paths = await cron_repo.list_source_paths_for_host(hostname)
        except Exception as exc:
            # D3 augmentation (re-adding operator-deleted cron.d/spool files) is
            # skipped this cycle; the next successful scan catches up.
            known_db_paths = frozenset()
            all_errors.append(CronScanError(host_source_path="<reconcile-prep>", error=str(exc)))
            partial = True
        for db_path in known_db_paths:
            if db_path == "/etc/crontab":
                continue  # already handled by the single-file branch
            if db_path in unreadable_paths:
                # The file exists on disk but could not be read this scan
                # (e.g. 0600 perms vs the container UID). Re-adding it here
                # would let reconcile soft-delete every row under it against
                # an empty found-set. NEVER reconcile an unreadable path
                # (STAGE-002-007A data-corruption bugfix).
                continue
            # Any other prefix: scanner does not own it -> never reconcile.
            if (
                db_path.startswith("/etc/cron.d/") and "/etc/cron.d" not in unreachable_prefixes
            ) or (
                db_path.startswith("crontab:")
                and "/var/spool/cron/crontabs" not in unreachable_prefixes
            ):
                clean_source_paths.add(db_path)

        # Final guard: an unreadable path must NEVER reach reconciliation,
        # regardless of how it entered clean_source_paths above. Subtracting
        # here makes the invariant total (STAGE-002-007A data-corruption bugfix).
        clean_source_paths -= unreadable_paths

        return CronScanResult(
            found_fingerprints=frozenset(found_fingerprints),
            partial=partial,
            errors=all_errors,
            inserted_count=inserted_count,
            updated_count=updated_count,
            bump_only_count=bump_only_count,
            host=hostname,
            clean_source_paths=frozenset(clean_source_paths),
            unreachable_source_path_prefixes=frozenset(unreachable_prefixes),
            unreadable_source_paths=frozenset(unreadable_paths),
            found_by_source_path={k: frozenset(v) for k, v in found_by_source_path.items()},
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


__all__ = ["CronDiscoverer", "resolve_hostname", "resolve_snapshot_dir"]
