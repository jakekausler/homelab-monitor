"""GET /api/integrations/home-assistant/summary — HA panel summary counts.

Sourced from VictoriaMetrics INSTANT queries (``/api/v1/query``) via the shared
``vm_query`` helper. NOT a SQLite cache and NOT a live HA re-query.

Failure contract:
  - VM unreachable / query error -> 502 ``upstream_unavailable`` (matches
    ``metrics_range``; NOT a 200-with-zeros).
  - HA down but VM up -> 200 with ``ha_up=false`` + last-known counts.
  - ``last_seen`` is derived from the ``homelab_ha_up`` sample timestamp; ``None``
    when ``homelab_ha_up`` returns no data.

Missing-series semantics: ``count(metric == 0)`` returns NO sample when zero
series match, so every count field defaults to 0 when its query's vector is
empty (handled by ``vm_query.vm_count``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.api.dependencies import (
    get_http_client,
    get_vm_url,
    require_session,
)
from homelab_monitor.kernel.api.vm_query import first_sample, vm_count, vm_instant_query
from homelab_monitor.kernel.auth.models import User

router = APIRouter(prefix="/integrations/home-assistant", tags=["integrations"])

# Battery thresholds — MUST match the vmalert rules exactly.
_BATTERY_CRITICAL_BELOW = 10
_BATTERY_LOW_FLOOR = _BATTERY_CRITICAL_BELOW  # no gap/overlap — must equal _BATTERY_CRITICAL_BELOW
_BATTERY_LOW_CEIL = 20

# Instant-query expressions. Keyed by response field for readability.
_Q_ENTITIES_TOTAL = "count(homelab_ha_entity_available)"
_Q_ENTITIES_AVAILABLE = "count(homelab_ha_entity_available == 1)"
_Q_ENTITIES_UNAVAILABLE = "count(homelab_ha_entity_available == 0)"
_Q_BATTERY_CRITICAL = f"count(homelab_ha_battery_level < {_BATTERY_CRITICAL_BELOW})"
_Q_BATTERY_LOW = (
    f"count(homelab_ha_battery_level >= {_BATTERY_LOW_FLOOR} "
    f"and homelab_ha_battery_level < {_BATTERY_LOW_CEIL})"
)
_Q_UPDATES_AVAILABLE = "count(homelab_ha_update_available == 1)"
_Q_UPDATES_TOTAL = "count(homelab_ha_update_available)"
_Q_CONFIG_ENTRIES_LOADED = "count(homelab_ha_config_entry_loaded == 1)"
_Q_CONFIG_ENTRIES_ERROR = "count(homelab_ha_config_entry_setup_error == 1)"
_Q_REPAIRS = "count(homelab_ha_repair_issue == 1)"
_Q_NOTIFICATIONS = "count(homelab_ha_persistent_notification == 1)"
_Q_HA_UP = "homelab_ha_up"


class HaEntitiesSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total: int
    available: int
    unavailable: int


class HaBatterySummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    low: int
    critical: int


class HaUpdatesSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    available: int
    total: int


class HaConfigEntriesSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    loaded: int
    error: int


class HaSummaryResponse(BaseModel):
    """Aggregated HA panel summary, sourced from VictoriaMetrics instant queries."""

    model_config = ConfigDict(extra="ignore")

    entities: HaEntitiesSummary
    battery: HaBatterySummary
    updates: HaUpdatesSummary
    config_entries: HaConfigEntriesSummary
    repairs: int
    notifications: int
    ha_up: bool
    # ISO-8601 UTC string of the homelab_ha_up sample timestamp; None when absent.
    last_seen: str | None


@router.get("/summary", response_model=HaSummaryResponse)
async def get_ha_summary(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> HaSummaryResponse:
    """Return HA panel summary counts from VictoriaMetrics instant queries.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Each count defaults to 0 when its instant query returns an empty vector
    (``count(metric == 0)`` yields no sample when zero series match). Any VM
    transport/query failure surfaces as 502 ``upstream_unavailable`` (via the
    shared ``vm_query`` helper) rather than a 200-with-zeros response.
    """
    # Issue all 12 VM instant queries concurrently.
    # Inner gather: 11 homogeneous vm_count coroutines -> tuple[int, ...].
    # Outer gather: inner group + ha_up sample-list query -> fully concurrent.
    # Default return_exceptions=False: first VM failure raises HttpProblem(502)
    # and propagates immediately — preserves the 502 contract.
    (
        (
            entities_total,
            entities_available,
            entities_unavailable,
            battery_critical,
            battery_low,
            updates_available,
            updates_total,
            config_loaded,
            config_error,
            repairs,
            notifications,
        ),
        ha_up_samples,
    ) = await asyncio.gather(
        asyncio.gather(
            vm_count(http_client, vm_url, _Q_ENTITIES_TOTAL),
            vm_count(http_client, vm_url, _Q_ENTITIES_AVAILABLE),
            vm_count(http_client, vm_url, _Q_ENTITIES_UNAVAILABLE),
            vm_count(http_client, vm_url, _Q_BATTERY_CRITICAL),
            vm_count(http_client, vm_url, _Q_BATTERY_LOW),
            vm_count(http_client, vm_url, _Q_UPDATES_AVAILABLE),
            vm_count(http_client, vm_url, _Q_UPDATES_TOTAL),
            vm_count(http_client, vm_url, _Q_CONFIG_ENTRIES_LOADED),
            vm_count(http_client, vm_url, _Q_CONFIG_ENTRIES_ERROR),
            vm_count(http_client, vm_url, _Q_REPAIRS),
            vm_count(http_client, vm_url, _Q_NOTIFICATIONS),
        ),
        vm_instant_query(http_client, vm_url, _Q_HA_UP),
    )

    # homelab_ha_up: read the scalar value AND its sample timestamp for last_seen.
    ha_up_sample = first_sample(ha_up_samples)
    ha_up = False
    last_seen: str | None = None
    if ha_up_sample is not None:
        last_seen = datetime.fromtimestamp(ha_up_sample.ts, tz=UTC).isoformat()
        try:
            ha_up = float(ha_up_sample.value_str) == 1.0
        except (ValueError, TypeError):
            ha_up = False

    return HaSummaryResponse(
        entities=HaEntitiesSummary(
            total=entities_total,
            available=entities_available,
            unavailable=entities_unavailable,
        ),
        battery=HaBatterySummary(low=battery_low, critical=battery_critical),
        updates=HaUpdatesSummary(available=updates_available, total=updates_total),
        config_entries=HaConfigEntriesSummary(loaded=config_loaded, error=config_error),
        repairs=repairs,
        notifications=notifications,
        ha_up=ha_up,
        last_seen=last_seen,
    )
