"""unifi_controller_up collector -- controller reachability + API latency.

Polls ``stat/sysinfo`` once per 30s tick and emits:

- ``homelab_unifi_up`` -- label-free gauge, 1.0 (fully reachable, authed, valid
  response) or 0.0 (any failure). Never absent -- keeps
  ``homelab_unifi_up == 0`` stable for the UnifiControllerDown alert.
- ``homelab_unifi_up_reason{reason=...}`` -- info-gauge always valued 1.0,
  with exactly ONE series per run labelling the current health reason.
  Possible reasons: "ok", "unreachable", "timeout", "auth", "rate_limited",
  "http_error", "bad_response", "empty_data", "not_configured".
- ``homelab_unifi_api_took_seconds{endpoint="stat/sysinfo"}`` -- API latency.
  Emitted ONLY when an HTTP response was actually received (UnifiResponse
  path). NOT emitted on UnifiError or when ctx.unifi is None.

DIVERGENCE FROM ALARMS: the null-ctx and UnifiError branches emit up=0 +
up_reason BEFORE returning (alarms emits nothing on those paths).  This
ensures the critical UnifiControllerDown alert has a stable series to fire on
even when the integration is unconfigured.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final, cast

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.kernel.unifi.errors import UnifiError

# --- Metric / endpoint constants -------------------------------------------
_M_UP: Final[str] = "homelab_unifi_up"
_M_UP_REASON: Final[str] = "homelab_unifi_up_reason"
_M_API_TOOK: Final[str] = "homelab_unifi_api_took_seconds"
_ENDPOINT: Final[str] = "stat/sysinfo"


def _record_count(payload: dict[str, object]) -> int:
    """Count dict entries in a classic {"data":[...]} payload.

    Returns 0 when ``data`` is not a list or contains no dict entries.
    """
    data_obj = payload.get("data")
    if not isinstance(data_obj, list):
        return 0
    data = cast("list[object]", data_obj)
    return sum(1 for r in data if isinstance(r, dict))


class UnifiControllerUpCollector(BaseCollector):
    """Emit controller reachability (up/down) and API latency from stat/sysinfo.

    Emits an always-present label-free up gauge plus a reason info-gauge so
    alerting rules have a stable, never-absent series to evaluate.
    """

    name: ClassVar[str] = "unifi_controller_up"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=10)
    concurrency_group: ClassVar[str] = "unifi"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll stat/sysinfo and emit up gauge + reason + latency."""
        start = time.monotonic()

        # Health collector: emit up=0 even when unconfigured so
        # UnifiControllerDown can fire.
        if ctx.unifi is None:
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_UP_REASON, 1.0, {"reason": "not_configured"})
            return CollectorResult(
                ok=False,
                metrics_emitted=2,
                errors=["unifi client not configured"],
                duration_seconds=time.monotonic() - start,
            )

        resp = await ctx.unifi.stat_sysinfo()
        if isinstance(resp, UnifiError):
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_UP_REASON, 1.0, {"reason": resp.reason})
            return CollectorResult(
                ok=False,
                metrics_emitted=2,
                errors=[resp.message],
                duration_seconds=time.monotonic() - start,
            )

        emitted = 0

        # HTTP succeeded: latency is valid regardless of payload shape.
        ctx.vm.write_gauge(_M_API_TOOK, resp.took_seconds, {"endpoint": resp.endpoint})
        emitted += 1

        # Narrow the payload to a typed dict once, then check meta.rc.
        payload = resp.payload
        if not isinstance(payload, dict):
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_UP_REASON, 1.0, {"reason": "bad_response"})
            emitted += 2
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted,
                errors=["unifi sysinfo payload not a dict"],
                duration_seconds=time.monotonic() - start,
            )

        payload_dict = cast("dict[str, object]", payload)
        meta = payload_dict.get("meta")
        rc_ok = isinstance(meta, dict) and cast("dict[str, object]", meta).get("rc") == "ok"

        if not rc_ok:
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_UP_REASON, 1.0, {"reason": "bad_response"})
            emitted += 2
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted,
                errors=["unifi sysinfo meta.rc not ok"],
                duration_seconds=time.monotonic() - start,
            )

        if _record_count(payload_dict) == 0:
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_UP_REASON, 1.0, {"reason": "empty_data"})
            emitted += 2
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted,
                errors=["unifi sysinfo returned no data"],
                duration_seconds=time.monotonic() - start,
            )

        ctx.vm.write_gauge(_M_UP, 1.0, {})
        ctx.vm.write_gauge(_M_UP_REASON, 1.0, {"reason": "ok"})
        emitted += 2
        return CollectorResult(
            ok=True,
            metrics_emitted=emitted,
            errors=[],
            duration_seconds=time.monotonic() - start,
        )
