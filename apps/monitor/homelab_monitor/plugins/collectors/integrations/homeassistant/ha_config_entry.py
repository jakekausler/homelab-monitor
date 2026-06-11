"""ha_config_entry collector — per-config-entry load state from HA (STAGE-005-010).

FIRST WebSocket-consuming collector. Each tick takes a ONE-SHOT
``config_entries/get`` snapshot over the injected HA WebSocket client (NO event
subscription — see Design D-WS-TO-METRIC-BRIDGE) and emits two cardinality-capped
1/0 gauge families per config entry:

- ``homelab_ha_config_entry_loaded{domain, title}`` — 1.0 iff ``state == "loaded"``.
- ``homelab_ha_config_entry_setup_error{domain, title}`` — 1.0 iff state is one of
  ``setup_error`` / ``setup_retry`` / ``migration_error`` / ``failed_unload``.

All other states (``not_loaded`` / ``setup_in_progress`` / unknown) yield (0.0, 0.0)
for both families. The ``reason`` field is panel-only and is NEVER emitted as a label.

The WS client is injected by the FastAPI lifespan AFTER construction (the
``DockerSocketCollector._client`` precedent), so ``self._ws`` is None until the
lifespan wires it. A None / not-connected client makes the tick a FAILED run
(``ok=False``) — transient; the scheduler / FailureBudget handle recovery.

Parse note (D-WS-CONFIG-ENTRY-PARSE): ``send_command`` is typed ``dict | HaError``
but HA's ``config_entries/get`` returns a top-level JSON array. The current WS
client's ``_result_payload`` collapses a non-dict result to ``{}``, so today the
list path is unreachable at runtime; the defensive parse still handles a bare list
(future-proof) and a dict-wrapped list, narrowing pyright-cleanly via ``object``.
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

# Metric family names (referenced by both the cap lookup and the emit).
M_CONFIG_ENTRY_LOADED: Final[str] = "homelab_ha_config_entry_loaded"
M_CONFIG_ENTRY_SETUP_ERROR: Final[str] = "homelab_ha_config_entry_setup_error"

# HA config-entry states (see homeassistant.config_entries.ConfigEntryState).
_STATE_LOADED: Final[str] = "loaded"
_ERROR_STATES: Final[frozenset[str]] = frozenset(
    {"setup_error", "setup_retry", "migration_error", "failed_unload"}
)

# WS command for the one-shot snapshot.
_WS_COMMAND: Final[str] = "config_entries/get"


def _state_gauges(state: str) -> tuple[float, float]:
    """Map an HA config-entry state to (loaded, setup_error) gauge values.

    The SINGLE place the state enum is interpreted:
      - ``loaded``                              -> (1.0, 0.0)
      - error states (setup_error/setup_retry/
        migration_error/failed_unload)          -> (0.0, 1.0)
      - everything else (not_loaded /
        setup_in_progress / unknown / "")        -> (0.0, 0.0)
    """
    loaded = 1.0 if state == _STATE_LOADED else 0.0
    setup_error = 1.0 if state in _ERROR_STATES else 0.0
    return (loaded, setup_error)


def _extract_entries(result: dict[str, object] | list[object]) -> list[object]:
    """Defensively extract the config-entries list from a ``send_command`` result.

    ``send_command`` is typed ``dict[str, object]`` but HA's ``config_entries/get``
    returns a top-level JSON array. Handle (a) a bare list (future-proof — the
    current WS client collapses it to ``{}``, but a later fix may pass it through),
    (b) a dict wrapping the list under ``entries`` / ``config_entries``, and
    (c) any other dict (today's ``{}`` degenerate) -> empty list.
    """
    payload: object = result  # widen: runtime value may be a list (WS type lie).
    if isinstance(payload, list):
        return payload
    entries_dict = payload
    candidate = entries_dict.get("entries")
    if candidate is None:
        candidate = entries_dict.get("config_entries")
    if isinstance(candidate, list):
        return cast("list[object]", candidate)
    return []


def _entry_labels(entry: object) -> dict[str, str] | None:
    """Build the {domain, title} label-set for one entry, or None to SKIP it.

    Returns None when ``entry`` is not a dict or has no usable (non-empty str)
    ``domain`` — a config entry without a domain is unusable. ``title`` defaults
    to "" when missing or non-str.
    """
    if not isinstance(entry, dict):
        return None
    entry_dict = cast("dict[str, object]", entry)
    domain_obj = entry_dict.get("domain")
    domain = domain_obj if isinstance(domain_obj, str) else ""
    if not domain:
        return None
    title_obj = entry_dict.get("title")
    title = title_obj if isinstance(title_obj, str) else ""
    return {"domain": domain, "title": title}


def _entry_state(entry: object) -> str:
    """Return the entry's ``state`` as a str ("" when missing or non-str)."""
    state_obj = cast("dict[str, object]", entry).get("state")
    return state_obj if isinstance(state_obj, str) else ""


class HaConfigEntryCollector(BaseCollector):
    """Emit per-config-entry loaded / setup-error gauges from an HA WS snapshot."""

    name: ClassVar[str] = "ha_config_entry"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "homeassistant"

    def __init__(self) -> None:
        """Construct with no WS client; the lifespan injects ``self._ws``."""
        super().__init__()
        self._ws: HomeAssistantWebsocketClient | None = None

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Snapshot config entries over the WS and emit the two gauge families."""
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

        entries = _extract_entries(result)

        loaded_obs: list[tuple[dict[str, str], float]] = []
        error_obs: list[tuple[dict[str, str], float]] = []
        for entry in entries:
            labels = _entry_labels(entry)
            if labels is None:
                continue
            loaded_val, error_val = _state_gauges(_entry_state(entry))
            loaded_obs.append((labels, loaded_val))
            error_obs.append((labels, error_val))

        caps = load_cardinality_caps_config()
        loaded_cap = caps.cap_for(M_CONFIG_ENTRY_LOADED)
        error_cap = caps.cap_for(M_CONFIG_ENTRY_SETUP_ERROR)

        events: list[CollectorEvent] = []
        emitter = CappedEmitter(writer=ctx.vm, events=events)
        survivors_loaded = emitter.emit_family(M_CONFIG_ENTRY_LOADED, loaded_cap, loaded_obs)
        survivors_error = emitter.emit_family(M_CONFIG_ENTRY_SETUP_ERROR, error_cap, error_obs)

        # Each emit_family call writes ONE drop gauge -> +2 for the two families.
        metrics_emitted = survivors_loaded + survivors_error + 2

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
