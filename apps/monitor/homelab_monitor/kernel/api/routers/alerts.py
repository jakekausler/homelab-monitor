"""POST /api/alerts/ingest, GET /api/alerts, GET /api/alerts/{id},
POST /api/alerts/{id}/ack, POST /api/alerts/{id}/dismiss.

Auth model:

- ``ingest`` accepts EITHER cookie session (with CSRF) OR API token with
  ``Scope.ALERTS_INGEST_WRITE``. Programmatic callers (Alertmanager) use
  the token path; operators using the dashboard go through cookies.
- ``list``, ``get``, ``ack``, ``dismiss`` are session-only (privileged
  operator surface; tokens are intended for ingest, not management).

Logging discipline: ``ingest`` MUST NOT log full alert payloads at INFO level
(label dicts may contain hostnames, internal IPs, secrets). Only counts and
fingerprints are logged at INFO; full payloads are persisted to ``alerts.payload_json``
where the operator can inspect them via ``GET /api/alerts/{id}``.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.alerts.events import (
    AlertFiringEvent,
    AlertResolvedEvent,
)
from homelab_monitor.kernel.alerts.fingerprinting import compute_fingerprint
from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import (
    Alert,
    AlertmanagerV2AlertItem,
    AlertmanagerV2Payload,
    AlertOutcome,
    AlertStatus,
    Severity,
)
from homelab_monitor.kernel.api.dependencies import (
    get_alert_dispatcher,
    get_alert_repo,
    require_session,
    require_user_or_token,
)
from homelab_monitor.kernel.api.errors import NotFoundProblem
from homelab_monitor.kernel.api.schemas import (
    AckResponse,
    AlertDetailResponse,
    AlertListResponse,
    AlertView,
    DismissResponse,
    IngestResponse,
    OutcomeView,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.dispatch.dispatcher import AlertDispatcher

router = APIRouter(prefix="/alerts", tags=["alerts"])


class CommentBody(BaseModel):
    """Optional comment body for ack/dismiss endpoints."""

    model_config = ConfigDict(extra="forbid")
    comment: str | None = None


def _alert_to_view(alert: Alert) -> AlertView:
    """Project an Alert into the public AlertView."""
    return AlertView(
        id=alert.id,
        fingerprint=alert.fingerprint,
        source_tool=alert.source_tool,
        severity=alert.severity,
        status=alert.status,
        opened_at=alert.opened_at,
        last_seen_at=alert.last_seen_at,
        resolved_at=alert.resolved_at,
        ack_at=alert.ack_at,
        ack_by=alert.ack_by,
        runbook_id=alert.runbook_id,
        labels=alert.labels,
        annotations=alert.annotations,
    )


def _resolve_severity(
    item: AlertmanagerV2AlertItem,
    fingerprint: str,
    log: BoundLogger,
) -> Severity:
    """Map item.labels['severity'] to a Severity, defaulting to WARNING with a log line.

    F10: log the fingerprint (not the alertname) so WARNING-level records
    cannot leak label-derived secrets (hostnames, internal IPs, etc.).
    """
    raw = item.labels.get("severity")
    if not raw:
        log.warning(
            "alerts.ingest.severity_missing",
            fingerprint=fingerprint,
        )
        return Severity.WARNING
    try:
        return Severity(raw)
    except ValueError:
        log.warning(
            "alerts.ingest.severity_invalid",
            fingerprint=fingerprint,
            severity_raw=raw,
        )
        return Severity.WARNING


def _build_payload_json(item: AlertmanagerV2AlertItem) -> str:
    """Serialise the item back to a stable payload_json that ``_row_to_alert`` can rehydrate.

    The repository's ``_row_to_alert`` helper expects ``payload.labels`` and
    ``payload.annotations`` to be present so they round-trip into ``Alert.labels``
    and ``Alert.annotations``.
    """
    payload: dict[str, Any] = {
        "labels": dict(item.labels),
        "annotations": dict(item.annotations),
        "startsAt": item.startsAt,
        "endsAt": item.endsAt,
        "generatorURL": item.generatorURL,
        "fingerprint": item.fingerprint,
        "status": item.status,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=202,
)
async def ingest_alerts(
    payload: AlertmanagerV2Payload,
    _principal: Annotated[
        object,
        Depends(require_user_or_token({Scope.ALERTS_INGEST_WRITE})),
    ],
    alert_repo: Annotated[AlertRepository, Depends(get_alert_repo)],
    dispatcher: Annotated[AlertDispatcher, Depends(get_alert_dispatcher)],
) -> JSONResponse:
    """Ingest an Alertmanager v2 webhook payload.

    For each alert in ``payload.alerts``:

    - Compute the fingerprint via ``compute_fingerprint``.
    - If ``status == "firing"``: dedupe by fingerprint. Bump ``last_seen_at`` if
      a row exists, else insert a new firing row. Dispatch ``AlertFiringEvent``.
    - If ``status == "resolved"``: look up the active row by fingerprint. If
      found, mark resolved + dispatch ``AlertResolvedEvent``. If not found,
      log INFO + skip (no row created).

    Returns 202 with ``{"received": int, "ingested": int}``. ``ingested`` counts
    items that produced a state change (firing dedup-or-insert OR resolved
    matching an active row).
    """
    log = structlog.get_logger().bind(component="alerts.ingest")
    received = len(payload.alerts)
    ingested = 0

    for item in payload.alerts:
        fingerprint = compute_fingerprint(item)
        source_tool = item.labels.get("source_tool", "alertmanager")
        severity = _resolve_severity(item, fingerprint, log)
        ts = utc_now_iso()
        labels = dict(item.labels)
        annotations = dict(item.annotations)
        payload_json = _build_payload_json(item)

        if item.status == "firing":
            existing = await alert_repo.find_active_by_fingerprint(fingerprint)
            if existing is not None:
                # NOTE (F2): severity / labels / annotations are PINNED at first
                # fire. Alertmanager severity escalation (warning -> critical)
                # on the same fingerprint will NOT be reflected here — the
                # re-fire event uses the row's stored severity. Operators
                # tracking severity changes should ensure upstream produces
                # distinct fingerprints per severity tier (e.g., include
                # severity in the labels hashed by compute_fingerprint).
                await alert_repo.update_last_seen(existing.id, ts)
                event = AlertFiringEvent(
                    alert_id=existing.id,
                    fingerprint=fingerprint,
                    source_tool=existing.source_tool,
                    severity=existing.severity,
                    opened_at=existing.opened_at,
                    last_seen_at=ts,
                    labels=existing.labels,
                    annotations=existing.annotations,
                    ts=ts,
                )
            else:
                # The full Alertmanager item payload (startsAt/endsAt/etc.)
                # is preserved on the alert row so the operator can inspect
                # it via GET /api/alerts/{id}; labels/annotations are also
                # stashed inside payload for round-trip via _row_to_alert.
                new_alert = Alert(
                    id="",  # repo allocates uuid7
                    fingerprint=fingerprint,
                    source_tool=source_tool,
                    severity=severity,
                    status=AlertStatus.FIRING,
                    opened_at=ts,
                    last_seen_at=ts,
                    payload=json.loads(payload_json),
                    labels=labels,
                    annotations=annotations,
                )
                # F8: explicit payload_json kept here because the wire payload
                # (startsAt/endsAt/generatorURL/...) is richer than what we
                # synthesise from labels/annotations alone. Pass-through keeps
                # exact round-trip; remove once payload schema is enforced.
                new_id = await alert_repo.insert_firing(new_alert, payload_json=payload_json)
                event = AlertFiringEvent(
                    alert_id=new_id,
                    fingerprint=fingerprint,
                    source_tool=source_tool,
                    severity=severity,
                    opened_at=ts,
                    last_seen_at=ts,
                    labels=labels,
                    annotations=annotations,
                    ts=ts,
                )
            await dispatcher.dispatch(event)
            ingested += 1
            log.info(
                "alerts.ingest.firing",
                fingerprint=fingerprint,
                alert_id=event.alert_id,
            )
        else:  # resolved
            existing = await alert_repo.find_active_by_fingerprint(fingerprint)
            if existing is None:
                log.info(
                    "alerts.ingest.resolved_without_prior_firing",
                    fingerprint=fingerprint,
                )
                continue
            await alert_repo.mark_resolved(existing.id, ts)
            resolved_event = AlertResolvedEvent(
                alert_id=existing.id,
                fingerprint=fingerprint,
                source_tool=existing.source_tool,
                severity=existing.severity,
                resolved_at=ts,
                labels=existing.labels,
                annotations=existing.annotations,
                ts=ts,
            )
            await dispatcher.dispatch(resolved_event)
            ingested += 1
            log.info(
                "alerts.ingest.resolved",
                fingerprint=fingerprint,
                alert_id=existing.id,
            )

    return JSONResponse(
        status_code=202,
        content=IngestResponse(received=received, ingested=ingested).model_dump(mode="json"),
    )


@router.get("", response_model=AlertListResponse)
async def list_alerts(  # noqa: PLR0913 -- explicit Query parameters
    _user: Annotated[User, Depends(require_session())],
    alert_repo: Annotated[AlertRepository, Depends(get_alert_repo)],
    status: Annotated[AlertStatus | None, Query()] = None,
    severity: Annotated[Severity | None, Query()] = None,
    source_tool: Annotated[str | None, Query()] = None,
    fingerprint: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> AlertListResponse:
    """List alerts with optional filters and cursor pagination.

    NOTE: ``total`` is omitted by design — cursor pagination already conveys
    "more available" via ``next_cursor``. Adding a separate COUNT(*) would
    double the per-request DB cost for negligible operator value.
    """
    try:
        items, next_cursor = await alert_repo.list_alerts(
            status=status,
            severity=severity,
            source_tool=source_tool,
            fingerprint=fingerprint,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid cursor: {exc}") from exc
    return AlertListResponse(
        items=[_alert_to_view(a) for a in items],
        next_cursor=next_cursor,
    )


@router.get("/{alert_id}", response_model=AlertDetailResponse)
async def get_alert(
    alert_id: str,
    _user: Annotated[User, Depends(require_session())],
    alert_repo: Annotated[AlertRepository, Depends(get_alert_repo)],
) -> AlertDetailResponse:
    """Return the alert + outcome history + raw payload."""
    alert = await alert_repo.get_alert_by_id(alert_id)
    if alert is None:
        raise NotFoundProblem(message=f"alert not found: {alert_id}")
    outcome_rows = await alert_repo.list_outcomes(alert_id)
    outcomes = [
        OutcomeView(
            outcome=AlertOutcome(row["outcome"]),
            decided_at=str(row["decided_at"]),
            decided_by=row["decided_by"],
        )
        for row in outcome_rows
    ]
    return AlertDetailResponse(
        alert=_alert_to_view(alert),
        outcomes=outcomes,
        payload=alert.payload,
    )


@router.post("/{alert_id}/ack", response_model=AckResponse)
async def ack_alert(
    alert_id: str,
    _body: CommentBody,
    user: Annotated[User, Depends(require_session())],
    alert_repo: Annotated[AlertRepository, Depends(get_alert_repo)],
) -> AckResponse:
    """Ack an alert. Records an outcome row + sets ack_at/ack_by columns."""
    alert = await alert_repo.get_alert_by_id(alert_id)
    if alert is None:
        raise NotFoundProblem(message=f"alert not found: {alert_id}")
    now = utc_now_iso()
    await alert_repo.insert_outcome(alert_id, AlertOutcome.ACKED, decided_by=user.id)
    await alert_repo.set_ack(alert_id, ack_at=now, ack_by=user.id)
    return AckResponse(alert_id=alert_id, ack_at=now)


@router.post("/{alert_id}/dismiss", response_model=DismissResponse)
async def dismiss_alert(
    alert_id: str,
    _body: CommentBody,
    user: Annotated[User, Depends(require_session())],
    alert_repo: Annotated[AlertRepository, Depends(get_alert_repo)],
) -> DismissResponse:
    """Dismiss an alert. Records an outcome row; does NOT modify status.

    A dismissed alert may still be firing — dismissal is operator intent
    ("don't show me this") not lifecycle.
    """
    alert = await alert_repo.get_alert_by_id(alert_id)
    if alert is None:
        raise NotFoundProblem(message=f"alert not found: {alert_id}")
    now = utc_now_iso()
    await alert_repo.insert_outcome(alert_id, AlertOutcome.DISMISSED, decided_by=user.id)
    return DismissResponse(alert_id=alert_id, dismissed_at=now)
