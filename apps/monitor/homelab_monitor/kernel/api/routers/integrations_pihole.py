"""POST /api/integrations/pihole/* — Pi-hole WRITE endpoints (STAGE-006-018).

Two state-changing actions guarded by Scope.PIHOLE_WRITE + a per-action
confirm_phrase (mirrors the docker pull-and-restart precedent):

- POST /api/integrations/pihole/blocking        -> set DNS blocking on/off
- POST /api/integrations/pihole/gravity/update  -> rebuild gravity (streaming)

Both use the long-lived RW Pi-hole client (app.state.pihole_rw_client) and write
an audit row (who/what/before/after/ip) within a local transaction. Pi-hole state
lives remotely (not in the local DB), so the audit row is the sole local record of
the action, mirroring how docker probe-toggle audits the action. A downstream
PiholeError surfaces as HTTP 502 Bad Gateway; the attempt is NOT audited (audit on
success only, matching the probe-toggle precedent which audits the completed write).
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.api.dependencies import (
    get_http_client,
    get_repo,
    get_vm_url,
    require_session,
    require_user_or_token,
)
from homelab_monitor.kernel.api.errors import HttpProblem
from homelab_monitor.kernel.api.vm_query import (
    VmInstantSample,
    first_sample,
    vm_count,
    vm_instant_query,
)
from homelab_monitor.kernel.auth.models import ApiToken, User
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repositories.unifi_clients_repository import (
    UnifiClientRepo,
    UnifiClientRow,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.pihole.client import PiholeRestClient
from homelab_monitor.kernel.pihole.errors import PiholeError

router = APIRouter(prefix="/integrations/pihole", tags=["integrations-pihole"])

_CONFIRM_ENABLE: Literal["enable"] = "enable"
_CONFIRM_DISABLE: Literal["disable"] = "disable"
_CONFIRM_GRAVITY: Literal["update"] = "update"

# Max log_tail lines persisted in the audit "after" payload (the client already
# truncates to 20; this is a defensive second cap on the audit row size).
_AUDIT_LOG_TAIL_MAX = 20

# --- STAGE-006-020 panel data endpoints ---------------------------------------

# count query-param clamp for live top-clients / recent-blocked endpoints.
_DEFAULT_TOP_COUNT = 10
_MIN_TOP_COUNT = 1
_MAX_TOP_COUNT = 100

# VM instant-query expressions (scalars -> vm_count;
# label/value-bearing series -> vm_instant_query).
_Q_PIHOLE_UP = "homelab_pihole_up"
_Q_BLOCKING_ENABLED = "homelab_pihole_blocking_enabled"
_Q_BLOCKING_TIMER = "homelab_pihole_blocking_timer_seconds"
_Q_PERCENT_BLOCKED = "homelab_pihole_percent_blocked"
_Q_QUERY_FREQUENCY = "homelab_pihole_query_frequency"
_Q_MESSAGES_COUNT = "homelab_pihole_messages_count"
_Q_PRIVACY_LEVEL = "homelab_pihole_privacy_level"
_Q_QUERY_LOGGING_ENABLED = "homelab_pihole_query_logging_enabled"
_Q_GRAVITY_DOMAINS = "homelab_pihole_gravity_domains"
_Q_GRAVITY_LAST_UPDATE_AGE = "homelab_pihole_gravity_last_update_age_seconds"
_Q_VERSION_INFO = "homelab_pihole_version_info"
_Q_UPDATE_AVAILABLE = "homelab_pihole_update_available"

_Q_ADLIST_STATUS = "homelab_pihole_adlist_status"
_Q_ADLIST_ENABLED = "homelab_pihole_adlist_enabled"
_Q_ADLIST_DOMAINS = "homelab_pihole_adlist_domains"

_Q_UPSTREAM_QUERIES = "homelab_pihole_upstream_queries"

_Q_UNBOUND_CACHE_HIT_RATIO = "homelab_unbound_cache_hit_ratio"
_Q_UNBOUND_QUERIES_TOTAL = "homelab_unbound_queries_total"
_Q_UNBOUND_CACHE_HITS_TOTAL = "homelab_unbound_cache_hits_total"
_Q_UNBOUND_CACHE_MISSES_TOTAL = "homelab_unbound_cache_misses_total"
_Q_UNBOUND_PREFETCH_TOTAL = "homelab_unbound_prefetch_total"
_Q_UNBOUND_REQUESTLIST_CURRENT = "homelab_unbound_requestlist_current"
_Q_UNBOUND_EXTENDED_STATS_ENABLED = "homelab_pihole_unbound_extended_stats_enabled"

# STAGE-006-023 — extended-stats fields (recursion percentiles, DNSSEC, SERVFAIL)
_Q_UNBOUND_RECURSION_P50 = 'homelab_unbound_recursion_time_seconds{quantile="0.5"}'
_Q_UNBOUND_RECURSION_P95 = 'homelab_unbound_recursion_time_seconds{quantile="0.95"}'
_Q_UNBOUND_DNSSEC_SECURE_TOTAL = "homelab_unbound_answer_secure_total"
_Q_UNBOUND_DNSSEC_BOGUS_TOTAL = "homelab_unbound_answer_bogus_total"
_Q_UNBOUND_SERVFAIL_TOTAL = 'homelab_unbound_answer_rcode{rcode="SERVFAIL"}'


def _resolve_client_name(
    pihole_name: str | None,
    unifi: UnifiClientRow | None,
) -> str | None:
    """Return the best display name for a Pi-hole client.

    Precedence (first non-empty wins):
    1. Unifi name   — authoritative device identity
    2. Unifi hostname
    3. Pi-hole name — often null/manual, used as last resort
    """
    if unifi is not None:
        if unifi.name:
            return unifi.name
        if unifi.hostname:
            return unifi.hostname
    return pihole_name


def _scalar_float(samples: list[VmInstantSample]) -> float | None:
    """First sample's value parsed to float, or None when the series is absent/non-numeric."""
    sample = first_sample(samples)
    if sample is None:
        return None
    try:
        return float(sample.value_str)
    except (ValueError, TypeError):
        return None


