"""pihole_ftl_messages collector — FTL diagnostic messages from /api/info/messages.

Polls Pi-hole v6 ``GET /api/info/messages`` once per 60s and emits:
- 1 api-took gauge              {endpoint="info/messages"}
- 1 total message count gauge   {} (always emitted; 0.0 for empty list)
- 0-N per-type count gauges     {type=<label>} (one per distinct type present)

COUNT vs PER-TYPE SUM: ``homelab_pihole_messages_count`` = len(messages list),
counting ALL list entries including any malformed ones. The per-type series
(``homelab_pihole_messages{type=...}``) group only over well-formed dict entries;
non-dict entries are skipped. As a result, sum(per-type values) may be less than
messages_count when malformed entries are present — this is documented behaviour.

MISSING/NON-STRING type field: falls back to label "unknown".

SCAFFOLDING: feeds alert rules in STAGE-006-016 and Grafana in STAGE-026.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, cast

from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult

M_API_TOOK = "homelab_pihole_api_took_seconds"
M_MESSAGES_COUNT = "homelab_pihole_messages_count"
M_MESSAGES_BY_TYPE = "homelab_pihole_messages"


def _count_by_type(messages: list[object]) -> dict[str, int]:
    """Group well-formed message dicts by their 'type' field.

    Non-dict entries are skipped (malformed entry branch).
    Missing or non-string 'type' falls back to "unknown" label.
    Returns a plain dict[str, int]; keys are present types only.
    """
    counts: dict[str, int] = {}
    for entry in messages:
        if not isinstance(entry, dict):
            # malformed entry — skip; do NOT crash
            continue
        entry_dict = cast("dict[str, object]", entry)
        type_obj = entry_dict.get("type")
        type_label: str = type_obj if isinstance(type_obj, str) else "unknown"
        counts[type_label] = counts.get(type_label, 0) + 1
    return counts


class PiholeFtlMessagesCollector(BaseCollector):
    """Emit FTL diagnostic message counts from GET /api/info/messages.

    Polls once per 60 seconds. Emits:
    - 1  api-took gauge               {endpoint="info/messages"}
    - 1  messages_count gauge         {} (len of messages list; always emitted)
    - 0-N messages-by-type gauges     {type=<label>} (grouped over well-formed)

    COUNT vs PER-TYPE SUM: messages_count = total list length including
    malformed entries. Per-type series group only well-formed dict entries;
    sum(per-type) may be < messages_count when malformed entries are present.

    FAILURE SEMANTICS:
    - ctx.pihole is None → ok=False, errors=["pihole client not configured"],
      metrics_emitted=0.
    - info_messages() returns PiholeError → ok=False, errors=[result.message],
      metrics_emitted=0.
    - payload not a dict → ok=False, errors=["unexpected payload shape"],
      metrics_emitted=1 (api_took already counted).
    - payload["messages"] missing or not a list → ok=False,
      errors=["unexpected payload shape (messages not a list)"],
      metrics_emitted=1 (api_took already counted). messages_count NOT emitted
      (we cannot know the true count; emitting 0 would falsely signal healthy).
    """

    name: ClassVar[str] = "pihole_ftl_messages"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll /api/info/messages, emit gauges, return CollectorResult."""
        start = time.monotonic()

        # Guard: pihole client not configured
        if ctx.pihole is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["pihole client not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        result = await ctx.pihole.info_messages()

        # Guard: transport / auth / HTTP error
        if isinstance(result, PiholeError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[result.message],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        emitted: list[int] = [0]

        # --- api-took (always present when we have successful response) ---
        ctx.vm.write_gauge(M_API_TOOK, result.took_seconds, {"endpoint": result.endpoint})
        emitted[0] += 1

        # Guard: payload shape — must be a dict
        raw_payload: object = result.payload
        if not isinstance(raw_payload, dict):
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted[0],
                errors=["unexpected payload shape"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        payload = cast("dict[str, object]", raw_payload)

        # Guard: messages key must be a list
        messages_obj = payload.get("messages")
        if not isinstance(messages_obj, list):
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted[0],
                errors=["unexpected payload shape (messages not a list)"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        messages = cast("list[object]", messages_obj)

        # --- total count (always emitted; 0.0 for empty list is healthy) ---
        ctx.vm.write_gauge(M_MESSAGES_COUNT, float(len(messages)), {})
        emitted[0] += 1

        # --- per-type breakdown (empty list → no series) ---
        counts = _count_by_type(messages)
        for type_label, count in counts.items():
            ctx.vm.write_gauge(M_MESSAGES_BY_TYPE, float(count), {"type": type_label})
            emitted[0] += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
