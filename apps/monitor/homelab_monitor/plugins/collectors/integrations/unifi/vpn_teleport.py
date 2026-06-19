"""unifi_vpn_teleport collector -- Teleport initialization state.

Polls ``stat/device`` once per 60s tick and emits:

- ``homelab_unifi_teleport_up`` -- label-free gauge, 1.0 when the UDM
  gateway device record contains a non-empty ``teleport_version`` field,
  0.0 otherwise. ALWAYS emitted (never absent) so the TeleportDown alert
  has a stable series to evaluate. Value means "Teleport is initialized on
  the gateway"; it does NOT prove a client can establish a session.
- ``homelab_unifi_teleport_reason{reason=...}`` -- info-gauge always 1.0,
  exactly ONE per run. Reasons: "ok", "not_initialized", "device_not_found",
  "bad_response", "not_configured" (response/state outcomes) plus the
  UnifiError pass-through reasons "unreachable", "timeout", "auth",
  "rate_limited", "http_error".
- ``homelab_unifi_teleport_version{version=...}`` -- info-gauge 1.0, emitted
  ONLY on the ok path when teleport_version is present and non-empty.
- ``homelab_unifi_api_took_seconds{endpoint="stat/device"}`` -- API latency.
  Emitted ONLY when an HTTP response was received (UnifiResponse). NOT emitted
  on UnifiError or when ctx.unifi is None.

NOTE: ``stat/health`` vpn subsystem is NOT polled (always reports
status="unknown" on this firmware). Teleport health is read from
``stat/device`` exclusively.

DIVERGENCE FROM WAN: this is a health collector (like controller_up). The
null-ctx and UnifiError branches emit up=0 + reason BEFORE returning so the
UnifiTeleportDown alert has a stable series to fire on even when unconfigured.
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
_M_UP: Final[str] = "homelab_unifi_teleport_up"
_M_REASON: Final[str] = "homelab_unifi_teleport_reason"
_M_VERSION: Final[str] = "homelab_unifi_teleport_version"
_M_API_TOOK: Final[str] = "homelab_unifi_api_took_seconds"


def _find_teleport_version(data: list[object]) -> str | None:
    """Return the first non-empty ``teleport_version`` string in the device list.

    Skips non-dict entries. Skips entries where ``teleport_version`` is absent,
    not a str, or an empty string. Returns None when no matching entry is found.
    """
    for entry in data:
        if not isinstance(entry, dict):
            continue
        tv = cast("dict[str, object]", entry).get("teleport_version")
        if isinstance(tv, str) and tv:
            return tv
    return None


class UnifiVpnTeleportCollector(BaseCollector):
    """Emit Teleport initialization state from stat/device.

    Scans the device list for a record with a non-empty ``teleport_version``
    field (UDM gateway only). Emits an always-present label-free up gauge plus
    a reason info-gauge so alerting rules have a stable, never-absent series.
    """

    name: ClassVar[str] = "unifi_vpn_teleport"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "unifi"

    async def run(self, ctx: CollectorContext) -> CollectorResult:  # noqa: PLR0911
        """Poll stat/device and emit teleport up gauge + reason + version.

        PLR0911: one early return per distinct health outcome (null-ctx, API
        error, malformed payload, no-devices, not-initialized, ok) keeps each
        reason path flat and readable; collapsing them would obscure the
        outcome-to-reason mapping the UnifiTeleportDown alert depends on.
        """
        start = time.monotonic()

        # Health collector: emit up=0 even when unconfigured so
        # UnifiTeleportDown can fire.
        if ctx.unifi is None:
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_REASON, 1.0, {"reason": "not_configured"})
            return CollectorResult(
                ok=False,
                metrics_emitted=2,
                errors=["unifi client not configured"],
                duration_seconds=time.monotonic() - start,
            )

        resp = await ctx.unifi.stat_device()
        if isinstance(resp, UnifiError):
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_REASON, 1.0, {"reason": resp.reason})
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

        # Narrow payload to a typed dict; return bad_response if not a dict.
        payload = resp.payload
        if not isinstance(payload, dict):
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_REASON, 1.0, {"reason": "bad_response"})
            emitted += 2
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted,
                errors=["unifi stat/device payload not a dict"],
                duration_seconds=time.monotonic() - start,
            )

        payload_dict = cast("dict[str, object]", payload)
        meta = payload_dict.get("meta")
        rc_ok = isinstance(meta, dict) and cast("dict[str, object]", meta).get("rc") == "ok"

        # rc-not-ok folds into bad_response (same reason as the non-dict payload
        # guard above): both mean "the response is not a usable device list".
        if not rc_ok:
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_REASON, 1.0, {"reason": "bad_response"})
            emitted += 2
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted,
                errors=["unifi stat/device meta.rc not ok"],
                duration_seconds=time.monotonic() - start,
            )

        data_obj = payload_dict.get("data")
        if not isinstance(data_obj, list):
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_REASON, 1.0, {"reason": "device_not_found"})
            emitted += 2
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted,
                errors=["unifi stat/device returned no devices"],
                duration_seconds=time.monotonic() - start,
            )

        data = cast("list[object]", data_obj)

        if not data:
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_REASON, 1.0, {"reason": "device_not_found"})
            emitted += 2
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted,
                errors=["unifi stat/device returned no devices"],
                duration_seconds=time.monotonic() - start,
            )

        version = _find_teleport_version(data)

        if version is None:
            ctx.vm.write_gauge(_M_UP, 0.0, {})
            ctx.vm.write_gauge(_M_REASON, 1.0, {"reason": "not_initialized"})
            emitted += 2
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted,
                errors=["unifi gateway reports no teleport_version"],
                duration_seconds=time.monotonic() - start,
            )

        ctx.vm.write_gauge(_M_UP, 1.0, {})
        ctx.vm.write_gauge(_M_REASON, 1.0, {"reason": "ok"})
        ctx.vm.write_gauge(_M_VERSION, 1.0, {"version": version})
        emitted += 3
        return CollectorResult(
            ok=True,
            metrics_emitted=emitted,
            errors=[],
            duration_seconds=time.monotonic() - start,
        )
