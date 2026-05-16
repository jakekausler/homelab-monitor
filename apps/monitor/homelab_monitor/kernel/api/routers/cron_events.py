"""POST /api/internal/cron-events — Vector → app structured cron-event ingest.

Option D pipeline (STAGE-002-008): Vector parses cron log lines via a VRL
transform and pushes structured events here. Auth: API token with
``Scope.CRON_EVENTS_INGEST_WRITE`` (Vector is configured with the token minted
by ``ensure_cron_events_token`` at boot).

Per-event flow:
1. Resolve a journald ``__CURSOR`` (or synthesize one for the syslog path).
2. ``try_claim_cursor`` — if already processed, skip (replay).
3. ``canonical_log_key(command)`` → match crons on ``(host, log_match_key)``.
4. 0 matches → skip + log. 2+ matches → skip + ``ambiguous`` metric + log (D4).
5. Exactly 1 match:
   - ``exit_code is None`` (vanilla dispatch line) → ``record_observed_run``
     (NEUTRAL — D1).
   - ``exit_code == 0`` → ``record_ok``.
   - ``exit_code != 0`` → ``record_fail``.

Logging discipline: never log full command strings at INFO (may contain
hostnames / paths). Log fingerprints + counts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, field_validator
from starlette.responses import JSONResponse
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.api.dependencies import (
    get_cron_repo,
    get_heartbeat_repo,
    get_metrics_writer,
    require_token_scope,
)
from homelab_monitor.kernel.auth.models import ApiToken
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.cron.log_event import (
    CronEventDisposition,
    CronLogEvent,
    synthesize_cursor,
)
from homelab_monitor.kernel.cron.log_match import canonical_log_key
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.heartbeat.repository import HeartbeatRepo
from homelab_monitor.kernel.plugins.io import MetricsWriter

router = APIRouter(prefix="/internal", tags=["internal"])

INGEST_WHO = "system:log-scrape"
_METRIC_MATCHES = "homelab_cron_logscrape_matches_total"
_METRIC_AMBIGUOUS = "homelab_cron_logscrape_ambiguous_total"


class CronEventItem(BaseModel):
    """One structured cron event in the ingest batch."""

    model_config = ConfigDict(extra="forbid")
    host: str
    command: str
    user: str = ""
    timestamp: str
    exit_code: int | None = None
    journal_cursor: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, v: str) -> str:
        """Validate + normalize the event timestamp to UTC ISO-8601.

        Vector's VRL ``to_string(ts) ?? ""`` can yield an empty string when the
        journald event carries no usable timestamp. Reject empty/garbage input
        with a 422 here so a bad timestamp never flows verbatim into
        ``heartbeats_state.last_observed_run_at`` or ``audit_log.when`` (repo
        invariant: all internal timestamps are UTC ISO-8601).
        """
        text_value = v.strip()
        if not text_value:
            msg = "timestamp must be a non-empty ISO-8601 string"
            raise ValueError(msg)
        candidate = text_value
        if candidate.endswith(("Z", "z")):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            msg = f"timestamp must be ISO-8601; got: {v!r}"
            raise ValueError(msg) from exc
        if parsed.tzinfo is None:
            msg = f"timestamp must be timezone-aware ISO-8601; got: {v!r}"
            raise ValueError(msg)
        return parsed.astimezone(UTC).isoformat()


class CronEventsIngestResponse(BaseModel):
    """202 response summary."""

    received: int
    observed_runs: int
    state_ok: int
    state_fail: int
    replay_skipped: int
    no_match: int
    ambiguous: int


@router.post("/cron-events", response_model=CronEventsIngestResponse, status_code=202)
async def ingest_cron_events(
    events: list[CronEventItem],
    _token: Annotated[ApiToken, Depends(require_token_scope(Scope.CRON_EVENTS_INGEST_WRITE))],
    cron_repo: Annotated[CronRepo, Depends(get_cron_repo)],
    heartbeat_repo: Annotated[HeartbeatRepo, Depends(get_heartbeat_repo)],
    metrics: Annotated[MetricsWriter, Depends(get_metrics_writer)],
) -> JSONResponse:
    """Ingest a batch of structured cron log events from Vector."""
    log = structlog.get_logger().bind(component="cron_events.ingest")
    counts = {d: 0 for d in CronEventDisposition}

    for item in events:
        event = CronLogEvent(
            host=item.host,
            command=item.command,
            user=item.user,
            timestamp=item.timestamp,
            exit_code=item.exit_code,
            journal_cursor=item.journal_cursor,
        )
        disposition = await _process_one(
            event,
            cron_repo=cron_repo,
            heartbeat_repo=heartbeat_repo,
            metrics=metrics,
            log=log,
        )
        counts[disposition] += 1

    return JSONResponse(
        status_code=202,
        content=CronEventsIngestResponse(
            received=len(events),
            observed_runs=counts[CronEventDisposition.OBSERVED_RUN],
            state_ok=counts[CronEventDisposition.STATE_OK],
            state_fail=counts[CronEventDisposition.STATE_FAIL],
            replay_skipped=counts[CronEventDisposition.REPLAY_SKIPPED],
            no_match=counts[CronEventDisposition.NO_MATCH],
            ambiguous=counts[CronEventDisposition.AMBIGUOUS],
        ).model_dump(mode="json"),
    )


async def _process_one(
    event: CronLogEvent,
    *,
    cron_repo: CronRepo,
    heartbeat_repo: HeartbeatRepo,
    metrics: MetricsWriter,
    log: BoundLogger,
) -> CronEventDisposition:
    """Process one event. Returns its disposition. Idempotent on cursor.

    Delivery semantics: AT-MOST-ONCE. ``try_claim_cursor`` commits the cursor
    in its own transaction before the ``record_*`` state write commits in a
    separate one. A crash between the two drops the run permanently on re-POST.
    Accepted trade-off — see ``CronRepo.try_claim_cursor`` and
    docs/architecture/cron-logscrape.md.
    """
    cursor = event.journal_cursor or synthesize_cursor(event)
    now = utc_now_iso()

    claimed = await cron_repo.try_claim_cursor(cursor, now)
    if not claimed:
        return CronEventDisposition.REPLAY_SKIPPED

    log_key = canonical_log_key(event.command)
    matches = await cron_repo.match_by_log_key(event.host, log_key)

    if not matches:
        log.info("cron_events.no_match", host=event.host)
        return CronEventDisposition.NO_MATCH

    if len(matches) > 1:
        metrics.write_counter(_METRIC_AMBIGUOUS, 1.0, {"host": event.host})
        log.warning(
            "cron_events.ambiguous_match",
            host=event.host,
            candidate_fingerprints=sorted(m.fingerprint for m in matches),
        )
        return CronEventDisposition.AMBIGUOUS

    cron = matches[0]
    metrics.write_counter(_METRIC_MATCHES, 1.0, {"host": event.host})

    if event.exit_code is None:
        await heartbeat_repo.record_observed_run(
            cron.fingerprint,
            observed_at=event.timestamp,
            who=INGEST_WHO,
            ip=None,
        )
        return CronEventDisposition.OBSERVED_RUN
    if event.exit_code == 0:
        await heartbeat_repo.record_ok(
            cron.fingerprint,
            duration_seconds=None,
            who=INGEST_WHO,
            ip=None,
        )
        return CronEventDisposition.STATE_OK
    await heartbeat_repo.record_fail(
        cron.fingerprint,
        duration_seconds=None,
        exit_code=event.exit_code,
        who=INGEST_WHO,
        ip=None,
    )
    return CronEventDisposition.STATE_FAIL


__all__ = ["router"]
