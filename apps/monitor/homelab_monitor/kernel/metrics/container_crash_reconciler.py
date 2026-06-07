"""ContainerCrashReconciler — poll-based container crash log correlation.

STAGE-004-032. Scheduler-registered BaseCollector mirroring CronRunReconciler
(STAGE-002-013): same ClassVar shape, same per-phase try/except, re-reads config
each tick, builds its own VictoriaLogs access from ctx.http.

There is NO container-stop event stream (the docker /events consumer ignores
die/stop), so detection is poll-based: each 30s tick lists docker containers,
filters to crashed ones (status exited/dead with a non-zero exit code), fetches
a VictoriaLogs window centered on the container's FinishedAt, and persists one
enrichment row per (logical_key, finished_at). The UNIQUE index makes re-detects
idempotent. A newly-inserted crash emits homelab_container_crash_total (drives the
vmalert ContainerCrashed alert) and homelab_container_crash_with_log_context.

Two phases per tick:
1. ENRICH — detect + enrich + persist + emit (per-container isolated).
2. PRUNE  — age + per-container retention.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from homelab_monitor.kernel.config import (
    CrashLogConfig,
    VlQueryLimits,
    load_crash_log_config,
    load_vl_query_limits,
)
from homelab_monitor.kernel.db.repositories.targets_repository import (
    DockerContainerListRow,
    TargetsRepository,
)
from homelab_monitor.kernel.logs.crash_enrichments_repo import CrashEnrichmentsRepository
from homelab_monitor.kernel.logs.log_window_fetcher import LogWindowFetcher, LogWindowResult
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    logsql_quote_phrase,
)
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

_CRASH_STATES: frozenset[str] = frozenset({"exited", "dead"})
# Docker emits this when a container has never stopped (no real FinishedAt).
_ZERO_FINISHED_AT: str = "0001-01-01T00:00:00Z"


def _parse_anchor(finished_at: str | None, now: datetime) -> tuple[datetime, bool]:
    """Parse the FinishedAt crash anchor, falling back to ``now``.

    Returns ``(anchor, real)`` where ``real`` is True iff a usable FinishedAt
    was parsed. Missing, empty, the Docker zero-sentinel, or an unparseable
    value → ``(now, False)``. A naive parsed datetime is assumed UTC.
    """
    if not finished_at or finished_at == _ZERO_FINISHED_AT:
        return now, False
    try:
        dt = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except ValueError:
        return now, False
    return (dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)), True


class ContainerCrashReconciler(BaseCollector):
    """Detect crashed containers, enrich with VL log windows, persist + emit."""

    name: ClassVar[str] = "container_crash_reconciler"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=20)
    concurrency_group: ClassVar[str] = "container_crash_reconciler"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run one tick: enrich crashed containers, then prune."""
        start = time.monotonic()
        errors: list[str] = []
        now = datetime.now(UTC)

        cfg = load_crash_log_config()
        vl_limits = load_vl_query_limits()
        targets_repo = TargetsRepository(ctx.db)
        crash_repo = CrashEnrichmentsRepository(ctx.db)

        metrics_emitted = 0
        try:
            metrics_emitted = await self._enrich(ctx, targets_repo, crash_repo, cfg, vl_limits, now)
        except Exception as exc:
            errors.append(f"enrich: {exc}")

        try:
            await self._prune(crash_repo, cfg, now)
        except Exception as exc:
            errors.append(f"prune: {exc}")

        return CollectorResult(
            ok=(len(errors) == 0),
            metrics_emitted=metrics_emitted,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    async def _enrich(  # noqa: PLR0913
        self,
        ctx: CollectorContext,
        targets_repo: TargetsRepository,
        crash_repo: CrashEnrichmentsRepository,
        cfg: CrashLogConfig,
        vl_limits: VlQueryLimits,
        now: datetime,
    ) -> int:
        """Detect + enrich + persist crashed containers. Returns metrics emitted."""
        rows = await targets_repo.list_docker_containers(include_hidden=False)
        crashed: list[DockerContainerListRow] = [
            r
            for r in rows
            if r.status in _CRASH_STATES and r.exit_code is not None and r.exit_code != 0
        ]
        if not crashed:
            return 0

        vl_url = os.environ.get("HOMELAB_MONITOR_VL_URL", "http://victorialogs:9428")
        vl_client = VictoriaLogsClient(
            vl_url=vl_url,
            http_client=ctx.http,
            limits=vl_limits,
        )
        # Per-tick fetcher: its TTL/LRU cache is intentionally discarded each tick
        # (it never sees a cross-tick hit). Crashes are rare and a re-detected
        # crash dedups at the DB layer (INSERT OR IGNORE), not here. The fetcher
        # holds no closable resources — it reuses the shared ctx.http client.
        fetcher = LogWindowFetcher(vl_client)

        emitted = 0
        for row in crashed:
            try:
                emitted += await self._enrich_one(crash_repo, fetcher, cfg, row, now, ctx)
            except Exception as exc:
                ctx.log.warning(
                    "container_crash_reconciler.enrich_container_skipped",
                    container_name=row.name,
                    error=str(exc),
                )
                continue
        return emitted

    async def _enrich_one(  # noqa: PLR0913
        self,
        crash_repo: CrashEnrichmentsRepository,
        fetcher: LogWindowFetcher,
        cfg: CrashLogConfig,
        row: DockerContainerListRow,
        now: datetime,
        ctx: CollectorContext,
    ) -> int:
        """Enrich + persist + emit for one crashed container. Returns metrics emitted."""
        anchor, anchor_is_real = _parse_anchor(row.finished_at, now)
        # The dedup key (one half of the UNIQUE (logical_key, finished_at)) MUST
        # be deterministic for a given crash, else the same crash re-inserts +
        # re-emits the alert counter every tick. A real FinishedAt is stable. When
        # it is absent we CANNOT use the wall-clock anchor (it changes per tick →
        # an alert storm); instead derive a stable key from the container's
        # identity so the crashed-but-no-FinishedAt container dedups to one row.
        # A genuine recreation gets a new container_id → a fresh crash row.
        finished_at_key = (
            anchor.isoformat() if anchor_is_real else f"unknown:{row.container_id or row.name}"
        )
        logical_key = row.logical_key if row.logical_key is not None else row.name
        # The crashed filter in _enrich guarantees exit_code is a non-None,
        # non-zero value; narrow for the type checker.
        assert row.exit_code is not None
        exit_code = row.exit_code

        logs_ql = f"container_name:{logsql_quote_phrase(row.name)} AND source_type:docker"
        result: LogWindowResult = await fetcher.fetch(
            logs_ql,
            anchor,
            window_before_s=cfg.window_before_s,
            window_after_s=cfg.window_after_s,
            limit=cfg.line_limit,
        )

        inserted = await crash_repo.insert(
            crash_id=str(uuid.uuid4()),
            logical_key=logical_key,
            container_name=row.name,
            container_id=row.container_id,
            exit_code=exit_code,
            finished_at=finished_at_key,
            image_name=row.image,
            compose_project=row.compose_project,
            compose_service=row.compose_service,
            lines=result.lines,
            truncated=result.truncated,
            degraded=result.degraded,
            window_start=result.window_start.isoformat(),
            window_end=result.window_end.isoformat(),
        )
        if not inserted:
            return 0

        # New crash: emit the alerting counter (even when degraded — the
        # enrichment row carries degraded=1 and possibly empty lines, but the
        # alert must still fire) + the log-context gauge.
        ctx.vm.write_counter(
            "homelab_container_crash_total",
            1.0,
            {
                "container_name": row.name,
                "exit_code": str(exit_code),
                "compose_project": row.compose_project or "",
                "compose_service": row.compose_service or "",
            },
        )
        ctx.vm.write_gauge(
            "homelab_container_crash_with_log_context",
            float(len(result.lines)),
            {"container_name": row.name},
        )
        return 2

    async def _prune(
        self,
        crash_repo: CrashEnrichmentsRepository,
        cfg: CrashLogConfig,
        now: datetime,
    ) -> None:
        """Prune crash rows beyond retention_days / max_rows_per_container."""
        cutoff = (now - timedelta(days=cfg.retention_days)).isoformat()
        await crash_repo.prune(
            retention_cutoff_iso=cutoff,
            max_rows_per_container=cfg.max_rows_per_container,
        )


__all__ = ["ContainerCrashReconciler"]
