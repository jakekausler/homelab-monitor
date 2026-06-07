"""ContainerHealthcheckReconciler — poll-based healthcheck-unhealthy log correlation.

STAGE-004-033. Scheduler-registered BaseCollector mirroring ContainerCrashReconciler
(STAGE-004-032): same ClassVar shape, same per-phase try/except, re-reads config
each tick, builds its own VictoriaLogs access from ctx.http.

Transition detection is COLLECTOR-side: the docker socket collector's upsert stamps
targets_docker.healthcheck_changed_at on the edge INTO "unhealthy" (one stamp per
episode). Each 30s tick this reconciler lists docker containers, filters to those
that are currently "unhealthy" WITH a stamped healthcheck_changed_at, fetches a
VictoriaLogs window centered on that stamp, and persists one enrichment row per
(logical_key, healthcheck_changed_at). The UNIQUE index makes re-detects idempotent.
A newly-inserted episode emits homelab_container_healthcheck_unhealthy_total (drives
the vmalert ContainerUnhealthy alert) and homelab_container_healthcheck_with_log_context.

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
    HealthcheckLogConfig,
    VlQueryLimits,
    load_healthcheck_log_config,
    load_vl_query_limits,
)
from homelab_monitor.kernel.db.repositories.targets_repository import (
    DockerContainerListRow,
    TargetsRepository,
)
from homelab_monitor.kernel.logs.healthcheck_enrichments_repo import (
    HealthcheckEnrichmentsRepository,
)
from homelab_monitor.kernel.logs.log_window_fetcher import LogWindowFetcher, LogWindowResult
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    logsql_quote_phrase,
)
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

_UNHEALTHY: str = "unhealthy"


def _parse_anchor(changed_at: str, now: datetime) -> datetime:
    """Parse the healthcheck_changed_at anchor, falling back to ``now``.

    The _enrich filter guarantees changed_at is a non-None stamped ISO string, but
    parse defensively: an unparseable value falls back to ``now``. A naive parsed
    datetime is assumed UTC.
    """
    try:
        dt = datetime.fromisoformat(changed_at.replace("Z", "+00:00"))
    except ValueError:
        return now
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class ContainerHealthcheckReconciler(BaseCollector):
    """Detect unhealthy containers, enrich with VL log windows, persist + emit."""

    name: ClassVar[str] = "container_healthcheck_reconciler"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=20)
    concurrency_group: ClassVar[str] = "container_healthcheck_reconciler"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run one tick: enrich unhealthy containers, then prune."""
        start = time.monotonic()
        errors: list[str] = []
        now = datetime.now(UTC)

        cfg = load_healthcheck_log_config()
        vl_limits = load_vl_query_limits()
        targets_repo = TargetsRepository(ctx.db)
        hc_repo = HealthcheckEnrichmentsRepository(ctx.db)

        metrics_emitted = 0
        try:
            metrics_emitted = await self._enrich(ctx, targets_repo, hc_repo, cfg, vl_limits, now)
        except Exception as exc:
            errors.append(f"enrich: {exc}")

        try:
            await self._prune(hc_repo, cfg, now)
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
        hc_repo: HealthcheckEnrichmentsRepository,
        cfg: HealthcheckLogConfig,
        vl_limits: VlQueryLimits,
        now: datetime,
    ) -> int:
        """Detect + enrich + persist unhealthy containers. Returns metrics emitted."""
        rows = await targets_repo.list_docker_containers(include_hidden=False)
        unhealthy: list[DockerContainerListRow] = [
            r for r in rows if r.healthcheck == _UNHEALTHY and r.healthcheck_changed_at is not None
        ]
        if not unhealthy:
            return 0

        vl_url = os.environ.get("HOMELAB_MONITOR_VL_URL", "http://victorialogs:9428")
        vl_client = VictoriaLogsClient(
            vl_url=vl_url,
            http_client=ctx.http,
            limits=vl_limits,
        )
        # Per-tick fetcher: its TTL/LRU cache is intentionally discarded each tick
        # (it never sees a cross-tick hit). Unhealthy episodes are rare and a
        # re-detected episode dedups at the DB layer (INSERT OR IGNORE), not here.
        # The fetcher holds no closable resources — it reuses the shared ctx.http.
        fetcher = LogWindowFetcher(vl_client)

        emitted = 0
        for row in unhealthy:
            try:
                emitted += await self._enrich_one(hc_repo, fetcher, cfg, row, now, ctx)
            except Exception as exc:
                ctx.log.warning(
                    "container_healthcheck_reconciler.enrich_container_skipped",
                    container_name=row.name,
                    error=str(exc),
                )
                continue
        return emitted

    async def _enrich_one(  # noqa: PLR0913
        self,
        hc_repo: HealthcheckEnrichmentsRepository,
        fetcher: LogWindowFetcher,
        cfg: HealthcheckLogConfig,
        row: DockerContainerListRow,
        now: datetime,
        ctx: CollectorContext,
    ) -> int:
        """Enrich + persist + emit for one unhealthy container. Returns metrics emitted."""
        # The _enrich filter guarantees healthcheck_changed_at is non-None.
        assert row.healthcheck_changed_at is not None
        changed_at = row.healthcheck_changed_at
        anchor = _parse_anchor(changed_at, now)
        logical_key = row.logical_key if row.logical_key is not None else row.name

        logs_ql = f"container_name:{logsql_quote_phrase(row.name)} AND source_type:docker"
        result: LogWindowResult = await fetcher.fetch(
            logs_ql,
            anchor,
            window_before_s=cfg.window_before_s,
            window_after_s=cfg.window_after_s,
            limit=cfg.line_limit,
        )

        inserted = await hc_repo.insert(
            incident_id=str(uuid.uuid4()),
            logical_key=logical_key,
            container_name=row.name,
            container_id=row.container_id,
            previous_healthcheck=row.previous_healthcheck,
            new_state=_UNHEALTHY,
            healthcheck_changed_at=changed_at,
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

        # New episode: emit the alerting counter (even when degraded — the
        # enrichment row carries degraded=1 and possibly empty lines, but the
        # alert must still fire) + the log-context gauge.
        ctx.vm.write_counter(
            "homelab_container_healthcheck_unhealthy_total",
            1.0,
            {
                "container_name": row.name,
                "compose_project": row.compose_project or "",
                "compose_service": row.compose_service or "",
            },
        )
        ctx.vm.write_gauge(
            "homelab_container_healthcheck_with_log_context",
            float(len(result.lines)),
            {"container_name": row.name},
        )
        return 2

    async def _prune(
        self,
        hc_repo: HealthcheckEnrichmentsRepository,
        cfg: HealthcheckLogConfig,
        now: datetime,
    ) -> None:
        """Prune rows beyond retention_days / max_rows_per_container."""
        cutoff = (now - timedelta(days=cfg.retention_days)).isoformat()
        await hc_repo.prune(
            retention_cutoff_iso=cutoff,
            max_rows_per_container=cfg.max_rows_per_container,
        )


__all__ = ["ContainerHealthcheckReconciler"]
