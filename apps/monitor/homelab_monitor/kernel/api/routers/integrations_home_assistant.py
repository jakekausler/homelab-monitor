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
from typing import Annotated, cast

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.api.dependencies import (
    get_ha_client,
    get_ha_ws_client,
    get_http_client,
    get_vm_url,
    require_session,
)
from homelab_monitor.kernel.api.errors import HttpProblem
from homelab_monitor.kernel.api.vm_query import (
    VmInstantSample,
    first_sample,
    vm_count,
    vm_instant_query,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.ha.client import HaState, HomeAssistantRestClient
from homelab_monitor.kernel.ha.enrichment import (
    RepairEnrichment,
    attr_str,
    build_repairs_index,
    build_states_index,
    extract_issues,
)
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.ha.notifications import extract_notifications
from homelab_monitor.kernel.ha.websocket import HomeAssistantWebsocketClient

router = APIRouter(prefix="/integrations/home-assistant", tags=["integrations"])

_NOTIFICATIONS_WS_COMMAND = "persistent_notification/get"
_REPAIRS_WS_COMMAND = "repairs/list_issues"

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

# ── 027 detail-endpoint per-series queries ──────────────────────────────────
# Unlike /summary (which wraps each metric in count(...)), the detail endpoints
# read the RAW per-series vector via vm_instant_query and map each sample to a row.
# VM comparison operators (==, <) filter the returned series server-side.

# Entities: query the staleness gauge for UNAVAILABLE entities only, ordered by
# age DESC and capped to the top N. `homelab_ha_entity_available == 0` selects
# unavailable; we read last_changed_seconds for THOSE entities. Strategy: two
# queries joined in Python by entity_id (see Step 4). The age query:
_Q_ENTITY_UNAVAILABLE_SERIES = "homelab_ha_entity_available == 0"
_Q_ENTITY_LAST_CHANGED_SERIES = "homelab_ha_entity_last_changed_seconds"

_Q_BATTERY_LOW_SERIES = f"homelab_ha_battery_level < {_BATTERY_LOW_CEIL}"
_Q_UPDATES_PENDING_SERIES = "homelab_ha_update_available == 1"
_Q_CONFIG_ENTRY_ERROR_SERIES = "homelab_ha_config_entry_setup_error == 1"
_Q_REPAIR_ISSUE_SERIES = "homelab_ha_repair_issue == 1"

# Cardinality cap for the entities endpoint (~984 unavailable possible).
_ENTITIES_TOP_N = 100

# Default filter literals echoed back in `filtered_to`.
_FILTER_UNAVAILABLE = "unavailable"
_FILTER_LOW_OR_CRITICAL = "low_or_critical"
_FILTER_ERROR = "error"


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


# ── 027 detail row + response models ────────────────────────────────────────
# Rows are SELECTED from VM (locked D-DETAIL-CONSUMES-VM). Display fields VM cannot
# supply are filled by the STAGE-005-031 live-HA enrichment layer (see per-field
# "Enriched by STAGE-005-031" notes). The one still-deferred field is the config-entry
# precise `state` (coarse "error" only; precise state is STAGE-005-032 — see its note).


class HaEntityRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entity_id: str
    domain: str
    available: bool
    last_changed_age_seconds: float
    # Enriched by STAGE-005-031 (live-HA get_states). None when HA is down or the
    # entity is absent / has no friendly_name attribute. Widget rendering of this
    # field is deferred to STAGE-005-033.
    friendly_name: str | None


class HaEntityRowsResponse(BaseModel):
    """Unavailable HA entities (VM per-series), capped to the stalest top-N."""

    model_config = ConfigDict(extra="ignore")

    entities: list[HaEntityRow]
    total: int
    returned: int
    filtered_to: str | None


class HaBatteryRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entity_id: str
    domain: str
    level: float
    # Enriched by STAGE-005-031 (live-HA get_states friendly_name). None when HA
    # is down or the entity is absent / has no friendly_name attribute.
    device: str | None


class HaBatteryRowsResponse(BaseModel):
    """Low/critical battery HA entities (VM per-series)."""

    model_config = ConfigDict(extra="ignore")

    batteries: list[HaBatteryRow]
    total: int
    returned: int
    filtered_to: str | None


class HaUpdateRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entity_id: str
    title: str
    # Enriched by STAGE-005-031 (live-HA get_states attributes). None when HA is
    # down or the entity is absent / the attribute is missing or non-str.
    installed_version: str | None
    latest_version: str | None
    release_url: str | None


class HaUpdateRowsResponse(BaseModel):
    """HA entities with a pending update (VM per-series)."""

    model_config = ConfigDict(extra="ignore")

    updates: list[HaUpdateRow]
    total: int
    returned: int
    filtered_to: str | None


class HaConfigEntryRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    domain: str
    title: str
    # Precise HA ConfigEntryState (setup_error / setup_retry / migration_error /
    # failed_unload) sourced from the `state` VM label on
    # homelab_ha_config_entry_setup_error (STAGE-005-032). Falls back to "error"
    # for pre-migration series that predate the label.
    state: str


class HaConfigEntryRowsResponse(BaseModel):
    """HA config entries in an error state (VM per-series, coarse state)."""

    model_config = ConfigDict(extra="ignore")

    config_entries: list[HaConfigEntryRow]
    total: int
    returned: int
    filtered_to: str | None


class HaRepairRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    domain: str
    issue_id: str
    severity: str
    # Enriched by STAGE-005-031 (live-HA repairs/list_issues WS, `description` +
    # `learn_more_url` keys). None when HA is down or the issue is absent /
    # carries no such field.
    description: str | None
    learn_more_url: str | None


class HaRepairRowsResponse(BaseModel):
    """Open HA repair issues (VM per-series)."""

    model_config = ConfigDict(extra="ignore")

    repairs: list[HaRepairRow]
    total: int
    returned: int
    filtered_to: str | None


class HaNotificationRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    notification_id: str
    title: str | None
    message: str
    created_at: str | None


class HaNotificationsResponse(BaseModel):
    """Live persistent-notification bodies fetched from HA over the websocket."""

    model_config = ConfigDict(extra="ignore")

    rows: list[HaNotificationRow]
    total: int
    returned: int


def _sample_float(sample: VmInstantSample) -> float | None:
    """Parse a VmInstantSample's value_str as float; None on a non-numeric value."""
    try:
        return float(sample.value_str)
    except (ValueError, TypeError):
        return None


async def _ha_states_index(
    ha_client: HomeAssistantRestClient,
) -> dict[str, HaState]:
    """One bulk ``get_states()`` -> entity_id index, or empty index on HaError.

    STAGE-005-031 enrichment seam: HA failure degrades to an empty index (every
    enriched field becomes None), NEVER a 502 — VM already supplied the rows.
    The HaError is swallowed silently (NOT logged) so no enriched/error content
    can leak via logs (D-ENRICH-PRIVACY).
    """
    result = await ha_client.get_states()
    if isinstance(result, HaError):
        return {}
    return build_states_index(result)


async def _ha_repairs_index(
    ws_client: HomeAssistantWebsocketClient,
) -> dict[tuple[str, str], RepairEnrichment]:
    """One ``repairs/list_issues`` WS snapshot -> (domain, issue_id) -> RepairEnrichment.

    STAGE-005-031 enrichment seam: HA failure degrades to an empty index (every
    repair's ``description`` + ``learn_more_url`` become None), NEVER a 502. The
    HaError is swallowed silently (NOT logged) so no enriched/error content can
    leak via logs.
    """
    result = await ws_client.send_command(_REPAIRS_WS_COMMAND)
    if isinstance(result, HaError):
        return {}
    return build_repairs_index(extract_issues(result))


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


@router.get("/entities", response_model=HaEntityRowsResponse)
async def get_ha_entities(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    ha_client: Annotated[HomeAssistantRestClient, Depends(get_ha_client)],
    filter: Annotated[str, Query()] = _FILTER_UNAVAILABLE,
) -> HaEntityRowsResponse:
    """Return UNAVAILABLE HA entities (VM per-series), stalest-first, capped.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Source: ``homelab_ha_entity_available == 0`` joined per ``entity_id`` with
    ``homelab_ha_entity_last_changed_seconds`` (the age). Ordered by age DESC and
    capped to the top ``_ENTITIES_TOP_N``. ``total`` is the full unavailable count;
    ``returned`` is after the cap. VM failure -> 502 (via vm_instant_query).
    Only ``filter=unavailable`` is supported; the value is echoed in ``filtered_to``.

    STAGE-005-031: each row is enriched with ``friendly_name`` from a single live
    ``get_states()``. HA-down / row-absent / attribute-missing -> ``friendly_name``
    is None; the VM rows + 200 are ALWAYS preserved (HA failure is never a 502).
    """
    unavailable_samples, age_samples = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_ENTITY_UNAVAILABLE_SERIES),
        vm_instant_query(http_client, vm_url, _Q_ENTITY_LAST_CHANGED_SERIES),
    )

    # entity_id -> age (seconds). Absent -> default 0.0 in the join below.
    age_by_entity: dict[str, float] = {}
    for s in age_samples:
        entity_id = s.labels.get("entity_id", "")
        age = _sample_float(s)
        if entity_id and age is not None:
            age_by_entity[entity_id] = age

    # Live-HA enrichment snapshot (one bulk call). HaError -> empty index -> all
    # friendly_name None; never raises, never a 502.
    states_index = await _ha_states_index(ha_client)

    rows: list[HaEntityRow] = []
    for s in unavailable_samples:
        entity_id = s.labels.get("entity_id", "")
        if not entity_id:
            continue
        rows.append(
            HaEntityRow(
                entity_id=entity_id,
                domain=s.labels.get("domain", ""),
                available=False,
                last_changed_age_seconds=age_by_entity.get(entity_id, 0.0),
                friendly_name=attr_str(states_index.get(entity_id), "friendly_name"),
            )
        )

    total = len(rows)
    rows.sort(key=lambda r: r.last_changed_age_seconds, reverse=True)
    capped = rows[:_ENTITIES_TOP_N]

    return HaEntityRowsResponse(
        entities=capped,
        total=total,
        returned=len(capped),
        filtered_to=filter,
    )


