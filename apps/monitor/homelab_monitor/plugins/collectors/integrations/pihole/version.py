"""pihole_version collector — version strings and update availability from /api/info/version.

Polls Pi-hole v6 ``GET /api/info/version`` once per 3600s and emits:
- 1 api-took gauge              {endpoint="info/version"}
- 0-N version_info gauges       {component=<c>, version=<local_str>} (value=1.0)
  Emitted whenever local version string is present. One per component in the payload.
- 0-N update_available gauges   {component=<c>} (1.0 if local != remote, else 0.0)
  Emitted ONLY when BOTH local and remote version strings are present. Omitted
  (not emitted as 0) when either side is missing — "unknown" is never falsely 0.

SHAPE RULES:
- core / web / ftl: component object has {"local": {"version": "<str>", ...},
  "remote": {"version": "<str>", ...}, ...}. Version string extracted via dict
  sub-key "version".
- docker: component object has {"local": "<str>", "remote": "<str>", ...}.
  Version string is the bare string value itself.
Branch dispatch is isinstance(v, str) vs isinstance(v, dict) — NOT component-name-driven.

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
M_UPDATE_AVAILABLE = "homelab_pihole_update_available"
M_VERSION_INFO = "homelab_pihole_version_info"


def _version_str(v: object) -> str | None:
    """Extract a version string from a component local/remote value.

    Handles two shapes:
    - str  (docker bare-string): return v if non-empty/non-whitespace, else None.
    - dict (core/web/ftl object): read v["version"] if a non-empty str, else None.
    - anything else: None.
    """
    if isinstance(v, str):
        return v if v.strip() else None
    if isinstance(v, dict):
        v_dict = cast("dict[str, object]", v)
        version_val = v_dict.get("version")
        if isinstance(version_val, str) and version_val.strip():
            return version_val
        return None
    return None


def _extract_versions(component_obj: object) -> tuple[str | None, str | None]:
    """Return (local_version, remote_version) strings from a per-component value.

    component_obj must be a dict with "local" and "remote" keys. If it is not a
    dict, or the keys are absent, the corresponding slot is None. Version string
    extraction delegates to _version_str for shape dispatch.
    """
    if not isinstance(component_obj, dict):
        return (None, None)
    comp = cast("dict[str, object]", component_obj)
    local_str = _version_str(comp.get("local"))
    remote_str = _version_str(comp.get("remote"))
    return (local_str, remote_str)


class PiholeVersionCollector(BaseCollector):
    """Emit Pi-hole version strings and update availability from GET /api/info/version.

    Polls once per 3600 seconds. Emits:
    - 1  api-took gauge              {endpoint="info/version"}
    - 0-N version_info gauges        {component=<c>, version=<local_str>} value=1.0
    - 0-N update_available gauges    {component=<c>} (1.0=update available, 0.0=current)

    FAILURE SEMANTICS:
    - ctx.pihole is None → ok=False, errors=["pihole client not configured"],
      metrics_emitted=0.
    - info_version() returns PiholeError → ok=False, errors=[result.message],
      metrics_emitted=0.
    - payload not a dict → ok=False, errors=["unexpected payload shape"],
      metrics_emitted=1 (api_took already counted).
    - payload["version"] missing or not a dict → ok=False,
      errors=["unexpected payload shape (version not a dict)"],
      metrics_emitted=1 (api_took already counted). No component metrics emitted.
    """

    name: ClassVar[str] = "pihole_version"
    interval: ClassVar[timedelta] = timedelta(seconds=3600)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll /api/info/version, emit gauges, return CollectorResult."""
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

        result = await ctx.pihole.info_version()

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

        # --- api-took (always present when we have a successful response) ---
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

        # Guard: "version" key must be a dict
        version_obj = payload.get("version")
        if not isinstance(version_obj, dict):
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted[0],
                errors=["unexpected payload shape (version not a dict)"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        version_map = cast("dict[str, object]", version_obj)

        # --- per-component version_info + update_available ---
        for component, comp_obj in version_map.items():
            local_str, remote_str = _extract_versions(comp_obj)

            # version_info: emitted whenever local version is present
            # (decoupled from remote)
            if local_str is not None:
                ctx.vm.write_gauge(
                    M_VERSION_INFO,
                    1.0,
                    {"component": component, "version": local_str},
                )
                emitted[0] += 1

            # update_available: emitted ONLY when both local and remote are known
            # Missing either side → omit (never emit a false 0 for "unknown")
            if local_str is not None and remote_str is not None:
                update = 1.0 if local_str != remote_str else 0.0
                ctx.vm.write_gauge(M_UPDATE_AVAILABLE, update, {"component": component})
                emitted[0] += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
