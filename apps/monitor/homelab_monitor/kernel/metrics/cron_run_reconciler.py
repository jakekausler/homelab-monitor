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
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx
import structlog

from homelab_monitor.kernel.config import (
    CronAnomalyConfig,
    CronRunReconcilerConfig,
    VlQueryLimits,
    load_cron_anomaly_config,
    load_cron_run_reconciler_config,
    load_vl_query_limits,
)
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.cron.run_anomaly import evaluate_run
from homelab_monitor.kernel.cron.run_repository import CronRunRecord, CronRunRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.logs.cron_run_failure_enrichments_repo import (
    CronRunFailureEnrichmentsRepository,
)
from homelab_monitor.kernel.logs.models import LogLine, from_victorialogs_line
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VictoriaLogsClientError,
    VlQueryResult,
    build_amode_query,
    build_bmode_query,
)
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import MetricsWriter
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
        anomaly_cfg = load_cron_anomaly_config()
        run_repo = CronRunRepository(ctx.db)
        cron_repo = CronRepo(ctx.db)

        # Phase 1: window-finalize (no VL needed).
        try:
            await self._window_finalize(run_repo, now, cfg)
        except Exception as exc:
            errors.append(f"window_finalize: {exc}")

        # Phase 2: enrich (needs VL; skipped on VL failure).
        metrics_emitted = 0
        try:
            metrics_emitted = await self._enrich(
                ctx.db,
                ctx.http,
                run_repo,
                cron_repo,
                now,
                cfg,
                vl_limits,
                anomaly_cfg,
                ctx.vm,
                ctx.log,
            )
        except Exception as exc:
            errors.append(f"enrich: {exc}")

        # Phase 3: prune (no VL needed).
        try:
            await self._prune(ctx.db, run_repo, now, cfg)
        except Exception as exc:
            errors.append(f"prune: {exc}")

        return CollectorResult(
            ok=(len(errors) == 0),
            metrics_emitted=metrics_emitted,
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
        anomaly_cfg: CronAnomalyConfig,
        vm: MetricsWriter,
        log: structlog.BoundLogger,
    ) -> int:
        """Enrich closed, un-enriched runs past the grace delay with VL fields.

        Returns the number of failure-enrich metric writes emitted this call
        (homelab_cron_run_failure_total). The main per-run VL enrichment emits no
        metrics; only the STAGE-004-034 failure-enrich step does.
        """
        grace_cutoff = (now - timedelta(seconds=cfg.enrich_grace_seconds)).isoformat()
        pending = await run_repo.list_runs_needing_enrich(grace_cutoff)
        if not pending:
            return 0

        # Bound per-tick work to keep the tick well inside the 20s timeout.
        # A large backlog drains over successive ticks; enrichment is idempotent
        # per §6.3 so retries are cheap. list_runs_needing_enrich orders oldest-
        # first, so this slice drains the oldest rows on every tick.
        if len(pending) > cfg.enrich_max_per_tick:
            pending = pending[: cfg.enrich_max_per_tick]

        vl_url = os.environ.get("HOMELAB_MONITOR_VL_URL", "http://victorialogs:9428")
        client = VictoriaLogsClient(vl_url=vl_url, http_client=http, limits=vl_limits)

        # STAGE-004-034: a dedicated small-limit client for the last-N failure
        # snapshot (one extra capped VL call per NEWLY-failed run). max_bytes /
        # timeout reuse the standard vl_limits; only max_lines is narrowed.
        failure_repo = CronRunFailureEnrichmentsRepository(db)
        failure_limits = replace(vl_limits, max_lines=cfg.cron_failure_enrich_max_lines)
        failure_client = VictoriaLogsClient(vl_url=vl_url, http_client=http, limits=failure_limits)
        failure_metrics_emitted = 0

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
                # Widen the upper bound of the VL query to bridge the gap between
                # when the wrapper posts /ok (stored as vl_window_end/ended_at) and
                # when the captured hmrun marker lines actually appear in
                # VictoriaLogs (journald → Vector → VL ingest latency). This slack
                # is wrapper-only: logscrape runs have no hmrun markers to wait for.
                if cfg.enrich_window_slack_seconds > 0:
                    window_end_dt = _parse_iso(window_end) + timedelta(
                        seconds=cfg.enrich_window_slack_seconds
                    )
                    window_end = window_end_dt.isoformat()
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
                line_count = len(messages)
                digest = compute_content_digest(messages)
                now_iso = utc_now_iso()
                await run_repo.set_enrichment(
                    run_id=run.run_id,
                    line_count=line_count,
                    byte_count=byte_count,
                    content_digest=digest,
                    enriched_at=now_iso,
                )
                # Anomaly evaluation — inside the same per-run try/except so a buggy
                # evaluator or a DB hiccup writing flags does not poison the enrich
                # phase. We hydrate an updated CronRunRecord with the just-computed
                # line_count/byte_count so the evaluator sees the same row the rest of
                # the system will see (a re-fetch via get_run would also work but
                # costs an extra DB round trip).
                updated_run = CronRunRecord(
                    run_id=run.run_id,
                    cron_fingerprint=run.cron_fingerprint,
                    source=run.source,
                    state=run.state,
                    started_at=run.started_at,
                    ended_at=run.ended_at,
                    duration_seconds=run.duration_seconds,
                    exit_code=run.exit_code,
                    vl_window_start=run.vl_window_start,
                    vl_window_end=run.vl_window_end,
                    overlapping=run.overlapping,
                    enriched_at=now_iso,
                    line_count=line_count,
                    byte_count=byte_count,
                    content_digest=digest,
                    anomaly_flags="",
                )
                history = await run_repo.list_recent_completed(
                    cron_fingerprint=run.cron_fingerprint,
                    limit=anomaly_cfg.rolling_window,
                    exclude_run_id=run.run_id,
                )
                flags = evaluate_run(updated_run, history, anomaly_cfg)
                if flags:
                    await run_repo.set_anomaly_flags(run_id=run.run_id, anomaly_flags=flags)
            except Exception as exc:  # per-run isolation, see STAGE-013 BUG-2
                log.warning(
                    "cron_run_reconciler.enrich_run_skipped",
                    run_id=run.run_id,
                    source=run.source,
                    error=str(exc),
                )
                continue

            # STAGE-004-034: failure-enrich — for a FAILED run, persist the last-N
            # lines as a forensic snapshot + emit the per-run alert counter. Its
            # OWN try/except: a VL/DB error here must not break the main enrich
            # loop or other runs. Reuses `expr` + `window_start`/`window_end`
            # already computed above for this run.
            if run.state == "fail":
                try:
                    failure_metrics_emitted += await self._failure_enrich_run(
                        failure_repo=failure_repo,
                        failure_client=failure_client,
                        cron_repo=cron_repo,
                        run=run,
                        expr=expr,
                        window_start=window_start,
                        window_end=window_end,
                        vm=vm,
                        log=log,
                    )
                except Exception as exc:
                    log.warning(
                        "cron_run_reconciler.failure_enrich_skipped",
                        run_id=run.run_id,
                        source=run.source,
                        error=str(exc),
                    )
                    continue

        return failure_metrics_emitted

    async def _failure_enrich_run(  # noqa: PLR0913
        self,
        *,
        failure_repo: CronRunFailureEnrichmentsRepository,
        failure_client: VictoriaLogsClient,
        cron_repo: CronRepo,
        run: CronRunRecord,
        expr: str,
        window_start: str,
        window_end: str,
        vm: MetricsWriter,
        log: structlog.BoundLogger,
    ) -> int:
        """Persist the last-N lines of a FAILED run + emit the alert counter.

        Returns the number of counter writes emitted (0 or 1). A VL failure still
        inserts a degraded row (lines=[], degraded=True) so the alert fires —
        mirrors the 032/033 "degraded-still-emits" decision. Idempotent via the
        repo's INSERT OR IGNORE on (cron_fingerprint, run_id): a re-enriched run
        neither double-inserts nor double-emits.
        """
        lines: list[LogLine] = []
        truncated = False
        degraded = False
        try:
            result: VlQueryResult = await failure_client.query(
                expr=expr, start=window_start, end=window_end
            )
            lines = [from_victorialogs_line(line) for line in result.lines]
            truncated = result.truncated
        except VictoriaLogsClientError as exc:
            # VL down: still persist a degraded row so the alert fires; the
            # snapshot is empty and degraded=1 flags it.
            degraded = True
            log.warning(
                "cron_run_reconciler.failure_enrich_vl_degraded",
                run_id=run.run_id,
                error=str(exc),
            )

        inserted = await failure_repo.insert(
            failure_id=uuid.uuid4().hex,
            cron_fingerprint=run.cron_fingerprint,
            run_id=run.run_id,
            exit_code=run.exit_code,
            started_at=run.started_at,
            ended_at=run.ended_at,
            lines=lines,
            truncated=truncated,
            degraded=degraded,
            window_start=window_start,
            window_end=window_end,
        )
        if not inserted:
            return 0

        # New failure row: emit the alerting counter. Resolve a human name for the
        # label from the cron registry (cheap — only on a newly-failed run).
        cron = await cron_repo.get_cron(run.cron_fingerprint, include_hidden=True)
        labels: dict[str, str] = {
            "cron_fingerprint": run.cron_fingerprint,
            "run_id": run.run_id,
        }
        if cron is not None:
            labels["name"] = cron.name
            labels["host"] = cron.host
        vm.write_counter("homelab_cron_run_failure_total", 1.0, labels)
        return 1

    async def _prune(
        self,
        db: SqliteRepository,
        run_repo: CronRunRepository,
        now: datetime,
        cfg: CronRunReconcilerConfig,
    ) -> None:
        """Prune cron_runs AND cron_run_failure_enrichments beyond retention.

        The failure-enrichment table has its OWN retention horizon
        (cron_failure_enrich_retention_days, default 30) + per-fingerprint cap,
        decoupled from cron_runs' prune so a failed run's forensic record
        outlives the lifecycle row (D-CRON-RETAIN-30D).
        """
        retention_cutoff = (now - timedelta(days=cfg.retention_days)).isoformat()
        await run_repo.prune_runs(
            retention_cutoff=retention_cutoff,
            max_rows_per_cron=cfg.max_rows_per_cron,
        )
        failure_cutoff = (now - timedelta(days=cfg.cron_failure_enrich_retention_days)).isoformat()
        await CronRunFailureEnrichmentsRepository(db).prune(
            retention_cutoff_iso=failure_cutoff,
            max_rows_per_cron=cfg.cron_failure_enrich_max_rows_per_cron,
        )


__all__ = [
    "CronRunReconciler",
    "compute_content_digest",
]