@router.get("/batteries", response_model=HaBatteryRowsResponse)
async def get_ha_batteries(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    ha_client: Annotated[HomeAssistantRestClient, Depends(get_ha_client)],
    filter: Annotated[str, Query()] = _FILTER_LOW_OR_CRITICAL,
) -> HaBatteryRowsResponse:
    """Return low/critical-battery HA entities (VM per-series).

    Auth: cookie session required. CSRF NOT enforced on GET.

    Source: ``homelab_ha_battery_level < _BATTERY_LOW_CEIL``. Naturally bounded;
    ``total`` == ``returned`` (no cap). Each sample's value_str is the battery
    level. VM failure -> 502.

    STAGE-005-031: each row is enriched with ``device`` (the entity's
    friendly_name from a single live ``get_states()``). HA-down / row-absent /
    attribute-missing -> ``device`` None; VM rows + 200 always preserved.
    """
    samples = await vm_instant_query(http_client, vm_url, _Q_BATTERY_LOW_SERIES)
    states_index = await _ha_states_index(ha_client)
    rows: list[HaBatteryRow] = []
    for s in samples:
        entity_id = s.labels.get("entity_id", "")
        level = _sample_float(s)
        if not entity_id or level is None:
            continue
        rows.append(
            HaBatteryRow(
                entity_id=entity_id,
                domain=s.labels.get("domain", ""),
                level=level,
                device=attr_str(states_index.get(entity_id), "friendly_name"),
            )
        )
    return HaBatteryRowsResponse(
        batteries=rows,
        total=len(rows),
        returned=len(rows),
        filtered_to=filter,
    )