def _scalar_bool(samples: list[VmInstantSample]) -> bool | None:
    """True when first sample == 1.0, False otherwise; None when the series is absent."""
    value = _scalar_float(samples)
    if value is None:
        return None
    return value == 1.0


def _scalar_int(samples: list[VmInstantSample]) -> int | None:
    """First sample's value as int, or None when absent/non-numeric."""
    value = _scalar_float(samples)
    if value is None:
        return None
    return int(value)


def _get_pihole_ro_client(request: Request) -> PiholeRestClient:
    client = getattr(request.app.state, "pihole_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pihole ro client is not initialized",
        )
    return client


def _get_pihole_rw_client(request: Request) -> PiholeRestClient:
    client = getattr(request.app.state, "pihole_rw_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pihole rw client is not initialized",
        )
    return client


def _who(principal: User | ApiToken) -> str:
    """Mirror docker.py: User -> username, ApiToken -> 'token:<name>'."""
    return principal.username if isinstance(principal, User) else f"token:{principal.name}"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


class BlockingRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: Literal["enable", "disable"]
    timer: int | None = None
    confirm_phrase: str


class BlockingResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    blocking: str
    timer: float | None
    audit_id: str


class GravityUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    confirm_phrase: str


class GravityUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    success: bool
    log_tail: list[str]
    audit_id: str


class PiholeVersionInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    component: str
    version: str


class PiholeUpdateInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    component: str


class PiholeOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    up: bool
    blocking_enabled: bool | None
    blocking_timer_seconds: float | None
    percent_blocked: float | None
    query_frequency: float | None
    messages_count: int
    privacy_level: int | None
    query_logging_enabled: bool | None
    gravity_domains: int | None
    versions: list[PiholeVersionInfo]
    updates_available: list[PiholeUpdateInfo]


class PiholeAdlistRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    list: str
    address: str
    status: str
    enabled: bool
    domains: int | None


class PiholeAdlistsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rows: list[PiholeAdlistRow]
    gravity_domains: int | None
    gravity_last_update_age_seconds: float | None


class PiholeUpstreamRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    upstream: str
    queries: float


class PiholeUpstreamsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rows: list[PiholeUpstreamRow]


class PiholeUnboundResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cache_hit_ratio: float | None
    queries_total: float | None
    cache_hits_total: float | None
    cache_misses_total: float | None
    prefetch_total: float | None
    requestlist_current: float | None
    # STAGE-006-023 — extended-stats-only fields. Null when extended stats are
    # disabled (the underlying VM series are absent in that case). Recursion times
    # are in SECONDS here; the frontend converts to ms for display.
    recursion_p50_seconds: float | None
    recursion_p95_seconds: float | None
    dnssec_secure_total: float | None
    dnssec_bogus_total: float | None
    servfail_total: float | None
    extended_stats_enabled: bool | None


class PiholeClientRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client: str
    name: str | None
    count: int


class PiholeClientsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rows: list[PiholeClientRow]
    returned: int


class PiholeRecentBlockedResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rows: list[str]
    returned: int


class PiholeMessageRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    type: str
    message: str
    timestamp: float | None
    url: str | None


class PiholeMessagesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rows: list[PiholeMessageRow]
    total: int
    returned: int


def _blocking_state_str(payload: object) -> str:
    """Extract the blocking-state string from a /api/dns/blocking payload (or 'unknown')."""
    if isinstance(payload, dict):
        val = cast("dict[str, object]", payload).get("blocking")
        if isinstance(val, str):
            return val
    return "unknown"


def _blocking_timer_val(payload: object) -> float | None:
    """Extract the timer (float|None) from a /api/dns/blocking payload."""
    if isinstance(payload, dict):
        val = cast("dict[str, object]", payload).get("timer")
        if isinstance(val, bool):
            return None
        if isinstance(val, (int, float)):
            return float(val)
    return None


def _validate_blocking_confirm(body: BlockingRequest) -> BlockingRequest:
    required = _CONFIRM_ENABLE if body.action == "enable" else _CONFIRM_DISABLE
    if body.confirm_phrase.strip().lower() != required:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"confirm_phrase must equal '{required}'",
        )
    return body


@router.post("/blocking", response_model=BlockingResponse)
async def set_blocking(
    body: Annotated[BlockingRequest, Depends(_validate_blocking_confirm)],
    request: Request,
    principal: Annotated[User | ApiToken, Depends(require_user_or_token({Scope.PIHOLE_WRITE}))],
    client: Annotated[PiholeRestClient, Depends(_get_pihole_rw_client)],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> BlockingResponse:
    """Enable/disable Pi-hole DNS blocking. confirm_phrase must equal the action."""

    # Read the CURRENT state first (for the audit `before`). A read failure is NOT
    # fatal — record before-state as "unknown" and proceed with the write.
    before_result = await client.dns_blocking()
    before_state = (
        _blocking_state_str(before_result.payload)
        if not isinstance(before_result, PiholeError)
        else "unknown"
    )

    blocking = body.action == "enable"
    result = await client.set_blocking(blocking=blocking, timer=body.timer)
    if isinstance(result, PiholeError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"pihole set_blocking failed: {result.message}",
        )

    new_state = _blocking_state_str(result.payload)
    new_timer = _blocking_timer_val(result.payload)
    audit_id = uuid7()
    async with repo.transaction() as conn:
        await insert_audit(
            conn,
            audit_id=audit_id,
            who=_who(principal),
            what=f"pihole.blocking.{body.action}",
            before={"blocking": before_state},
            after={"blocking": new_state, "timer": new_timer},
            ip=_client_ip(request),
        )
    return BlockingResponse(blocking=new_state, timer=new_timer, audit_id=audit_id)


def _validate_gravity_confirm(body: GravityUpdateRequest) -> GravityUpdateRequest:
    if body.confirm_phrase.strip().lower() != _CONFIRM_GRAVITY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"confirm_phrase must equal '{_CONFIRM_GRAVITY}'",
        )
    return body


