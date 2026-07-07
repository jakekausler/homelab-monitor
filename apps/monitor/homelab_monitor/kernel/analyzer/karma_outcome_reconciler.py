"""KarmaOutcomeReconciler — hourly Alertmanager → alert_outcomes reconciliation.

STAGE-010-003. Retrofit for EPIC-001..009 + 017: Karma (the embedded alert-
lifecycle UI) posts ack/silence actions directly to Alertmanager, never
touching our monitor, so ``alert_outcomes`` was empty in prod despite 168k
firing alerts. This reconciler closes the loop.

Two phases per tick (both must succeed to advance ``last_reconciliation_at``):

1. SILENCE phase — ``GET /api/v2/silences`` from AM; for each silence, resolve
   its target ``alerts.fingerprint`` (either a direct ``fingerprint`` matcher
   or by reconstructing the label set + calling ``compute_fingerprint``). If
   exactly one ``alerts`` row matches, write ``AlertOutcome.ACKED`` with
   ``decided_by="karma"``, ``decided_at=silence.startsAt`` via
   ``insert_outcome_if_absent`` (silent on repeat).

2. RESOLUTION phase — query our own ``alerts`` table for rows resolved since
   ``last_reconciliation_at``; write ``AlertOutcome.AUTO_RESOLVED`` with
   ``decided_by="reconciler"``, ``decided_at=alert.resolved_at`` for each.

If the SILENCE phase's AM call raises ``httpx.HTTPError``, the tick is marked
failed and the RESOLUTION phase is SKIPPED — we do not advance
``last_reconciliation_at`` so the next healthy tick sees the same resolutions.

Idempotent by construction: ``insert_outcome_if_absent`` short-circuits on the
``uq_alert_outcomes_alert_id_outcome`` UNIQUE index (migration 0053).

Domain metrics emitted per tick:

- ``homelab_karma_reconciler_silences_matched_total`` (counter, no labels)
- ``homelab_karma_reconciler_silences_skipped_total{reason=...}`` where
  ``reason`` is one of: ``unsupported_matcher``, ``no_match``,
  ``ambiguous_match``, ``invalid_startsAt``.
- ``homelab_karma_reconciler_resolutions_matched_total`` (counter, no labels)
- ``homelab_karma_reconciler_last_reconciliation_lag_seconds`` (gauge — seconds
  since the previous successful reconciliation, or ``0.0`` on first ever run).

The scheduler emits ``homelab_collector_run_*`` self-metrics automatically.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar, cast

import httpx
from sqlalchemy import text

from homelab_monitor.kernel.alertmanager.silences_client import (
    Matcher,
    Silence,
    SilencesClient,
)
from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import AlertOutcome
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

# app_settings key holding the ISO-8601 UTC timestamp of the last successful
# tick. Only advanced on a fully-successful tick (both phases exception-free).
_LAST_RECONCILIATION_KEY: str = "analyzer.last_reconciliation_at"


def _compute_fingerprint_from_labels(labels: dict[str, str]) -> str:
    """Recompute the SHA-256 alerts fingerprint from a label set.

    Mirrors ``kernel/alerts/fingerprinting.compute_fingerprint`` for the
    label-hash path (that helper is bound to an ``AlertmanagerV2AlertItem``;
    we need the raw-labels form for silence matchers).
    """
    sorted_labels = json.dumps(labels, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(sorted_labels.encode("utf-8")).hexdigest()


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, attaching UTC when naive.

    Alertmanager emits ``startsAt`` with a trailing ``Z`` which
    ``datetime.fromisoformat`` accepts on Python 3.11+.
    """
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class KarmaOutcomeReconciler(BaseCollector):
    """Periodic reconciler: Karma silences → ACKED, own resolutions → AUTO_RESOLVED."""

    name: ClassVar[str] = "karma_outcome_reconciler"
    interval: ClassVar[timedelta] = timedelta(seconds=3600)
    timeout: ClassVar[timedelta] = timedelta(seconds=300)
    concurrency_group: ClassVar[str] = "analyzer"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run one reconciler tick: silences → ACKED, resolutions → AUTO_RESOLVED."""
        start = time.monotonic()
        errors: list[str] = []
        now = datetime.now(UTC)

        alert_repo = AlertRepository(ctx.db)
        settings_repo = AppSettingsRepository(ctx.db)

        am_url = os.environ.get(
            "HOMELAB_MONITOR_ALERTMANAGER_URL",
            "http://alertmanager:9093",
        )
        silences_client = SilencesClient(
            am_url=am_url,
            http_client=ctx.http,
            log=ctx.log,
        )

        # Read the last-reconciliation anchor BEFORE any writes so the
        # RESOLUTION phase can bound its scan and the lag gauge can be computed.
        last_iso = await settings_repo.get(_LAST_RECONCILIATION_KEY)

        # --- SILENCE phase ---
        try:
            silences = await silences_client.list_silences()
        except httpx.HTTPError as exc:
            ctx.log.warning(
                "karma_reconciler.alertmanager_unreachable",
                am_url=am_url,
                error=str(exc),
            )
            errors.append(f"alertmanager_unreachable: {exc}")
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=errors,
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        silences_matched = 0
        for silence in silences:
            outcome_written = await self._process_silence(
                silence=silence,
                alert_repo=alert_repo,
                ctx=ctx,
            )
            if outcome_written:
                silences_matched += 1

        # --- RESOLUTION phase ---
        resolutions_matched = await self._process_resolutions(
            alert_repo=alert_repo,
            db=ctx.db,
            last_iso=last_iso,
            ctx=ctx,
        )

        # --- Metrics + settings on successful tick ---
        ctx.vm.write_counter(
            "homelab_karma_reconciler_silences_matched_total",
            float(silences_matched),
            {},
        )
        ctx.vm.write_counter(
            "homelab_karma_reconciler_resolutions_matched_total",
            float(resolutions_matched),
            {},
        )

        lag_seconds = 0.0
        if last_iso is not None:
            try:
                lag_seconds = (now - _parse_iso(last_iso)).total_seconds()
            except ValueError:
                # Corrupted timestamp — treat as first-run.
                lag_seconds = 0.0
        ctx.vm.write_gauge(
            "homelab_karma_reconciler_last_reconciliation_lag_seconds",
            lag_seconds,
            {},
        )

        await settings_repo.set(_LAST_RECONCILIATION_KEY, now.isoformat())

        ctx.log.info(
            "karma_reconciler.completed",
            silences_matched=silences_matched,
            resolutions_matched=resolutions_matched,
            lag_seconds=lag_seconds,
        )

        # metrics_emitted tracks new outcome rows (DB writes), matching the
        # convention used by cron_run_reconciler / container_healthcheck_reconciler.
        return CollectorResult(
            ok=True,
            metrics_emitted=silences_matched + resolutions_matched,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    async def _process_silence(  # noqa: PLR0911
        self,
        *,
        silence: Silence,
        alert_repo: AlertRepository,
        ctx: CollectorContext,
    ) -> bool:
        """Handle one silence. Returns True iff a NEW ACKED outcome was written."""
        # Widen the runtime type so pyright allows the defensive isinstance
        # check. The Silence TypedDict declares status as ``dict[str, str]``,
        # but the value came from ``resp.json()`` at the wire — so real-world
        # payloads may include lists / strings / None here. STAGE-010-003
        # Finding 8: treat non-dict status as unknown state (silence ignored).
        status_obj_raw: object = cast(object, silence.get("status", {}))
        status_obj: dict[str, str] = (
            cast(dict[str, str], status_obj_raw) if isinstance(status_obj_raw, dict) else {}
        )
        state = status_obj.get("state")
        if state not in ("active", "expired"):
            # Pending silences and any other state are ignored.
            return False

        matchers = silence.get("matchers", [])
        if not matchers:
            self._skip_silence(ctx, "unsupported_matcher", silence)
            return False

        # Special case: single ``fingerprint`` matcher — use verbatim.
        target_fp: str | None = None
        fp_matcher = self._extract_fingerprint_matcher(matchers)
        if fp_matcher is not None:
            target_fp = fp_matcher
        else:
            # General case: reconstruct label set from equality matchers.
            labels = self._extract_equality_labels(matchers)
            if labels is None:
                self._skip_silence(ctx, "unsupported_matcher", silence)
                return False
            target_fp = _compute_fingerprint_from_labels(labels)

        # Look up matching alert rows.
        rows = await ctx.db.fetch_all(
            text("SELECT id FROM alerts WHERE fingerprint = :fp"),
            {"fp": target_fp},
        )
        if len(rows) == 0:
            self._skip_silence(ctx, "no_match", silence, target_fp=target_fp)
            return False
        if len(rows) > 1:
            self._skip_silence(
                ctx,
                "ambiguous_match",
                silence,
                target_fp=target_fp,
                match_count=len(rows),
            )
            return False

        alert_id = str(rows[0].id)
        starts_at = silence.get("startsAt")
        if not isinstance(starts_at, str) or not starts_at:
            self._skip_silence(ctx, "invalid_startsAt", silence)
            return False

        # Prefer updatedAt (closer to the human-ack time) over startsAt
        # (which for scheduled silences may be well before the operator acted).
        # STAGE-010-003 Finding 4.
        updated_at_val = silence.get("updatedAt")
        decided_at_val = (
            updated_at_val if isinstance(updated_at_val, str) and updated_at_val else starts_at
        )
        wrote = await alert_repo.insert_outcome_if_absent(
            alert_id=alert_id,
            outcome=AlertOutcome.ACKED,
            decided_by="karma",
            decided_at=decided_at_val,
        )
        if wrote:
            ctx.log.info(
                "karma_reconciler.silence_matched",
                alert_id=alert_id,
                silence_id=str(silence.get("id", "")),
                fingerprint=target_fp,
            )
            return True
        # Idempotent skip: outcome already existed. Not a match this tick;
        # not a skip either. Debug log only.
        ctx.log.debug(
            "karma_reconciler.silence_already_acked",
            alert_id=alert_id,
            silence_id=str(silence.get("id", "")),
        )
        return False

    @staticmethod
    def _extract_fingerprint_matcher(matchers: list[Matcher]) -> str | None:
        """If ``matchers`` is EXACTLY one equality matcher on ``fingerprint``,
        return its value verbatim; else None.

        Design intent (STAGE-010-003 Finding 2): only short-circuit when the
        silence uniquely targets a fingerprint. Silences that mix a fingerprint
        matcher with additional label matchers are treated as label-based
        reconstruction (which will typically fail to match since our fingerprint
        column doesn't include the extra label constraints — correct behavior).

        Matches AM's own convention for silence-by-fingerprint. Requires the
        matcher to be ``isEqual=true, isRegex=false``.
        """
        if len(matchers) != 1:
            return None
        m = matchers[0]
        if (
            m.get("name") == "fingerprint"
            and m.get("isEqual", True) is True
            and m.get("isRegex", False) is False
        ):
            value = m.get("value")
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _extract_equality_labels(matchers: list[Matcher]) -> dict[str, str] | None:
        """Reconstruct the alert label set from equality matchers.

        Returns None if ANY matcher is non-equality (regex or negated),
        because such a silence cannot be uniquely mapped to a single
        ``alerts.fingerprint`` — it targets an alert *class*, not an instance.
        Returns the label dict otherwise.
        """
        labels: dict[str, str] = {}
        for m in matchers:
            if m.get("isRegex", False) is True:
                return None
            if m.get("isEqual", True) is False:
                return None
            name = m.get("name")
            value = m.get("value")
            if not isinstance(name, str) or not isinstance(value, str):
                return None
            labels[name] = value
        return labels

    @staticmethod
    def _skip_silence(
        ctx: CollectorContext,
        reason: str,
        silence: Silence,
        **extra: object,
    ) -> None:
        """Emit the skipped-total counter with the reason label + log."""
        ctx.vm.write_counter(
            "homelab_karma_reconciler_silences_skipped_total",
            1.0,
            {"reason": reason},
        )
        log_fn = ctx.log.warning if reason == "ambiguous_match" else ctx.log.debug
        log_fn(
            "karma_reconciler.silence_skipped",
            reason=reason,
            silence_id=str(silence.get("id", "")),
            **extra,
        )

    async def _process_resolutions(
        self,
        *,
        alert_repo: AlertRepository,
        db: SqliteRepository,
        last_iso: str | None,
        ctx: CollectorContext,
    ) -> int:
        """Scan our own ``alerts`` table for rows resolved since ``last_iso``.

        First-ever run (``last_iso is None``) scans all ``resolved_at IS NOT NULL``
        rows. Subsequent runs bound the scan.

        Returns the number of NEW AUTO_RESOLVED outcomes written this tick.
        """
        if last_iso is None:
            rows = await db.fetch_all(
                text("SELECT id, resolved_at FROM alerts WHERE resolved_at IS NOT NULL"),
                {},
            )
        else:
            rows = await db.fetch_all(
                text(
                    "SELECT id, resolved_at FROM alerts "
                    "WHERE resolved_at IS NOT NULL AND resolved_at > :last"
                ),
                {"last": last_iso},
            )

        matched = 0
        for row in rows:
            alert_id = str(row.id)
            resolved_at = str(row.resolved_at)
            wrote = await alert_repo.insert_outcome_if_absent(
                alert_id=alert_id,
                outcome=AlertOutcome.AUTO_RESOLVED,
                decided_by="reconciler",
                decided_at=resolved_at,
            )
            if wrote:
                matched += 1
                ctx.log.info(
                    "karma_reconciler.resolution_matched",
                    alert_id=alert_id,
                    resolved_at=resolved_at,
                )
        return matched


__all__ = ["KarmaOutcomeReconciler"]