@router.get("/updates", response_model=HaUpdateRowsResponse)
async def get_ha_updates(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    ha_client: Annotated[HomeAssistantRestClient, Depends(get_ha_client)],
) -> HaUpdateRowsResponse:
    """Return HA entities with a pending update (VM per-series).

    Auth: cookie session required. CSRF NOT enforced on GET.

    Source: ``homelab_ha_update_available == 1``. ``title`` is read from the
    metric's ``title`` label. VM failure -> 502.

    STAGE-005-031: each row is enriched with ``installed_version`` /
    ``latest_version`` / ``release_url`` from a single live ``get_states()``.
    HA-down / row-absent / attribute-missing -> the field is None; VM rows + 200
    always preserved.
    """
    samples = await vm_instant_query(http_client, vm_url, _Q_UPDATES_PENDING_SERIES)
    states_index = await _ha_states_index(ha_client)
    rows: list[HaUpdateRow] = []
    for s in samples:
        entity_id = s.labels.get("entity_id", "")
        if not entity_id:
            continue
        state = states_index.get(entity_id)
        rows.append(
            HaUpdateRow(
                entity_id=entity_id,
                title=s.labels.get("title", ""),
                installed_version=attr_str(state, "installed_version"),
                latest_version=attr_str(state, "latest_version"),
                release_url=attr_str(state, "release_url"),
            )
        )
    return HaUpdateRowsResponse(
        updates=rows,
        total=len(rows),
        returned=len(rows),
        filtered_to=None,
    )