@router.post("/gravity/update", response_model=GravityUpdateResponse)
async def gravity_update(
    body: Annotated[GravityUpdateRequest, Depends(_validate_gravity_confirm)],
    request: Request,
    principal: Annotated[User | ApiToken, Depends(require_user_or_token({Scope.PIHOLE_WRITE}))],
    client: Annotated[PiholeRestClient, Depends(_get_pihole_rw_client)],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> GravityUpdateResponse:
    """Trigger a Pi-hole gravity rebuild. confirm_phrase must equal 'update'."""

    result = await client.gravity_update()
    if isinstance(result, PiholeError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"pihole gravity update failed: {result.message}",
        )

    payload = result.payload
    success = False
    log_tail: list[str] = []
    if isinstance(payload, dict):
        success_obj = cast("dict[str, object]", payload).get("success")
        success = bool(success_obj) if isinstance(success_obj, bool) else False
        tail_obj = cast("dict[str, object]", payload).get("log_tail")
        if isinstance(tail_obj, list):
            log_tail = [str(x) for x in cast("list[object]", tail_obj)][:_AUDIT_LOG_TAIL_MAX]

    audit_id = uuid7()
    async with repo.transaction() as conn:
        await insert_audit(
            conn,
            audit_id=audit_id,
            who=_who(principal),
            what="pihole.gravity.update",
            before=None,
            after={"success": success, "log_tail": log_tail},
            ip=_client_ip(request),
        )
    return GravityUpdateResponse(success=success, log_tail=log_tail, audit_id=audit_id)


@router.get("/overview", response_model=PiholeOverviewResponse)
async def get_pihole_overview(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> PiholeOverviewResponse:
    # Scalars (vm_count defaults absent->0; we want None for "absent" on most fields,
    # so use vm_instant_query + _scalar_* for the nullable ones, vm_count only for messages_count).
    up_samples = await vm_instant_query(http_client, vm_url, _Q_PIHOLE_UP)
    blocking_enabled_samples = await vm_instant_query(http_client, vm_url, _Q_BLOCKING_ENABLED)
    blocking_timer_samples = await vm_instant_query(http_client, vm_url, _Q_BLOCKING_TIMER)
    percent_blocked_samples = await vm_instant_query(http_client, vm_url, _Q_PERCENT_BLOCKED)
    query_frequency_samples = await vm_instant_query(http_client, vm_url, _Q_QUERY_FREQUENCY)
    privacy_level_samples = await vm_instant_query(http_client, vm_url, _Q_PRIVACY_LEVEL)
    query_logging_samples = await vm_instant_query(http_client, vm_url, _Q_QUERY_LOGGING_ENABLED)
    gravity_domains_samples = await vm_instant_query(http_client, vm_url, _Q_GRAVITY_DOMAINS)
    version_samples = await vm_instant_query(http_client, vm_url, _Q_VERSION_INFO)
    update_samples = await vm_instant_query(http_client, vm_url, _Q_UPDATE_AVAILABLE)
    messages_count = await vm_count(http_client, vm_url, _Q_MESSAGES_COUNT)

    up_value = _scalar_float(up_samples)
    up = up_value == 1.0 if up_value is not None else False

    versions: list[PiholeVersionInfo] = []
    for sample in version_samples:
        component = sample.labels.get("component", "")
        version = sample.labels.get("version", "")
        if component:
            versions.append(PiholeVersionInfo(component=component, version=version))

    updates_available: list[PiholeUpdateInfo] = []
    for sample in update_samples:
        try:
            value = float(sample.value_str)
        except (ValueError, TypeError):
            continue
        component = sample.labels.get("component", "")
        if value == 1.0 and component:
            updates_available.append(PiholeUpdateInfo(component=component))

    return PiholeOverviewResponse(
        up=up,
        blocking_enabled=_scalar_bool(blocking_enabled_samples),
        blocking_timer_seconds=_scalar_float(blocking_timer_samples),
        percent_blocked=_scalar_float(percent_blocked_samples),
        query_frequency=_scalar_float(query_frequency_samples),
        messages_count=messages_count,
        privacy_level=_scalar_int(privacy_level_samples),
        query_logging_enabled=_scalar_bool(query_logging_samples),
        gravity_domains=_scalar_int(gravity_domains_samples),
        versions=versions,
        updates_available=updates_available,
    )


@router.get("/adlists", response_model=PiholeAdlistsResponse)
async def get_pihole_adlists(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> PiholeAdlistsResponse:
    status_samples = await vm_instant_query(http_client, vm_url, _Q_ADLIST_STATUS)
    enabled_samples = await vm_instant_query(http_client, vm_url, _Q_ADLIST_ENABLED)
    domains_samples = await vm_instant_query(http_client, vm_url, _Q_ADLIST_DOMAINS)
    gravity_domains_samples = await vm_instant_query(http_client, vm_url, _Q_GRAVITY_DOMAINS)
    gravity_age_samples = await vm_instant_query(http_client, vm_url, _Q_GRAVITY_LAST_UPDATE_AGE)

    # Index enabled & domains by (list, address).
    enabled_by_key: dict[tuple[str, str], bool] = {}
    for sample in enabled_samples:
        key = (sample.labels.get("list", ""), sample.labels.get("address", ""))
        try:
            enabled_by_key[key] = float(sample.value_str) == 1.0
        except (ValueError, TypeError):
            enabled_by_key[key] = False

    domains_by_key: dict[tuple[str, str], int] = {}
    for sample in domains_samples:
        key = (sample.labels.get("list", ""), sample.labels.get("address", ""))
        try:
            domains_by_key[key] = int(float(sample.value_str))
        except (ValueError, TypeError):
            continue

    rows: list[PiholeAdlistRow] = []
    for sample in status_samples:
        list_label = sample.labels.get("list", "")
        address = sample.labels.get("address", "")
        status_label = sample.labels.get("status", "")
        key = (list_label, address)
        rows.append(
            PiholeAdlistRow(
                list=list_label,
                address=address,
                status=status_label,
                enabled=enabled_by_key.get(key, False),
                domains=domains_by_key.get(key),
            )
        )

    return PiholeAdlistsResponse(
        rows=rows,
        gravity_domains=_scalar_int(gravity_domains_samples),
        gravity_last_update_age_seconds=_scalar_float(gravity_age_samples),
    )


@router.get("/upstreams", response_model=PiholeUpstreamsResponse)
async def get_pihole_upstreams(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> PiholeUpstreamsResponse:
    samples = await vm_instant_query(http_client, vm_url, _Q_UPSTREAM_QUERIES)
    rows: list[PiholeUpstreamRow] = []
    for sample in samples:
        upstream = sample.labels.get("upstream", "")
        try:
            queries = float(sample.value_str)
        except (ValueError, TypeError):
            continue
        rows.append(PiholeUpstreamRow(upstream=upstream, queries=queries))
    return PiholeUpstreamsResponse(rows=rows)


@router.get("/unbound", response_model=PiholeUnboundResponse)
async def get_pihole_unbound(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> PiholeUnboundResponse:
    cache_hit_ratio = await vm_instant_query(http_client, vm_url, _Q_UNBOUND_CACHE_HIT_RATIO)
    queries_total = await vm_instant_query(http_client, vm_url, _Q_UNBOUND_QUERIES_TOTAL)
    cache_hits_total = await vm_instant_query(http_client, vm_url, _Q_UNBOUND_CACHE_HITS_TOTAL)
    cache_misses_total = await vm_instant_query(http_client, vm_url, _Q_UNBOUND_CACHE_MISSES_TOTAL)
    prefetch_total = await vm_instant_query(http_client, vm_url, _Q_UNBOUND_PREFETCH_TOTAL)
    requestlist_current = await vm_instant_query(
        http_client, vm_url, _Q_UNBOUND_REQUESTLIST_CURRENT
    )
    recursion_p50 = await vm_instant_query(http_client, vm_url, _Q_UNBOUND_RECURSION_P50)
    recursion_p95 = await vm_instant_query(http_client, vm_url, _Q_UNBOUND_RECURSION_P95)
    dnssec_secure = await vm_instant_query(http_client, vm_url, _Q_UNBOUND_DNSSEC_SECURE_TOTAL)
    dnssec_bogus = await vm_instant_query(http_client, vm_url, _Q_UNBOUND_DNSSEC_BOGUS_TOTAL)
    servfail = await vm_instant_query(http_client, vm_url, _Q_UNBOUND_SERVFAIL_TOTAL)
    extended_stats = await vm_instant_query(http_client, vm_url, _Q_UNBOUND_EXTENDED_STATS_ENABLED)

    return PiholeUnboundResponse(
        cache_hit_ratio=_scalar_float(cache_hit_ratio),
        queries_total=_scalar_float(queries_total),
        cache_hits_total=_scalar_float(cache_hits_total),
        cache_misses_total=_scalar_float(cache_misses_total),
        prefetch_total=_scalar_float(prefetch_total),
        requestlist_current=_scalar_float(requestlist_current),
        recursion_p50_seconds=_scalar_float(recursion_p50),
        recursion_p95_seconds=_scalar_float(recursion_p95),
        dnssec_secure_total=_scalar_float(dnssec_secure),
        dnssec_bogus_total=_scalar_float(dnssec_bogus),
        servfail_total=_scalar_float(servfail),
        extended_stats_enabled=_scalar_bool(extended_stats),
    )


@router.get("/clients", response_model=PiholeClientsResponse)
async def get_pihole_clients(
    _user: Annotated[User, Depends(require_session())],
    client: Annotated[PiholeRestClient, Depends(_get_pihole_ro_client)],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
    blocked: Annotated[bool, Query()] = False,
    count: Annotated[int, Query(ge=_MIN_TOP_COUNT, le=_MAX_TOP_COUNT)] = _DEFAULT_TOP_COUNT,
) -> PiholeClientsResponse:
    result = await client.stats_top_clients(blocked=blocked, count=count)
    if isinstance(result, PiholeError):
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="pihole top clients query failed",
        )

    unifi_repo = UnifiClientRepo(repo)
    # Capture one consistent timestamp for all IP→MAC lookups in this request.
    now = utc_now_iso()

    rows: list[PiholeClientRow] = []
    payload = result.payload
    if isinstance(payload, dict):
        clients_obj = cast("dict[str, object]", payload).get("clients")
        if isinstance(clients_obj, list):
            for entry in cast("list[object]", clients_obj):
                if not isinstance(entry, dict):
                    continue
                entry_dict = cast("dict[str, object]", entry)
                ip_obj = entry_dict.get("ip")
                if not isinstance(ip_obj, str) or not ip_obj:
                    continue
                name_obj = entry_dict.get("name")
                pihole_name = name_obj if isinstance(name_obj, str) and name_obj else None
                count_obj = entry_dict.get("count")
                count_val = (
                    int(count_obj)
                    if isinstance(count_obj, (int, float)) and not isinstance(count_obj, bool)
                    else 0
                )

                # Unifi join: IP → MAC → client row.
                # N+1 pattern bounded by the count cap (≤100 rows); could be batched later
                # if query latency becomes measurable. TODO(perf): batch find_mac_by_ip_at +
                # get_client into single queries when needed.
                mac = await unifi_repo.find_mac_by_ip_at(ip_obj, now)
                unifi_row = await unifi_repo.get_client(mac) if mac is not None else None
                name = _resolve_client_name(pihole_name=pihole_name, unifi=unifi_row)

                rows.append(PiholeClientRow(client=ip_obj, name=name, count=count_val))

    return PiholeClientsResponse(rows=rows, returned=len(rows))


@router.get("/recent-blocked", response_model=PiholeRecentBlockedResponse)
async def get_pihole_recent_blocked(
    _user: Annotated[User, Depends(require_session())],
    client: Annotated[PiholeRestClient, Depends(_get_pihole_ro_client)],
    count: Annotated[int, Query(ge=_MIN_TOP_COUNT, le=_MAX_TOP_COUNT)] = _DEFAULT_TOP_COUNT,
) -> PiholeRecentBlockedResponse:
    result = await client.stats_recent_blocked()
    if isinstance(result, PiholeError):
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="pihole recent blocked query failed",
        )

    payload = result.payload
    # Accept {"blocked": [...]} OR a bare list. Each item may be a domain string
    # or a dict with a "domain" key.
    items: list[object] = []
    if isinstance(payload, dict):
        blocked_obj = cast("dict[str, object]", payload).get("blocked")
        if isinstance(blocked_obj, list):
            items = cast("list[object]", blocked_obj)
    elif isinstance(payload, list):
        items = cast("list[object]", payload)

    rows: list[str] = []
    for item in items:
        if isinstance(item, str):
            if item:
                rows.append(item)
        elif isinstance(item, dict):
            domain_obj = cast("dict[str, object]", item).get("domain")
            if isinstance(domain_obj, str) and domain_obj:
                rows.append(domain_obj)
        if len(rows) >= count:
            break

    return PiholeRecentBlockedResponse(rows=rows, returned=len(rows))


@router.get("/messages", response_model=PiholeMessagesResponse)
async def get_pihole_messages(
    _user: Annotated[User, Depends(require_session())],
    client: Annotated[PiholeRestClient, Depends(_get_pihole_ro_client)],
) -> PiholeMessagesResponse:
    result = await client.info_messages()
    if isinstance(result, PiholeError):
        raise HttpProblem(
            status_code=502,
            code="upstream_unavailable",
            message="pihole messages query failed",
        )

    rows: list[PiholeMessageRow] = []
    total = 0
    payload = result.payload
    if isinstance(payload, dict):
        messages_obj = cast("dict[str, object]", payload).get("messages")
        if isinstance(messages_obj, list):
            messages_list = cast("list[object]", messages_obj)
            total = len(messages_list)
            for entry in messages_list:
                if not isinstance(entry, dict):
                    continue
                entry_dict = cast("dict[str, object]", entry)

                id_obj = entry_dict.get("id")
                msg_id = id_obj if isinstance(id_obj, int) and not isinstance(id_obj, bool) else 0

                type_obj = entry_dict.get("type")
                msg_type = type_obj if isinstance(type_obj, str) else "unknown"

                # Pi-hole v6 carries the text in "plain"; fall back to "message".
                plain_obj = entry_dict.get("plain")
                message = plain_obj if isinstance(plain_obj, str) else None
                if message is None:
                    message_obj = entry_dict.get("message")
                    message = message_obj if isinstance(message_obj, str) else ""

                ts_obj = entry_dict.get("timestamp")
                timestamp = (
                    float(ts_obj)
                    if isinstance(ts_obj, (int, float)) and not isinstance(ts_obj, bool)
                    else None
                )

                url_obj = entry_dict.get("url")
                url = url_obj if isinstance(url_obj, str) else None

                rows.append(
                    PiholeMessageRow(
                        id=msg_id,
                        type=msg_type,
                        message=message,
                        timestamp=timestamp,
                        url=url,
                    )
                )

    return PiholeMessagesResponse(rows=rows, total=total, returned=len(rows))
