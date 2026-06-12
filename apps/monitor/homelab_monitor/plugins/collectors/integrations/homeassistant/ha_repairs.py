"""ha_repairs collector — active repair issues from HA (STAGE-005-011).

Each tick takes a ONE-SHOT ``repairs/list_issues`` snapshot over the injected
HA WebSocket client (NO event subscription) and emits one cardinality-capped
gauge family:

- ``homelab_ha_repair_issue{domain,issue_id,severity}`` — 1.0 for each ACTIVE,
  NON-IGNORED issue.

Issues where ``active`` is explicitly ``False`` or ``ignored`` is ``True`` are
silently excluded. Free-text fields (translation_key, description,
learn_more_url, etc.) are NEVER emitted as labels.

The WS client is injected by the FastAPI lifespan AFTER construction (the
``HaConfigEntryCollector._ws`` precedent), so ``self._ws`` is None until the
lifespan wires it. A None / not-connected client makes the tick a FAILED run
(``ok=False``) — transient; the scheduler / FailureBudget handle recovery.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar, Final, cast

from homelab_monitor.kernel.config import load_cardinality_caps_config
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.metrics.cardinality import CappedEmitter
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult

if TYPE_CHECKING:
    from homelab_monitor.kernel.ha.websocket import HomeAssistantWebsocketClient
    from homelab_monitor.kernel.plugins.context import CollectorContext

# Metric family name.
M_REPAIR_ISSUE: Final[str] = "homelab_ha_repair_issue"

# WS command for the one-shot snapshot.
_WS_COMMAND: Final[str] = "repairs/list_issues"


def _extract_issues(result: dict[str, object] | list[object]) -> list[object]:
    """Defensively extract the issues list from a ``send_command`` result.

    Handles:
    (a) bare list — return as-is.
    (b) dict wrapping the list under ``issues`` — return that list.
    (c) any other dict (e.g. the ``{}`` degenerate) — return [].
    """
    payload: object = result  # widen: runtime value may be a list.
    if isinstance(payload, list):
        return payload
    issues_dict = payload
    candidate = issues_dict.get("issues")
    if isinstance(candidate, list):
        return cast("list[object]", candidate)
    return []


def _issue_labels(issue: object) -> dict[str, str] | None:
    """Build the {domain, issue_id, severity} label-set for one issue, or None to SKIP.

    Skip when:
    - ``issue`` is not a dict.
    - ``active`` is explicitly False (missing → treat as active → emit).
    - ``ignored`` is True (missing → treat as not-ignored → emit).
    - ``domain`` is missing, empty, or non-str.
    - ``issue_id`` is missing, empty, or non-str.

    ``severity`` defaults to ``"unknown"`` when missing or non-str.
    """
    if not isinstance(issue, dict):
        return None
    issue_dict = cast("dict[str, object]", issue)

    # active filter: skip only when explicitly False.
    active_obj = issue_dict.get("active")
    if active_obj is False:
        return None

    # ignored filter: skip when explicitly True.
    ignored_obj = issue_dict.get("ignored")
    if ignored_obj is True:
        return None

    # domain: required, non-empty str.
    domain_obj = issue_dict.get("domain")
    domain = domain_obj if isinstance(domain_obj, str) else ""
    if not domain:
        return None

    # issue_id: required, non-empty str.
    issue_id_obj = issue_dict.get("issue_id")
    issue_id = issue_id_obj if isinstance(issue_id_obj, str) else ""
    if not issue_id:
        return None

    # severity: optional, defaults to "unknown".
    severity_obj = issue_dict.get("severity")
    severity = severity_obj if isinstance(severity_obj, str) and severity_obj else "unknown"

    return {"domain": domain, "issue_id": issue_id, "severity": severity}


class HaRepairsCollector(BaseCollector):
    """Emit per-repair-issue gauges from an HA WS snapshot."""

    name: ClassVar[str] = "ha_repairs"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "homeassistant"

    def __init__(self) -> None:
        """Construct with no WS client; the lifespan injects ``self._ws``."""
        super().__init__()
        self._ws: HomeAssistantWebsocketClient | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Snapshot repair issues over the WS and emit the gauge family."""
        start = time.monotonic()

        if self._ws is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["ha websocket not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )
        if not self._ws.connected:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["ha websocket not connected"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        result = await self._ws.send_command(_WS_COMMAND)
        if isinstance(result, HaError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[result.message],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        issues = _extract_issues(result)

        observations: list[tuple[dict[str, str], float]] = []
        for issue in issues:
            labels = _issue_labels(issue)
            if labels is None:
                continue
            observations.append((labels, 1.0))

        caps = load_cardinality_caps_config()
        cap = caps.cap_for(M_REPAIR_ISSUE)

        events: list[CollectorEvent] = []
        emitter = CappedEmitter(writer=ctx.vm, events=events)
        survivors = emitter.emit_family(M_REPAIR_ISSUE, cap, observations)

        # emit_family writes ONE drop gauge -> +1 for the single family.
        metrics_emitted = survivors + 1

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