@router.get("/config-entries", response_model=HaConfigEntryRowsResponse)
async def get_ha_config_entries(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    filter: Annotated[str, Query()] = _FILTER_ERROR,
) -> HaConfigEntryRowsResponse:
    """Return HA config entries in an error state (VM per-series, precise state).

    Auth: cookie session required. CSRF NOT enforced on GET.

    Source: ``homelab_ha_config_entry_setup_error == 1``. ``state`` is the precise
    HA ConfigEntryState read from the series' ``state`` label (STAGE-005-032),
    falling back to "error" for pre-migration series with no ``state`` label.
    VM failure -> 502.
    """
    samples = await vm_instant_query(http_client, vm_url, _Q_CONFIG_ENTRY_ERROR_SERIES)
    rows: list[HaConfigEntryRow] = []
    for s in samples:
        domain = s.labels.get("domain", "")
        if not domain:
            continue
        rows.append(
            HaConfigEntryRow(
                domain=domain,
                title=s.labels.get("title", ""),
                # Precise state from the VM label; fall back to the coarse "error"
                # for pre-migration series that lack a `state` label.
                state=s.labels.get("state", _FILTER_ERROR),
            )
        )
    return HaConfigEntryRowsResponse(
        config_entries=rows,
        total=len(rows),
        returned=len(rows),
        filtered_to=filter,
    )


@router.get("/repairs", response_model=HaRepairRowsResponse)
async def get_ha_repairs(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    ws_client: Annotated[HomeAssistantWebsocketClient, Depends(get_ha_ws_client)],
) -> HaRepairRowsResponse:
    """Return open HA repair issues (VM per-series).

    Auth: cookie session required. CSRF NOT enforced on GET.

    Source: ``homelab_ha_repair_issue == 1``. ``severity`` from the metric label.
    VM failure -> 502.

    STAGE-005-031: each row is enriched with ``description`` (free-form prose) +
    ``learn_more_url`` (doc URL) from a live ``repairs/list_issues`` WS snapshot,
    keyed by ``(domain, issue_id)``. HA-down / issue-absent / field-missing ->
    both fields None; VM rows + 200 always preserved (the WS failure is swallowed
    to None, NEVER a 502).
    """
    samples = await vm_instant_query(http_client, vm_url, _Q_REPAIR_ISSUE_SERIES)
    repairs_index = await _ha_repairs_index(ws_client)
    rows: list[HaRepairRow] = []
    for s in samples:
        issue_id = s.labels.get("issue_id", "")
        if not issue_id:
            continue
        domain = s.labels.get("domain", "")
        enrichment = repairs_index.get((domain, issue_id))
        rows.append(
            HaRepairRow(
                domain=domain,
                issue_id=issue_id,
                severity=s.labels.get("severity", ""),
                description=enrichment.description if enrichment is not None else None,
                learn_more_url=(enrichment.learn_more_url if enrichment is not None else None),
            )
        )
    return HaRepairRowsResponse(
        repairs=rows,
        total=len(rows),
        returned=len(rows),
        filtered_to=None,
    )


@router.get("/notifications", response_model=HaNotificationsResponse)
async def get_ha_notifications(
    _user: Annotated[User, Depends(require_session())],
    ws_client: Annotated[HomeAssistantWebsocketClient, Depends(get_ha_ws_client)],
) -> HaNotificationsResponse:
    """Live-fetch HA persistent-notification bodies over the websocket.

    Any HaError (unreachable / timeout / auth / http_error / bad_response) maps
    to HTTP 502 ``upstream_unavailable``. The 502 body never echoes notification
    content. Notification bodies are returned to the authenticated session but
    are NEVER logged in this handler.
    """
    result = await ws_client.send_command(_NOTIFICATIONS_WS_COMMAND)
    if isinstance(result, HaError):
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="home assistant notifications query failed",
        )

    rows: list[HaNotificationRow] = []
    for notification in extract_notifications(result):
        if not isinstance(notification, dict):
            continue
        notification_dict = cast("dict[str, object]", notification)

        nid_obj = notification_dict.get("notification_id")
        nid = nid_obj if isinstance(nid_obj, str) else ""
        if not nid:
            continue

        title_obj = notification_dict.get("title")
        title = title_obj if isinstance(title_obj, str) else None

        message_obj = notification_dict.get("message")
        message = message_obj if isinstance(message_obj, str) else ""

        created_at_obj = notification_dict.get("created_at")
        created_at = created_at_obj if isinstance(created_at_obj, str) else None

        rows.append(
            HaNotificationRow(
                notification_id=nid,
                title=title,
                message=message,
                created_at=created_at,
            )
        )

    return HaNotificationsResponse(rows=rows, total=len(rows), returned=len(rows))
