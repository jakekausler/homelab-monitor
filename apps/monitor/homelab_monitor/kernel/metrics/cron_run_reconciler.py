"""CronRunReconciler — closes B-mode run windows, enriches closed runs,
prunes cron_runs.

STAGE-002-013. Scheduler-registered BaseCollector, mirrors HeartbeatStateCollector
(STAGE-002-010): same ClassVar shape, same registration in lifespan.py. The
scheduler emits homelab_collector_run_* self-metrics automatically.

Three phases per 30s tick:
1. window-finalize — close B-mode runs by next-CMD line or 6h timeout cap; set
   the `overlapping` flag.
2. enrich — for each closed, un-enriched run past the 15s grace delay, query
   VictoriaLogs and compute line_count / byte_count / content_digest.
3. prune — delete cron_runs rows beyond 30-day / 50k-per-cron retention.

Idempotent and stateless: a missed or re-run tick re-derives. If VL is down the
enrich phase is skipped; window-finalize and prune still run.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx
import structlog

from homelab_monitor.kernel.config import (
    CronRunReconcilerConfig,
    VlQueryLimits,
    load_cron_run_reconciler_config,
    load_vl_query_limits,
)
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.cron.run_repository import CronRunRecord, CronRunRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VlQueryResult,
    build_amode_query,
    build_bmode_query,
)
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

# content_digest normalization regexes (D-DIGEST — most aggressive).
_RE_ISO_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?")
_RE_SYSLOG_TS = re.compile(r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}")
_RE_PID_BRACKET = re.compile(r"\[\d+\]")
_RE_STANDALONE_INT = re.compile(r"\b\d+\b")


def _normalize_for_digest(message: str) -> str:
    """Strip timestamps, [pid] brackets, and standalone integers from a line.

    D-DIGEST: most-aggressive normalization so two runs that differ only in
    counts / durations / timestamps still digest identically — content_digest
    reflects content SHAPE, not values. Order matters: timestamps first (they
    contain digits a later integer-strip would otherwise mangle), then [pid],
    then any remaining standalone integers.

    Example: ``v1.20.1 started [1234]`` → ``v.. started`` (every standalone
    integer is stripped, ``[1234]`` is removed, the version digits go too).
    Two crons that log different counts/versions but the same shape will
    digest identically.
    """
    out = _RE_ISO_TS.sub("", message)
    out = _RE_SYSLOG_TS.sub("", out)
    out = _RE_PID_BRACKET.sub("", out)
    out = _RE_STANDALONE_INT.sub("", out)
    return out


def compute_content_digest(messages: list[str]) -> str:
    """sha256 of the normalized, newline-joined log messages (hex).

    Each message is normalized via _normalize_for_digest before joining.

    Empty-list semantics: an empty messages list produces ``sha256("")``,
    the same digest as ``[""]`` (one empty line). For 2+ entries the
    newline separator distinguishes content from layout (`["", ""]` →
    ``sha256("\\n")``). This is consistent with D-DIGEST's "shape-only"
    intent and means line_count=0 and line_count=1-empty are
    indistinguishable by digest — only line_count tells them apart.
    """
    normalized = "\n".join(_normalize_for_digest(m) for m in messages)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string, attaching UTC if naive."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class CronRunReconciler(BaseCollector):
    """Closes B-mode run windows, enriches closed runs, prunes cron_runs."""

    name: ClassVar[str] = "cron_run_reconciler"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=20)
    concurrency_group: ClassVar[str] = "cron_run_reconciler"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run one reconciler tick: window-finalize, enrich, prune.

        Tunables (``HOMELAB_MONITOR_CRON_RUN_*``, ``HOMELAB_MONITOR_VL_QUERY_*``)
        and the VL base URL are re-read from the environment on EVERY tick.
        Operators can change them without restarting the monitor; the new
        values take effect on the next tick. The 30s tick ``ClassVar`` interval
        itself is read once at collector registration (lifespan.py).
        """
        start = time.monotonic()
        errors: list[str] = []
        now = datetime.now(UTC)

        cfg = load_cron_run_reconciler_config()
        vl_limits = load_vl_query_limits()
        run_repo = CronRunRepository(ctx.db)
        cron_repo = CronRepo(ctx.db)

        # Phase 1: window-finalize (no VL needed).
        try:
            await self._window_finalize(run_repo, now, cfg)
        except Exception as exc:
            errors.append(f"window_finalize: {exc}")

        # Phase 2: enrich (needs VL; skipped on VL failure).
        try:
            await self._enrich(ctx.db, ctx.http, run_repo, cron_repo, now, cfg, vl_limits, ctx.log)
        except Exception as exc:
            errors.append(f"enrich: {exc}")

        # Phase 3: prune (no VL needed).
        try:
            await self._prune(run_repo, now, cfg)
        except Exception as exc:
            errors.append(f"prune: {exc}")

        return CollectorResult(
            ok=(len(errors) == 0),
            metrics_emitted=0,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    async def _window_finalize(
        self,
        run_repo: CronRunRepository,
        now: datetime,
        cfg: CronRunReconcilerConfig,
    ) -> None:
        """Close B-mode running runs by next-CMD line or 6h timeout cap.

        KNOWN LIMITATION (wrapper-transition window): the next-CMD rule only
        sees B-mode (logscrape) runs of the same fingerprint. If a cron's
        observation mode switches from B-mode → A-mode (wrapper install)
        mid-stream, an intermediate A-mode start does NOT close the prior
        B-mode run; the B-mode run instead closes at the NEXT B-mode CMD
        line (which may be much later) and carries an inflated duration.
        The corresponding A-mode run carries the real per-invocation
        boundary, so the operator-visible truth is correct via A-mode; only
        the legacy B-mode row is imprecise. Mixed-source streams are
        transitional by design (D-BMODE-WINDOW), so this is acceptable.
        """
        open_runs = await run_repo.list_open_bmode_runs()
        timeout_cutoff = now - timedelta(hours=cfg.bmode_timeout_hours)

        # Group consecutive runs by fingerprint (list is ordered fp ASC, started ASC).
        by_fp: dict[str, list[CronRunRecord]] = {}
        for run in open_runs:
            by_fp.setdefault(run.cron_fingerprint, []).append(run)

        for runs in by_fp.values():
            for idx, run in enumerate(runs):
                started_dt = _parse_iso(run.started_at)
                next_run = runs[idx + 1] if idx + 1 < len(runs) else None
                if next_run is not None:
                    # next-CMD rule: close at the next run's start.
                    ended_at = next_run.started_at
                    duration = (_parse_iso(ended_at) - started_dt).total_seconds()
                    await run_repo.finalize_bmode_run(
                        run_id=run.run_id,
                        state="unknown",
                        ended_at=ended_at,
                        duration_seconds=duration,
                    )
                    continue
                # No next run: close by 6h timeout cap if past it.
                if started_dt < timeout_cutoff:
                    ended_at = (started_dt + timedelta(hours=cfg.bmode_timeout_hours)).isoformat()
                    await run_repo.finalize_bmode_run(
                        run_id=run.run_id,
                        state="unknown",
                        ended_at=ended_at,
                        duration_seconds=float(cfg.bmode_timeout_hours * 3600),
                    )
                    # A timeout-closed run whose window extends past a later run's
                    # start is the case overlapping=1 catches (D-BMODE-WINDOW).
                    await run_repo.set_overlapping(run.run_id)

    async def _enrich(  # noqa: PLR0913
        self,
        db: SqliteRepository,
        http: httpx.AsyncClient,
        run_repo: CronRunRepository,
        cron_repo: CronRepo,
        now: datetime,
        cfg: CronRunReconcilerConfig,
        vl_limits: VlQueryLimits,
        log: structlog.BoundLogger,
    ) -> None:
        """Enrich closed, un-enriched runs past the grace delay with VL fields."""
        grace_cutoff = (now - timedelta(seconds=cfg.enrich_grace_seconds)).isoformat()
        pending = await run_repo.list_runs_needing_enrich(grace_cutoff)
        if not pending:
            return

        vl_url = os.environ.get("HOMELAB_MONITOR_VL_URL", "http://victorialogs:9428")
        client = VictoriaLogsClient(vl_url=vl_url, http_client=http, limits=vl_limits)

        for run in pending:
            window_start = run.vl_window_start or run.started_at
            if run.ended_at is None:
                # Defensive: the enrich SQL already enforces ended_at IS NOT
                # NULL. If invariant breaks (schema change, hand-crafted row),
                # log + skip rather than crash the tick.
                log.warning(
                    "cron_run_reconciler.enrich_skipped_no_ended_at",
                    run_id=run.run_id,
                )
                continue
            window_end = run.vl_window_end or run.ended_at
            if run.source == "wrapper":
                expr = build_amode_query(run.run_id)
            else:
                # B-mode: need the cron's command for the canonical-key query.
                cron = await cron_repo.get_cron(run.cron_fingerprint, include_hidden=True)
                if cron is None:
                    continue  # cron gone; cannot build the heuristic query
                expr = build_bmode_query(cron.command)
            # Per-run isolation: a single run's enrich failure (a malformed
            # query, a VL-side HTTP error, a transient DB lock during
            # set_enrichment, etc.) MUST NOT abort the whole enrich phase.
            # Log it, leave enriched_at NULL (retried next tick, idempotent
            # per §6.3), and continue with the other runs. We catch the broad
            # ``Exception`` here on purpose: any per-run failure mode is a
            # local concern, not a phase-level concern. The outer ``run()``
            # still catches anything that escapes the loop.
            try:
                result: VlQueryResult = await client.query(
                    expr=expr, start=window_start, end=window_end
                )
                messages = [line.message for line in result.lines]
                byte_count = sum(len(m.encode("utf-8")) for m in messages)
                await run_repo.set_enrichment(
                    run_id=run.run_id,
                    line_count=len(messages),
                    byte_count=byte_count,
                    content_digest=compute_content_digest(messages),
                    enriched_at=utc_now_iso(),
                )
            except Exception as exc:  # per-run isolation, see STAGE-013 BUG-2
                log.warning(
                    "cron_run_reconciler.enrich_run_skipped",
                    run_id=run.run_id,
                    source=run.source,
                    error=str(exc),
                )
                continue

    async def _prune(
        self,
        run_repo: CronRunRepository,
        now: datetime,
        cfg: CronRunReconcilerConfig,
    ) -> None:
        """Prune cron_runs beyond 30-day / 50k-per-cron retention."""
        retention_cutoff = (now - timedelta(days=cfg.retention_days)).isoformat()
        await run_repo.prune_runs(
            retention_cutoff=retention_cutoff,
            max_rows_per_cron=cfg.max_rows_per_cron,
        )


__all__ = [
    "CronRunReconciler",
    "compute_content_digest",
]
