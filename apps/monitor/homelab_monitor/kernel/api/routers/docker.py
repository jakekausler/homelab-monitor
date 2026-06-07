"""GET /api/integrations/docker/containers — session-auth.

Single endpoint returning ContainerRow[] (matches UI contract in
apps/ui/src/routes/integrations/types.ts). Cadvisor fields (cpu_pct, mem_mib)
come from the SQLite cache populated by DockerSocketCollector's VM merge
step (T-MERGE-LOCATION) — sub-10ms read, no live VM query.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from homelab_monitor.kernel.api.dependencies import (
    get_http_client,
    get_repo,
    get_vl_url,
    require_session,
    require_user_or_token,
)
from homelab_monitor.kernel.api.errors import HttpProblem
from homelab_monitor.kernel.auth.models import ApiToken, User
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.config import load_vl_query_limits
from homelab_monitor.kernel.db.repositories.compose_actions_repository import (
    ComposeActionRow,
    ComposeActionsRepository,
)
from homelab_monitor.kernel.db.repositories.docker_build_hashes_repository import (
    DockerBuildHashesRepository,
    DockerBuildHashRow,
)
from homelab_monitor.kernel.db.repositories.image_update_state_repository import (
    ImageUpdateStateRepository,
    ImageUpdateStateRow,
)
from homelab_monitor.kernel.db.repositories.probe_targets_repository import (
    ProbeTargetRow,
    ProbeTargetsRepository,
)
from homelab_monitor.kernel.db.repositories.suggestions_repository import (
    ALLOWED_STATES,
    SuggestionsRepository,
)
from homelab_monitor.kernel.db.repositories.suggestions_repository import (
    DockerSuggestionRow as DockerSuggestionRepoRow,
)
from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.compose_action_runner import ComposeActionRunner
from homelab_monitor.kernel.docker.socket_client import DockerSocketClient
from homelab_monitor.kernel.logs.crash_enrichments_repo import CrashEnrichmentsRepository
from homelab_monitor.kernel.logs.models import LogLine, from_victorialogs_line
from homelab_monitor.kernel.logs.pagination import (
    InvalidCursorError,
    paginate_older,
)
from homelab_monitor.kernel.logs.time_window import parse_and_validate_window
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VictoriaLogsClientError,
    logsql_quote_phrase,
)

router = APIRouter(prefix="/integrations/docker", tags=["docker"])

# Healthcheck Test minimum length: ["CMD"|"CMD-SHELL", "command", ...] requires at least 2 elements
_HEALTHCHECK_TEST_MIN_LENGTH = 2


class ContainerRow(BaseModel):
    """Mirrors apps/ui/src/routes/integrations/types.ts::ContainerRow."""

    # extra="ignore" for forward-compat: STAGE-003-005+ will add fields incrementally.
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    image: str | None = None
    status: str | None = None
    cpu_pct: float | None = None
    mem_mib: float | None = None
    restart_count: int | None = None
    exit_code: int | None = None
    healthcheck: str | None = None  # 'healthy' | 'unhealthy' | 'starting' | None
    network_mode: str | None = None
    labels: dict[str, str] = {}
    # NEW: STAGE-003-005 Refinement — logical-key rekey + forensics
    container_id: str | None = None
    logical_key_kind: str | None = None  # 'compose' | 'name'
    logical_key: str | None = None
    previous_container_id: str | None = None  # most-recent prior container_id
    recreated_at: str | None = None
    # NEW: STAGE-003-005 Q2 + Q1 — compose columns + 24h restart count
    compose_project: str | None = None
    compose_service: str | None = None
    compose_file_path: str | None = None
    restart_count_24h: int | None = None


class ContainerListResponse(BaseModel):
    # extra="ignore" for forward-compat: STAGE-003-005+ will add fields incrementally.
    model_config = ConfigDict(extra="ignore")

    containers: list[ContainerRow]


_ContainerLogStatus = Literal[
    "available",
    "no_lines",
    "container_unknown",
    "vl_unavailable",
]


class ContainerLogsResponse(BaseModel):
    """Response body for GET /api/integrations/docker/containers/{name}/logs.

    log_status values:
      - "available": >=1 line returned within the window.
      - "no_lines": container known to inventory but VL returned no lines.
      - "container_unknown": container not in targets table (returned with 404).
      - "vl_unavailable": VictoriaLogs unreachable/timeout/non-200 (returned with 503).

    For container_unknown and vl_unavailable: lines = [], window_start/window_end
    may be None (no query was actually run for unknown; no result for unavailable).
    """

    model_config = ConfigDict(extra="forbid")

    container_name: str
    log_status: _ContainerLogStatus
    lines: list[LogLine]
    truncated: bool
    window_start: str | None
    window_end: str | None
    next_cursor: str | None = None
    has_more: bool = False


class ContainerCrashSummary(BaseModel):
    """One crash enrichment summary (no log lines)."""

    model_config = ConfigDict(extra="forbid")

    crash_id: str
    exit_code: int
    finished_at: str
    image_name: str | None
    compose_project: str | None
    compose_service: str | None
    line_count: int
    truncated: bool
    degraded: bool
    created_at: str


class ContainerCrashesResponse(BaseModel):
    """List of crash summaries for one container, newest crash first."""

    model_config = ConfigDict(extra="forbid")

    container_name: str
    crashes: list[ContainerCrashSummary]


class ContainerCrashDetail(BaseModel):
    """One crash enrichment with its persisted VictoriaLogs window."""

    model_config = ConfigDict(extra="forbid")

    crash_id: str
    container_name: str
    exit_code: int
    finished_at: str
    image_name: str | None
    compose_project: str | None
    compose_service: str | None
    line_count: int
    truncated: bool
    degraded: bool
    created_at: str
    window_start: str
    window_end: str
    lines: list[LogLine]


def _get_targets_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> TargetsRepository:
    """Construct a TargetsRepository from the injected SqliteRepository."""
    return TargetsRepository(repo)


def _get_crash_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> CrashEnrichmentsRepository:
    """Construct a CrashEnrichmentsRepository from the injected SqliteRepository."""
    return CrashEnrichmentsRepository(repo)


@router.get("/containers", response_model=ContainerListResponse)
async def list_containers(
    _user: Annotated[User, Depends(require_session())],
    targets_repo: Annotated[TargetsRepository, Depends(_get_targets_repo)],
) -> ContainerListResponse:
    """List all Docker containers from the targets table.

    Requires an authenticated session. Returns cached CPU/mem metrics from
    the last collector tick; does not query VictoriaMetrics live.
    """
    rows = await targets_repo.list_docker_containers(include_hidden=False)
    return ContainerListResponse(
        containers=[
            ContainerRow(
                id=row.id,
                name=row.name,
                image=row.image,
                status=row.status,
                cpu_pct=row.cpu_pct_cached,
                mem_mib=row.mem_mib_cached,
                restart_count=row.restart_count,
                exit_code=row.exit_code,
                healthcheck=row.healthcheck,
                network_mode=row.network_mode,
                labels=row.labels,
                container_id=row.container_id,
                logical_key_kind=row.logical_key_kind,
                logical_key=row.logical_key,
                previous_container_id=row.previous_container_id,
                recreated_at=row.recreated_at,
                compose_project=row.compose_project,
                compose_service=row.compose_service,
                compose_file_path=row.compose_file_path,
                restart_count_24h=row.restart_count_24h_cached,
            )
            for row in rows
        ]
    )


class DockerSuggestionRow(BaseModel):
    """Joined view of one Docker suggestion."""

    model_config = ConfigDict(extra="ignore")

    id: str
    # 'docker_container_discovered' | 'docker_label_collision' | 'docker_file_override_malformed'
    kind: str
    deduplication_key: str
    state: str  # 'pending' | 'accepted' | 'ignored' | 'container_gone'
    created_at: str
    updated_at: str
    container_id: str
    container_name: str
    image_ref: str
    labels: dict[str, str] = {}
    compose_project: str | None = None
    compose_service: str | None = None
    compose_file_path: str | None = None
    detection_reason: str  # 'no_homelab_monitor_label' | 'disabled_profile' | 'label_collision'


class DockerSuggestionListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    suggestions: list[DockerSuggestionRow]
    next_cursor: str | None = None


# ----------------------------------------------------------------------
# STAGE-003-012 — Accept / Customize / Ignore endpoints.
#
# EPIC-011 cross-reference:
# These per-integration suggestion endpoints may be subsumed by generic
# /api/suggestions/{id}/{accept,customize,ignore} endpoints when EPIC-011
# builds the global Discovery & Suggestions inbox. See:
#   - epics/EPIC-011-discovery-suggestions/EPIC-011.md "Inherited carry-forwards from EPIC-003"
#   - epics/EPIC-003-docker/EPIC-003.md "Cross-epic carry-forward → EPIC-011"
# The suggestions schema is stable; only URL paths may change.
# ----------------------------------------------------------------------


class ProbeSpec(BaseModel):
    """One probe descriptor in a Customize request body."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["http", "tcp", "exec", "metrics"]
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    target_value: str = Field(min_length=1)
    interval_seconds: int = Field(default=60, ge=1, le=3600)
    timeout_seconds: int = Field(default=10, ge=1, le=300)


class CreateProbeTargetRequest(BaseModel):
    """Request body for POST /integrations/docker/probe-targets.

    Mirrors ProbeSpec shape, but adds container_name (since this endpoint
    is NOT scoped under a suggestion).
    """

    model_config = ConfigDict(extra="forbid")

    container_name: str = Field(min_length=1, max_length=255)
    kind: Literal["http", "tcp", "exec", "metrics"]
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    target_value: str = Field(min_length=1)
    interval_seconds: int = Field(default=60, ge=1, le=3600)
    timeout_seconds: int = Field(default=10, ge=1, le=300)


class UpdateProbeTargetRequest(BaseModel):
    """Request body for PATCH /integrations/docker/probe-targets/{probe_id}.

    All fields optional — partial-update semantics. kind, name, container_name
    are NOT mutable (UNIQUE key on probe_targets).
    """

    model_config = ConfigDict(extra="forbid")

    target_value: str | None = Field(default=None, min_length=1)
    interval_seconds: int | None = Field(default=None, ge=1, le=3600)
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)


class SuggestionAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apply_default_probes: bool = True


class SuggestionAcceptResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    suggestion: DockerSuggestionRow
    probes_created: int


class SuggestionCustomizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    probes: list[ProbeSpec] = Field(min_length=1)


class SuggestionCustomizeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    suggestion: DockerSuggestionRow
    probes_created: int
    probes_updated: int


class SuggestionIgnoreResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    suggestion: DockerSuggestionRow


class SuggestionDefaultProbesResponse(BaseModel):
    probes: list[ProbeSpec]
    reason: Literal["available", "docker_unavailable", "container_gone", "no_ports_no_healthcheck"]
    model_config = ConfigDict(extra="forbid")


def _get_suggestions_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> SuggestionsRepository:
    return SuggestionsRepository(repo)


_SUGGESTION_STATUS_QUERY = Literal["pending", "accepted", "ignored", "container_gone", "all"]
_DEFAULT_SUGGESTION_PAGE_SIZE: int = 50
_MAX_SUGGESTION_PAGE_SIZE: int = 200

# STAGE-003-011 — container log viewer
_LOGS_DEFAULT_SINCE: str = "15m"
# Both intentionally 500 in STAGE-003-011 — the limit param is kept in the contract for
# future flexibility; EPIC-004 STAGE-004-005 will introduce real pagination (cursor +
# raised cap). For now, any caller-supplied limit is silently clamped to 500.
_LOGS_MAX_LINES: int = 500
_LOGS_DEFAULT_LIMIT: int = 500
_LOGS_MAX_SINCE_SECONDS: int = 7 * 24 * 60 * 60  # 7 days
_SINCE_PATTERN = re.compile(r"^(\d+)([smhd])$")
_SECONDS_PER_UNIT: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}


@router.get("/suggestions", response_model=DockerSuggestionListResponse)
async def list_suggestions(
    _user: Annotated[User, Depends(require_session())],
    suggestions_repo: Annotated[SuggestionsRepository, Depends(_get_suggestions_repo)],
    status_filter: Annotated[
        _SUGGESTION_STATUS_QUERY,
        Query(alias="status"),
    ] = "pending",
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[
        int, Query(ge=1, le=_MAX_SUGGESTION_PAGE_SIZE)
    ] = _DEFAULT_SUGGESTION_PAGE_SIZE,
) -> DockerSuggestionListResponse:
    """List Docker suggestions filtered by state, paginated by cursor.

    Cursor opaque to the client. status='all' lists every state.
    """
    if status_filter not in ALLOWED_STATES and status_filter != "all":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid status: {status_filter}",
        )
    try:
        rows, next_cursor = await suggestions_repo.list_pending_docker_suggestions(
            status=status_filter,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return DockerSuggestionListResponse(
        suggestions=[
            DockerSuggestionRow(
                id=r.id,
                kind=r.kind,
                deduplication_key=r.deduplication_key,
                state=r.state,
                created_at=r.created_at,
                updated_at=r.updated_at,
                container_id=r.container_id,
                container_name=r.container_name,
                image_ref=r.image_ref,
                labels=r.labels,
                compose_project=r.compose_project,
                compose_service=r.compose_service,
                compose_file_path=r.compose_file_path,
                detection_reason=r.detection_reason,
            )
            for r in rows
        ],
        next_cursor=next_cursor,
    )


class ProbeRow(BaseModel):
    """One probe target — mirrors ProbeTargetRow."""

    model_config = ConfigDict(extra="ignore")

    id: str
    container_name: str
    kind: str  # 'http' | 'tcp' | 'exec' | 'metrics'
    name: str
    target_value: str
    config_source: str  # 'label' | 'file_override' | 'auto_default' | 'discovered_accepted'
    enabled: bool
    interval_seconds: int
    timeout_seconds: int
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    created_at: str
    hidden_at: str | None = None
    exec_authorized: bool


class ListProbesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    probes: list[ProbeRow]


class ProbeSummaryEntry(BaseModel):
    """Per-container probe counts + source breakdown + config errors."""

    model_config = ConfigDict(extra="ignore")

    container_name: str
    active: int  # count of enabled probes
    failing: int  # count of enabled probes with last_status='fail' or 'error'
    # STAGE-003-007 D-SUMMARY-ENDPOINT-EXTENSION:
    source_breakdown: dict[str, int] = {}
    config_errors: list[str] | None = None


class ProbeSummaryResponse(BaseModel):
    """Aggregate probe summary, one entry per container with at least one probe."""

    model_config = ConfigDict(extra="ignore")

    summaries: list[ProbeSummaryEntry]


def _get_probe_targets_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> ProbeTargetsRepository:
    return ProbeTargetsRepository(repo)


@router.get(
    "/containers/{name}/probes",
    response_model=ListProbesResponse,
)
async def list_container_probes(
    name: str,
    _user: Annotated[User, Depends(require_session())],
    targets_repo: Annotated[TargetsRepository, Depends(_get_targets_repo)],
    probes_repo: Annotated[ProbeTargetsRepository, Depends(_get_probe_targets_repo)],
) -> ListProbesResponse:
    """List probes for one container. 404 if the container is unknown."""
    rows = await targets_repo.list_docker_containers(include_hidden=False)
    if not any(r.name == name for r in rows):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"container not found: {name}"
        )
    probes = await probes_repo.list_for_container(container_name=name, include_hidden=False)
    return ListProbesResponse(
        probes=[_probe_row_to_dto(p) for p in probes],
    )


def _parse_since(since: str) -> int:
    """Parse 'Xs|Xm|Xh|Xd' → total seconds. Raises HTTPException 422 on bad format.

    Clamped at _LOGS_MAX_SINCE_SECONDS (7d) — caller does NOT need to clamp.
    Empty string / whitespace are treated as invalid (use _LOGS_DEFAULT_SINCE
    at the FastAPI default level instead).
    """
    m = _SINCE_PATTERN.match(since.strip())
    if m is None:
        raise HTTPException(
            status_code=422,
            detail=f"invalid since format (expected Xs|Xm|Xh|Xd): {since!r}",
        )
    value, unit = int(m.group(1)), m.group(2)
    if value <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"since must be > 0: {since!r}",
        )
    total = value * _SECONDS_PER_UNIT[unit]
    return min(total, _LOGS_MAX_SINCE_SECONDS)


@router.get(
    "/containers/{name}/logs",
    response_model=ContainerLogsResponse,
    responses={
        200: {"description": "Logs available or window empty"},
        400: {"description": "Invalid start/end range (format, order, or >30d)"},
        404: {"description": "Container not in inventory"},
        422: {"description": "Invalid since param, or start/end+since conflict"},
        503: {"description": "VictoriaLogs temporarily unavailable"},
    },
)
async def get_container_logs(  # noqa: PLR0913 -- FastAPI route with injected dependencies
    name: str,
    _user: Annotated[User, Depends(require_session())],
    targets_repo: Annotated[TargetsRepository, Depends(_get_targets_repo)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    vl_url: Annotated[str, Depends(get_vl_url)],
    since: Annotated[str | None, Query()] = None,
    start: Annotated[str | None, Query()] = None,
    end: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = _LOGS_DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> ContainerLogsResponse:
    """Fetch recent log lines for one container from VictoriaLogs.

    Auth: session-only (operator dashboard). LogsQL is constructed server-side
    using `service:"<name>"` (vector's add_labels guarantees this label).

    Time window: supply EITHER `since` (Xs|Xm|Xh|Xd, default 15m, 7d cap) OR an
    explicit ISO-8601 `start`+`end` pair (30d cap, shared validation with
    /api/logs/query). The two are mutually exclusive (422 if both given).

    Hard caps:
      - limit: silently clamped at 500.
      - since: silently clamped at 7d.
      - start/end span: max 30d (HttpProblem 400 if exceeded).
      - max-bytes / timeout: inherited from VlQueryLimits (load_vl_query_limits()).
    """
    # Clamp limit silently (per D-API-CONTRACT: no 422 for over-cap limit).
    effective_limit = min(limit, _LOGS_MAX_LINES)

    # STAGE-004-008 — resolve the [start, end] window from EITHER an explicit
    # ISO start/end pair OR a since-duration token. Mutually exclusive.
    has_explicit_range = start is not None or end is not None
    since_supplied = since is not None
    if has_explicit_range and since_supplied:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start/end and since are mutually exclusive",
        )
    if has_explicit_range and (start is None or end is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start and end must be supplied together",
        )

    # 404 if container not in inventory.
    rows = await targets_repo.list_docker_containers(include_hidden=False)
    if not any(r.name == name for r in rows):
        # 404 with populated body per D-API-CONTRACT.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ContainerLogsResponse(
                container_name=name,
                log_status="container_unknown",
                lines=[],
                truncated=False,
                window_start=None,
                window_end=None,
            ).model_dump(),
        )

    # Build the [start, end] window.
    if has_explicit_range:
        # Explicit ISO range path: 30-day cap, ISO parse, start<end (shared with
        # /api/logs/query). Raises HttpProblem(400, ...) on violation.
        assert start is not None  # narrowed by has_explicit_range + pair check
        assert end is not None
        window_start, window_end = parse_and_validate_window(start, end)
    else:
        # since-duration path: 7-day cap (raises 422 on bad format).
        effective_since = since if since is not None else _LOGS_DEFAULT_SINCE
        window_seconds = _parse_since(effective_since)
        now = datetime.now(UTC)
        window_start = (now - timedelta(seconds=window_seconds)).isoformat()
        window_end = now.isoformat()

    # Build the LogsQL query — D-LOG-LABEL-SERVICE.
    expr = f"service:{logsql_quote_phrase(name)}"

    # Run the VL query via the bounded client + A1 paginator.
    base_limits = load_vl_query_limits()
    client = VictoriaLogsClient(vl_url=vl_url, http_client=http_client, limits=base_limits)
    try:
        page = await paginate_older(
            client=client,
            expr=expr,
            window_start=window_start,
            window_end=window_end,
            page_size=effective_limit,
            base_limits=base_limits,
            cursor=cursor,
        )
    except InvalidCursorError as exc:
        raise HttpProblem(
            status_code=400,
            code="invalid_cursor",
            message=str(exc),
        ) from exc
    except VictoriaLogsClientError as exc:
        # D-API-CONTRACT: 503 with populated body, lines=[].
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ContainerLogsResponse(
                container_name=name,
                log_status="vl_unavailable",
                lines=[],
                truncated=False,
                window_start=window_start,
                window_end=window_end,
            ).model_dump(),
        ) from exc

    if len(page.lines) == 0 and cursor is None:
        log_status: _ContainerLogStatus = "no_lines"
    else:
        log_status = "available"

    return ContainerLogsResponse(
        container_name=name,
        log_status=log_status,
        lines=[from_victorialogs_line(ln) for ln in page.lines],
        truncated=page.truncated,
        window_start=window_start,
        window_end=window_end,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get(
    "/containers/{name}/crashes",
    response_model=ContainerCrashesResponse,
    responses={
        200: {"description": "Crash list (possibly empty)"},
        404: {"description": "Container not in inventory"},
    },
)
async def list_container_crashes(
    name: str,
    _user: Annotated[User, Depends(require_session())],
    targets_repo: Annotated[TargetsRepository, Depends(_get_targets_repo)],
    crash_repo: Annotated[CrashEnrichmentsRepository, Depends(_get_crash_repo)],
) -> ContainerCrashesResponse:
    """List detected crashes for one container (summaries; no log lines).

    Resolves the container's logical_key from inventory, then returns its crash
    enrichment rows newest-first. 404 if the container is unknown.
    """
    rows = await targets_repo.list_docker_containers(include_hidden=False)
    target = next((r for r in rows if r.name == name), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"container not found: {name}",
        )
    logical_key = target.logical_key if target.logical_key is not None else target.name
    crashes = await crash_repo.list_for_container(logical_key)
    return ContainerCrashesResponse(
        container_name=name,
        crashes=[
            ContainerCrashSummary(
                crash_id=c.crash_id,
                exit_code=c.exit_code,
                finished_at=c.finished_at,
                image_name=c.image_name,
                compose_project=c.compose_project,
                compose_service=c.compose_service,
                line_count=c.line_count,
                truncated=c.truncated,
                degraded=c.degraded,
                created_at=c.created_at,
            )
            for c in crashes
        ],
    )


@router.get(
    "/containers/{name}/crashes/{crash_id}",
    response_model=ContainerCrashDetail,
    responses={
        200: {"description": "Crash detail with log window"},
        404: {"description": "Container or crash unknown / mismatched"},
    },
)
async def get_container_crash_detail(
    name: str,
    crash_id: str,
    _user: Annotated[User, Depends(require_session())],
    targets_repo: Annotated[TargetsRepository, Depends(_get_targets_repo)],
    crash_repo: Annotated[CrashEnrichmentsRepository, Depends(_get_crash_repo)],
) -> ContainerCrashDetail:
    """Return one crash's full detail incl. the persisted VL log window.

    404 if the container is unknown, the crash is missing, OR the crash belongs
    to a different container (defends against cross-container crash_id probing).
    """
    rows = await targets_repo.list_docker_containers(include_hidden=False)
    target = next((r for r in rows if r.name == name), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"container not found: {name}",
        )
    crash = await crash_repo.get(crash_id)
    logical_key = target.logical_key if target.logical_key is not None else target.name
    # Intentionally a superset of the list filter (which matches strictly on
    # logical_key): a crash belongs to this container if EITHER its stored
    # container_name matches the path name OR its logical_key matches. The
    # logical_key branch keeps a compose container's crashes reachable after a
    # recreate changed its container_name. Reached via an id from the list, so
    # the extra name branch is harmless and only widens legitimate matches.
    if crash is None or (crash.container_name != name and crash.logical_key != logical_key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"crash not found: {crash_id}",
        )
    return ContainerCrashDetail(
        crash_id=crash.crash_id,
        container_name=crash.container_name,
        exit_code=crash.exit_code,
        finished_at=crash.finished_at,
        image_name=crash.image_name,
        compose_project=crash.compose_project,
        compose_service=crash.compose_service,
        line_count=crash.line_count,
        truncated=crash.truncated,
        degraded=crash.degraded,
        created_at=crash.created_at,
        window_start=crash.window_start,
        window_end=crash.window_end,
        lines=crash.parse_lines(),
    )


@router.post("/probes/{probe_id}/disable", response_model=ProbeRow)
async def disable_probe(
    probe_id: str,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
    probes_repo: Annotated[ProbeTargetsRepository, Depends(_get_probe_targets_repo)],
) -> ProbeRow:
    return await _toggle_probe(
        repo, probes_repo, probe_id, enabled=False, user=user, what="docker.probe.disable"
    )


@router.post("/probes/{probe_id}/enable", response_model=ProbeRow)
async def enable_probe(
    probe_id: str,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
    probes_repo: Annotated[ProbeTargetsRepository, Depends(_get_probe_targets_repo)],
) -> ProbeRow:
    return await _toggle_probe(
        repo, probes_repo, probe_id, enabled=True, user=user, what="docker.probe.enable"
    )


@router.post("/probe-targets", response_model=ProbeRow, status_code=status.HTTP_200_OK)
async def create_probe_target(
    body: CreateProbeTargetRequest,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
    targets_repo: Annotated[TargetsRepository, Depends(_get_targets_repo)],
    probes_repo: Annotated[ProbeTargetsRepository, Depends(_get_probe_targets_repo)],
) -> ProbeRow:
    """Create (or upsert by (container_name, kind, name)) a probe target.

    config_source is always set to "manual" for direct-create. The existing
    /suggestions/{id}/accept path uses "discovered_accepted" and is unchanged.

    Auth: session + X-CSRF-Token.
    """
    from homelab_monitor.kernel.db.audit import insert_audit  # noqa: PLC0415
    from homelab_monitor.kernel.db.time import utc_now_iso  # noqa: PLC0415

    # 404 if container not in inventory.
    rows = await targets_repo.list_docker_containers(include_hidden=False)
    if not any(r.name == body.container_name for r in rows):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"container not found: {body.container_name}",
        )

    now = utc_now_iso()
    async with repo.transaction() as conn:
        probe_id = await ProbeTargetsRepository.upsert_probe_target_conn(
            conn,
            container_name=body.container_name,
            kind=body.kind,
            name=body.name,
            target_value=body.target_value,
            config_source="manual",
            enabled=True,
            interval_seconds=body.interval_seconds,
            timeout_seconds=body.timeout_seconds,
            now=now,
        )
        await insert_audit(
            conn,
            who=user.username,
            what="docker.probe.create",
            before=None,
            after={
                "probe_id": probe_id,
                "container_name": body.container_name,
                "kind": body.kind,
                "name": body.name,
                "config_source": "manual",
            },
        )
    after = await probes_repo.get_by_id(probe_id)
    if after is None:  # pragma: no cover -- defensive
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="probe vanished"
        )
    return _probe_row_to_dto(after)


@router.patch("/probe-targets/{probe_id}", response_model=ProbeRow)
async def update_probe_target(
    probe_id: str,
    body: UpdateProbeTargetRequest,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
    probes_repo: Annotated[ProbeTargetsRepository, Depends(_get_probe_targets_repo)],
) -> ProbeRow:
    """Update mutable probe-target fields. Returns the updated row.

    Mutable: target_value, interval_seconds, timeout_seconds.
    Immutable: kind, name, container_name (they form the logical UNIQUE key).
    """
    from homelab_monitor.kernel.db.audit import insert_audit  # noqa: PLC0415

    before = await probes_repo.get_by_id(probe_id)
    if before is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"probe not found: {probe_id}"
        )

    # Short-circuit empty-body PATCH (don't audit no-op).
    if body.target_value is None and body.interval_seconds is None and body.timeout_seconds is None:
        # Empty body: no fields to update. Return current state without auditing a no-op.
        return _probe_row_to_dto(before)

    async with repo.transaction() as conn:
        affected = await ProbeTargetsRepository.update_probe_target_conn(
            conn,
            probe_id=probe_id,
            target_value=body.target_value,
            interval_seconds=body.interval_seconds,
            timeout_seconds=body.timeout_seconds,
        )
        if affected == 0:  # pragma: no cover -- defensive race-condition guard
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="probe vanished during update"
            )
        await insert_audit(
            conn,
            who=user.username,
            what="docker.probe.update",
            before={
                "probe_id": probe_id,
                "container_name": before.container_name,
                "target_value": before.target_value,
                "interval_seconds": before.interval_seconds,
                "timeout_seconds": before.timeout_seconds,
            },
            after={
                "probe_id": probe_id,
                "container_name": before.container_name,
                "target_value": (
                    body.target_value if body.target_value is not None else before.target_value
                ),
                "interval_seconds": (
                    body.interval_seconds
                    if body.interval_seconds is not None
                    else before.interval_seconds
                ),
                "timeout_seconds": (
                    body.timeout_seconds
                    if body.timeout_seconds is not None
                    else before.timeout_seconds
                ),
            },
        )

    after = await probes_repo.get_by_id(probe_id)
    if after is None:  # pragma: no cover -- defensive
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="probe vanished"
        )
    return _probe_row_to_dto(after)


@router.delete(
    "/probe-targets/{probe_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={204: {"description": "Probe deleted"}, 404: {"description": "Probe not found"}},
)
async def delete_probe_target(
    probe_id: str,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
    probes_repo: Annotated[ProbeTargetsRepository, Depends(_get_probe_targets_repo)],
) -> None:
    """Hard-delete a probe target row.

    Distinct from disable (set_enabled_conn) and hide (mark_missing_except_conn).
    """
    from homelab_monitor.kernel.db.audit import insert_audit  # noqa: PLC0415

    before = await probes_repo.get_by_id(probe_id)
    if before is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"probe not found: {probe_id}"
        )
    async with repo.transaction() as conn:
        affected = await ProbeTargetsRepository.delete_probe_target_conn(conn, probe_id=probe_id)
        if affected == 0:  # pragma: no cover -- defensive
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="probe vanished during delete"
            )
        await insert_audit(
            conn,
            who=user.username,
            what="docker.probe.delete",
            before={
                "probe_id": probe_id,
                "container_name": before.container_name,
                "kind": before.kind,
                "name": before.name,
                "config_source": before.config_source,
            },
            after=None,
        )


async def _toggle_probe(  # noqa: PLR0913
    repo: SqliteRepository,
    probes_repo: ProbeTargetsRepository,
    probe_id: str,
    *,
    enabled: bool,
    user: User,
    what: str,
) -> ProbeRow:
    from homelab_monitor.kernel.db.audit import insert_audit  # noqa: PLC0415

    before = await probes_repo.get_by_id(probe_id)
    if before is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"probe not found: {probe_id}"
        )
    async with repo.transaction() as conn:
        await ProbeTargetsRepository.set_enabled_conn(
            conn,
            probe_id=probe_id,
            enabled=enabled,
        )
        await insert_audit(
            conn,
            who=user.username,
            what=what,
            before={
                "probe_id": probe_id,
                "enabled": before.enabled,
                "container_name": before.container_name,
            },
            after={
                "probe_id": probe_id,
                "enabled": enabled,
                "container_name": before.container_name,
            },
        )
    after = await probes_repo.get_by_id(probe_id)
    if after is None:  # pragma: no cover -- defensive; row exists
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="probe vanished"
        )
    return _probe_row_to_dto(after)


@router.get("/probes/summary", response_model=ProbeSummaryResponse)
async def get_probes_summary(
    request: Request,
    _user: Annotated[User, Depends(require_session())],
    probes_repo: Annotated[ProbeTargetsRepository, Depends(_get_probe_targets_repo)],
) -> ProbeSummaryResponse:
    """Return active + failing probe counts grouped by container, including
    per-source breakdown and any current override-file validation errors.

    Single query for the docker grid's per-row badge (avoids N+1).
    `source_breakdown` keys are config_source values ('label', 'file_override',
    etc.); `config_errors` is non-None only when the override loader currently
    has unresolved validation errors for that container.
    """
    # STAGE-003-007: thread the loader's current error map through so the
    # repo can attach errors to existing-container rows AND surface
    # orphan-file-error rows for containers without probes.
    loader = getattr(request.app.state, "override_loader", None)
    errors_mapping: dict[str, tuple[str, ...]] = (
        loader.current_errors_by_container() if loader is not None else {}
    )
    config_errors_by_container: dict[str, list[str]] = {
        k: list(v) for k, v in errors_mapping.items()
    }
    summaries = await probes_repo.summarize_by_container(
        config_errors_by_container=config_errors_by_container
    )
    return ProbeSummaryResponse(
        summaries=[
            ProbeSummaryEntry(
                container_name=s.container_name,
                active=s.active,
                failing=s.failing,
                source_breakdown=s.source_breakdown,
                config_errors=s.config_errors,
            )
            for s in summaries
        ]
    )


def _probe_row_to_dto(row: ProbeTargetRow) -> ProbeRow:
    return ProbeRow(
        id=row.id,
        container_name=row.container_name,
        kind=row.kind,
        name=row.name,
        target_value=row.target_value,
        config_source=row.config_source,
        enabled=row.enabled,
        interval_seconds=row.interval_seconds,
        timeout_seconds=row.timeout_seconds,
        last_run_at=row.last_run_at,
        last_status=row.last_status,
        last_error=row.last_error,
        created_at=row.created_at,
        hidden_at=row.hidden_at,
        exec_authorized=row.exec_authorized,
    )


class ImageUpdateSummaryEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    container_name: str
    available: bool
    source: Literal["registry", "local_build"] = "registry"
    # Registry-source fields (unchanged):
    current_digest: str | None = None
    latest_digest: str | None = None
    last_checked_at: str | None = None
    check_failed_at: str | None = None
    check_error_reason: str | None = None
    # Local-build-source fields (D-SUMMARY-SIBLING-ENDPOINT extension):
    compose_service: str | None = None
    build_context_path: str | None = None
    last_source_hash: str | None = None


class ImageUpdateSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summaries: list[ImageUpdateSummaryEntry]
    rate_limit_skipped_count: int = 0
    rate_limit_remaining_by_registry: dict[str, int] = {}


class ImageUpdateDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    container_name: str
    source: Literal["registry", "local_build"] = "registry"
    update_available: bool
    # Registry-source fields:
    last_local_digest: str | None = None
    last_registry_digest: str | None = None
    last_image_ref: str | None = None
    last_checked_at: str | None = None
    check_failed_at: str | None = None
    check_error_reason: str | None = None
    # Local-build fields:
    compose_service: str | None = None
    build_context_path: str | None = None
    last_source_hash: str | None = None
    baseline_source_hash: str | None = None


def _get_image_update_state_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> ImageUpdateStateRepository:
    return ImageUpdateStateRepository(repo)


def _get_docker_build_hashes_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> DockerBuildHashesRepository:
    return DockerBuildHashesRepository(repo)


@router.get("/image-updates/summary", response_model=ImageUpdateSummaryResponse)
async def get_image_updates_summary(
    request: Request,
    _user: Annotated[User, Depends(require_session())],
    state_repo: Annotated[ImageUpdateStateRepository, Depends(_get_image_update_state_repo)],
    build_repo: Annotated[DockerBuildHashesRepository, Depends(_get_docker_build_hashes_repo)],
) -> ImageUpdateSummaryResponse:
    """Aggregate image-update state across registry + local-build sources.

    D-SUMMARY-SIBLING-ENDPOINT extension: returns BOTH registry and
    local-build entries in a single list. Each entry carries `source` to
    discriminate. Rate-limit fields apply ONLY to registry source.
    Local-build presence supersedes registry for the SAME container_name
    (a container with both image: and build: in compose is a local build).
    """
    registry_rows = await state_repo.list_all()
    build_rows = await build_repo.list_all()
    build_names = {r.container_name for r in build_rows}

    summaries: list[ImageUpdateSummaryEntry] = []
    for r in registry_rows:
        if r.container_name in build_names:
            continue  # local-build wins
        summaries.append(
            ImageUpdateSummaryEntry(
                container_name=r.container_name,
                available=r.update_available,
                source="registry",
                current_digest=r.last_local_digest,
                latest_digest=r.last_registry_digest,
                last_checked_at=r.last_checked_at,
                check_failed_at=r.check_failed_at,
                check_error_reason=r.check_error_reason,
            )
        )
    for b in build_rows:
        summaries.append(
            ImageUpdateSummaryEntry(
                container_name=b.container_name,
                available=b.update_available,
                source="local_build",
                compose_service=b.compose_service,
                build_context_path=b.build_context_path,
                last_source_hash=b.last_source_hash,
                last_checked_at=b.last_checked_at,
                check_failed_at=b.check_failed_at,
                check_error_reason=b.check_error_reason,
            )
        )

    collector = getattr(request.app.state, "image_update_collector", None)
    skipped_count = collector.current_skipped_count() if collector is not None else 0
    remaining_view = dict(collector.current_rate_limit_remaining()) if collector is not None else {}
    return ImageUpdateSummaryResponse(
        summaries=summaries,
        rate_limit_skipped_count=skipped_count,
        rate_limit_remaining_by_registry=remaining_view,
    )


@router.get(
    "/containers/{name}/image-update",
    response_model=ImageUpdateDetail,
)
async def get_container_image_update(
    name: str,
    _user: Annotated[User, Depends(require_session())],
    state_repo: Annotated[ImageUpdateStateRepository, Depends(_get_image_update_state_repo)],
    build_repo: Annotated[DockerBuildHashesRepository, Depends(_get_docker_build_hashes_repo)],
) -> ImageUpdateDetail:
    """Per-container image-update detail; local-build supersedes registry."""
    build_row: DockerBuildHashRow | None = await build_repo.get_by_container(name)
    if build_row is not None:
        return ImageUpdateDetail(
            container_name=build_row.container_name,
            source="local_build",
            update_available=build_row.update_available,
            compose_service=build_row.compose_service,
            build_context_path=build_row.build_context_path,
            last_source_hash=build_row.last_source_hash,
            baseline_source_hash=build_row.baseline_source_hash,
            last_checked_at=build_row.last_checked_at,
            check_failed_at=build_row.check_failed_at,
            check_error_reason=build_row.check_error_reason,
        )

    row: ImageUpdateStateRow | None = await state_repo.get_by_container(name)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no image-update state for container: {name}",
        )
    return ImageUpdateDetail(
        container_name=row.container_name,
        source="registry",
        update_available=row.update_available,
        last_local_digest=row.last_local_digest,
        last_registry_digest=row.last_registry_digest,
        last_image_ref=row.last_image_ref,
        last_checked_at=row.last_checked_at,
        check_failed_at=row.check_failed_at,
        check_error_reason=row.check_error_reason,
    )


def _get_compose_actions_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> ComposeActionsRepository:
    return ComposeActionsRepository(repo)


def _get_compose_action_runner(request: Request) -> ComposeActionRunner:
    runner = getattr(request.app.state, "compose_action_runner", None)
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="compose action runner is not initialized",
        )
    return runner


# ----------------------------------------------------------------------
# STAGE-003-010 — Pull & Restart confirm-gated action.
# ----------------------------------------------------------------------

_CONFIRM_PHRASE: Literal["pull"] = "pull"


class ActionInProgressDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")
    error_code: Literal["action_in_progress"] = "action_in_progress"
    in_flight_action_id: int
    container_name: str
    state: str


class PullAndRestartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    confirm_phrase: str


class PullAndRestartAcceptedResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action_id: int
    state: Literal["pulling", "building", "restarting", "failed"]


class ComposeActionDetailResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action_id: int
    action: str
    container_name: str
    compose_service: str
    before_image: str | None = None
    before_digest: str | None = None
    after_image: str | None = None
    after_digest: str | None = None
    command: str
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    state: str
    error_reason: str | None = None
    started_at: str
    ended_at: str | None = None
    duration_seconds: float | None = None
    who: str
    client_ip: str | None = None


class ComposeActionListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    actions: list[ComposeActionDetailResponse]


def _row_to_detail(
    row: ComposeActionRow,
) -> ComposeActionDetailResponse:
    """Convert ComposeActionRow → ComposeActionDetailResponse."""
    return ComposeActionDetailResponse(
        action_id=row.id,
        action=row.action,
        container_name=row.container_name,
        compose_service=row.compose_service,
        before_image=row.before_image,
        before_digest=row.before_digest,
        after_image=row.after_image,
        after_digest=row.after_digest,
        command=row.command,
        stdout=row.stdout,
        stderr=row.stderr,
        exit_code=row.exit_code,
        state=row.state,
        error_reason=row.error_reason,
        started_at=row.started_at,
        ended_at=row.ended_at,
        duration_seconds=row.duration_seconds,
        who=row.who,
        client_ip=row.client_ip,
    )


@router.post(
    "/containers/{name}/pull-and-restart",
    response_model=PullAndRestartAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def pull_and_restart_container(  # noqa: PLR0913 -- FastAPI route with injected dependencies
    name: str,
    body: PullAndRestartRequest,
    request: Request,
    principal: Annotated[User | ApiToken, Depends(require_user_or_token({Scope.DOCKER_WRITE}))],
    runner: Annotated[ComposeActionRunner, Depends(_get_compose_action_runner)],
    actions_repo: Annotated[ComposeActionsRepository, Depends(_get_compose_actions_repo)],
    targets_repo: Annotated[TargetsRepository, Depends(_get_targets_repo)],
) -> PullAndRestartAcceptedResponse:
    """Trigger a Pull & Restart for `name`. Returns 202 with action_id.

    Confirm: body.confirm_phrase must equal 'pull' (case-insensitive).
    Auth: session OR API token with Scope.DOCKER_WRITE.
    """
    if body.confirm_phrase.strip().lower() != _CONFIRM_PHRASE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"confirm_phrase must equal '{_CONFIRM_PHRASE}'",
        )
    # 404 if the container is not even discovered.
    rows = await targets_repo.list_docker_containers(include_hidden=False)
    if not any(r.name == name for r in rows):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"container not found: {name}",
        )
    # 409 if an action is already in flight for this container.
    in_flight = await actions_repo.get_active_for_container(name)
    if in_flight is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ActionInProgressDetail(
                in_flight_action_id=in_flight.id,
                container_name=name,
                state=in_flight.state,
            ).model_dump(),
        )
    # Determine who + client_ip from auth principal.
    who = principal.username if isinstance(principal, User) else f"token:{principal.name}"
    client_ip = request.client.host if request.client is not None else None
    action_id = await runner.trigger_pull_and_restart(
        container_name=name, who=who, client_ip=client_ip
    )
    # The runner returns the action_id. State at this point is normally
    # "pulling"; if pre-resolution failed it's already "failed".
    row = await actions_repo.get_by_id(action_id)
    state: Literal["pulling", "restarting", "running", "failed"] = "pulling"
    if row is not None and row.state == "failed":
        state = "failed"
    return PullAndRestartAcceptedResponse(action_id=action_id, state=state)


@router.get(
    "/compose-actions/{action_id}",
    response_model=ComposeActionDetailResponse,
)
async def get_compose_action(
    action_id: int,
    _user: Annotated[User, Depends(require_session())],
    actions_repo: Annotated[ComposeActionsRepository, Depends(_get_compose_actions_repo)],
) -> ComposeActionDetailResponse:
    """Fetch one compose action's full record (stdout/stderr included)."""
    row = await actions_repo.get_by_id(action_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"compose action not found: {action_id}",
        )
    return _row_to_detail(row)


@router.get(
    "/compose-actions",
    response_model=ComposeActionListResponse,
)
async def list_compose_actions(
    _user: Annotated[User, Depends(require_session())],
    actions_repo: Annotated[ComposeActionsRepository, Depends(_get_compose_actions_repo)],
    container: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> ComposeActionListResponse:
    """List recent actions for one container. `container` query param required."""
    rows = await actions_repo.list_for_container(container_name=container, limit=limit)
    return ComposeActionListResponse(actions=[_row_to_detail(r) for r in rows])


# ======================================================================
# STAGE-003-012 — Accept / Customize / Ignore endpoints.
# ======================================================================


def _get_docker_socket_client(request: Request) -> DockerSocketClient | None:
    """Return the singleton DockerSocketClient if available, else None.

    Lifespan sets `app.state.docker_socket_client` when the socket is
    reachable. If absent, Accept silently skips inspect-based probe
    inference (logs a warning, returns probes_created=0).
    """
    from homelab_monitor.kernel.docker.socket_client import DockerSocketClient  # noqa: PLC0415

    client = getattr(request.app.state, "docker_socket_client", None)
    if isinstance(client, DockerSocketClient):
        return client
    return None


def _default_probes_from_inspect(  # noqa: PLR0912 -- complex inspect-shape handling
    inspect: dict[str, Any],
) -> list[ProbeSpec]:
    """Compute the conservative default probe set for an accepted suggestion.

    Per D-ACCEPT-INVENTORY-ONLY-WITH-CONSERVATIVE-PROBES:
      Probes are inserted ONLY when BOTH:
        - container has at least one exposed port, AND
        - container has a healthcheck defined.
      One `tcp` probe per exposed port (target_value="tcp://host.docker.internal:<port>")
      One `exec` probe wrapping the healthcheck command (joined with shell quoting via
      shlex when the docker Healthcheck.Test is a `["CMD", ...]` or `["CMD-SHELL", ...]` form).
    """
    import shlex  # noqa: PLC0415

    config: Any = inspect.get("Config")  # type: ignore[assignment]
    host_config: Any = inspect.get("HostConfig")  # type: ignore[assignment]
    if not isinstance(config, dict) or not isinstance(host_config, dict):
        return []

    # Collect exposed ports from BOTH HostConfig.PortBindings and Config.ExposedPorts.
    # PortBindings has {"<port>/<proto>": [{"HostIp": "...", "HostPort": "..."}], ...}
    # ExposedPorts has {"<port>/<proto>": {}}
    ports: set[int] = set()
    bindings: Any = host_config.get("PortBindings")  # type: ignore[assignment]
    if isinstance(bindings, dict):
        for port_spec in cast(dict[str, Any], bindings):
            port = _parse_port_spec(str(port_spec))
            if port is not None:
                ports.add(port)
    exposed: Any = config.get("ExposedPorts")  # type: ignore[assignment]
    if isinstance(exposed, dict):
        for port_spec in cast(dict[str, Any], exposed):
            port = _parse_port_spec(str(port_spec))
            if port is not None:
                ports.add(port)

    # Extract healthcheck command.
    healthcheck: Any = config.get("Healthcheck")  # type: ignore[assignment]
    hc_cmd: str | None = None
    if isinstance(healthcheck, dict):
        test: Any = healthcheck.get("Test")  # type: ignore[assignment]
        if isinstance(test, list) and len(test) > 0:  # type: ignore[arg-type]
            # Common forms: ["CMD-SHELL", "<shell-cmd>"], ["CMD", "executable", "arg1", ...],
            # or ["NONE"] (no healthcheck — skip).
            test_list = cast(list[Any], test)
            head = str(test_list[0]) if test_list else ""
            if head == "CMD-SHELL" and len(test_list) >= _HEALTHCHECK_TEST_MIN_LENGTH:
                hc_cmd = str(test_list[1])
            elif head == "CMD" and len(test_list) >= _HEALTHCHECK_TEST_MIN_LENGTH:
                hc_cmd = " ".join(shlex.quote(str(x)) for x in test_list[1:])
            # Anything else (NONE, empty, unknown shape) → no exec probe.

    # D-ACCEPT-INVENTORY-ONLY-WITH-CONSERVATIVE-PROBES gate.
    if not ports or hc_cmd is None:
        return []

    out: list[ProbeSpec] = []
    for _i, port in enumerate(sorted(ports)):
        out.append(
            ProbeSpec(
                kind="tcp",
                name=f"tcp-{port}",
                target_value=f"tcp://host.docker.internal:{port}",
                interval_seconds=60,
                timeout_seconds=10,
            )
        )
    out.append(
        ProbeSpec(
            kind="exec",
            name="healthcheck",
            target_value=hc_cmd,
            interval_seconds=60,
            timeout_seconds=10,
        )
    )
    return out


def _parse_port_spec(spec: str) -> int | None:
    """Parse "<port>/<proto>" or "<port>" → int port. Returns None on failure."""
    port_part = spec.split("/", 1)[0]
    try:
        return int(port_part)
    except ValueError:
        return None


@router.post(
    "/suggestions/{suggestion_id}/accept",
    response_model=SuggestionAcceptResponse,
)
async def accept_suggestion(  # noqa: PLR0913 -- FastAPI deps
    suggestion_id: str,
    body: SuggestionAcceptRequest,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
    suggestions_repo: Annotated[SuggestionsRepository, Depends(_get_suggestions_repo)],
) -> SuggestionAcceptResponse:
    """Accept a pending Docker suggestion.

    Per D-IDEMPOTENT-200:
      - state='pending' → transition to 'accepted', optionally insert default probes.
      - state='accepted' → 200 no-op (no duplicate probes).
      - state='ignored' or 'container_gone' → 409 Conflict.
      - missing → 404.
    """
    from homelab_monitor.kernel.db.audit import insert_audit  # noqa: PLC0415
    from homelab_monitor.kernel.db.time import utc_now_iso  # noqa: PLC0415

    sugg = await suggestions_repo.get_docker_suggestion_by_id(suggestion_id)
    if sugg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"suggestion not found: {suggestion_id}",
        )

    if sugg.state == "accepted":
        # Idempotent no-op.
        return SuggestionAcceptResponse(
            suggestion=_sugg_row_to_dto(sugg),
            probes_created=0,
        )

    if sugg.state in ("ignored", "container_gone"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot accept suggestion in state {sugg.state!r}",
        )

    # Compute default probes (only if apply_default_probes=true AND we can reach docker).
    probes_to_insert: list[ProbeSpec] = []
    if body.apply_default_probes:
        client = _get_docker_socket_client(request)
        if client is not None:
            try:
                inspect = await client.inspect_container(sugg.container_id)
                probes_to_insert = _default_probes_from_inspect(cast("dict[str, object]", inspect))
            except Exception:
                probes_to_insert = []

    now = utc_now_iso()
    probes_created = 0
    async with repo.transaction() as conn:
        await SuggestionsRepository.set_state_conn(
            conn,
            suggestion_id=suggestion_id,
            new_state="accepted",
            now=now,
        )
        for spec in probes_to_insert:  # pragma: no cover -- async-for inside async with
            await ProbeTargetsRepository.upsert_probe_target_conn(
                conn,
                container_name=sugg.container_name,
                kind=spec.kind,
                name=spec.name,
                target_value=spec.target_value,
                config_source="discovered_accepted",
                enabled=True,
                interval_seconds=spec.interval_seconds,
                timeout_seconds=spec.timeout_seconds,
                now=now,
            )
            probes_created += 1  # pragma: no cover -- same instrumentation gap
        await insert_audit(
            conn,
            who=user.username,
            what="docker.suggestion.accept",
            before={
                "suggestion_id": suggestion_id,
                "state": sugg.state,
                "container_name": sugg.container_name,
            },
            after={
                "suggestion_id": suggestion_id,
                "state": "accepted",
                "container_name": sugg.container_name,
                "probes_created": probes_created,
            },
        )

    refreshed = await suggestions_repo.get_docker_suggestion_by_id(suggestion_id)
    if refreshed is None:  # pragma: no cover -- defensive
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="suggestion vanished"
        )
    return SuggestionAcceptResponse(
        suggestion=_sugg_row_to_dto(refreshed),
        probes_created=probes_created,
    )


@router.post(
    "/suggestions/{suggestion_id}/customize",
    response_model=SuggestionCustomizeResponse,
)
async def customize_suggestion(  # noqa: PLR0913
    suggestion_id: str,
    body: SuggestionCustomizeRequest,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
    suggestions_repo: Annotated[SuggestionsRepository, Depends(_get_suggestions_repo)],
    probes_repo: Annotated[ProbeTargetsRepository, Depends(_get_probe_targets_repo)],
) -> SuggestionCustomizeResponse:
    """Customize-accept a suggestion with user-supplied probe specs.

    Per D-IDEMPOTENT-200:
      - state in {'pending', 'accepted'} → upsert probes + ensure state='accepted'.
      - state in {'ignored', 'container_gone'} → 409 Conflict.
      - missing → 404.
    Server-side validation of probe uniqueness (kind, name) within the request body.
    """
    from homelab_monitor.kernel.db.audit import insert_audit  # noqa: PLC0415
    from homelab_monitor.kernel.db.time import utc_now_iso  # noqa: PLC0415

    sugg = await suggestions_repo.get_docker_suggestion_by_id(suggestion_id)
    if sugg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"suggestion not found: {suggestion_id}",
        )
    if sugg.state in ("ignored", "container_gone"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot customize suggestion in state {sugg.state!r}",
        )

    # Server-side defense-in-depth: reject duplicate (kind, name) within request body.
    keys = [(p.kind, p.name) for p in body.probes]
    if len(set(keys)) != len(keys):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="duplicate probe (kind, name) in request body",
        )

    # Pre-read existing probes to compute created-vs-updated counts.
    existing = await probes_repo.list_for_container(
        container_name=sugg.container_name, include_hidden=True
    )
    existing_keys = {(p.kind, p.name) for p in existing}
    probes_created = 0
    probes_updated = 0
    for spec in body.probes:
        if (spec.kind, spec.name) in existing_keys:
            probes_updated += 1
        else:
            probes_created += 1

    now = utc_now_iso()
    async with repo.transaction() as conn:
        if sugg.state != "accepted":
            await SuggestionsRepository.set_state_conn(
                conn,
                suggestion_id=suggestion_id,
                new_state="accepted",
                now=now,
            )
        for spec in body.probes:  # pragma: no branch -- body.probes validated non-empty by Pydantic
            await ProbeTargetsRepository.upsert_probe_target_conn(
                conn,
                container_name=sugg.container_name,
                kind=spec.kind,
                name=spec.name,
                target_value=spec.target_value,
                config_source="discovered_accepted",
                enabled=True,
                interval_seconds=spec.interval_seconds,
                timeout_seconds=spec.timeout_seconds,
                now=now,
            )
        await insert_audit(
            conn,
            who=user.username,
            what="docker.suggestion.customize",
            before={
                "suggestion_id": suggestion_id,
                "state": sugg.state,
                "container_name": sugg.container_name,
            },
            after={
                "suggestion_id": suggestion_id,
                "state": "accepted",
                "container_name": sugg.container_name,
                "probes_created": probes_created,
                "probes_updated": probes_updated,
            },
        )

    refreshed = await suggestions_repo.get_docker_suggestion_by_id(suggestion_id)
    if refreshed is None:  # pragma: no cover -- defensive
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="suggestion vanished"
        )
    return SuggestionCustomizeResponse(
        suggestion=_sugg_row_to_dto(refreshed),
        probes_created=probes_created,
        probes_updated=probes_updated,
    )


@router.post(
    "/suggestions/{suggestion_id}/ignore",
    response_model=SuggestionIgnoreResponse,
)
async def ignore_suggestion(
    suggestion_id: str,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[SqliteRepository, Depends(get_repo)],
    suggestions_repo: Annotated[SuggestionsRepository, Depends(_get_suggestions_repo)],
) -> SuggestionIgnoreResponse:
    """Ignore a suggestion from ANY state (D-IGNORE-FROM-ANY-STATE).

    Per D-NO-PROBE-MISSING-ON-IGNORE: existing probe_targets rows are NOT
    touched. The user manages probe cleanup separately via the probe-disable
    UI from STAGE-003-007.
    """
    from homelab_monitor.kernel.db.audit import insert_audit  # noqa: PLC0415
    from homelab_monitor.kernel.db.time import utc_now_iso  # noqa: PLC0415

    sugg = await suggestions_repo.get_docker_suggestion_by_id(suggestion_id)
    if sugg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"suggestion not found: {suggestion_id}",
        )
    if sugg.state == "ignored":
        return SuggestionIgnoreResponse(suggestion=_sugg_row_to_dto(sugg))

    now = utc_now_iso()
    async with repo.transaction() as conn:
        await SuggestionsRepository.set_state_conn(
            conn,
            suggestion_id=suggestion_id,
            new_state="ignored",
            now=now,
        )
        await insert_audit(
            conn,
            who=user.username,
            what="docker.suggestion.ignore",
            before={
                "suggestion_id": suggestion_id,
                "state": sugg.state,
                "container_name": sugg.container_name,
            },
            after={
                "suggestion_id": suggestion_id,
                "state": "ignored",
                "container_name": sugg.container_name,
            },
        )
    refreshed = await suggestions_repo.get_docker_suggestion_by_id(suggestion_id)
    if refreshed is None:  # pragma: no cover -- defensive
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="suggestion vanished"
        )
    return SuggestionIgnoreResponse(suggestion=_sugg_row_to_dto(refreshed))


@router.get(
    "/suggestions/{suggestion_id}/default-probes",
    response_model=SuggestionDefaultProbesResponse,
)
async def get_suggestion_default_probes(
    suggestion_id: str,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    suggestions_repo: Annotated[SuggestionsRepository, Depends(_get_suggestions_repo)],
) -> SuggestionDefaultProbesResponse:
    sugg = await suggestions_repo.get_docker_suggestion_by_id(suggestion_id)
    if sugg is None:
        raise HTTPException(status_code=404, detail="suggestion not found")

    client = _get_docker_socket_client(request)
    if client is None:
        return SuggestionDefaultProbesResponse(probes=[], reason="docker_unavailable")

    try:
        inspect = await client.inspect_container(sugg.container_id)
    except Exception:
        return SuggestionDefaultProbesResponse(probes=[], reason="container_gone")

    probes = _default_probes_from_inspect(cast("dict[str, Any]", inspect))
    if not probes:
        return SuggestionDefaultProbesResponse(probes=[], reason="no_ports_no_healthcheck")
    return SuggestionDefaultProbesResponse(probes=probes, reason="available")


def _sugg_row_to_dto(row: DockerSuggestionRepoRow) -> DockerSuggestionRow:
    """Convert a repo DockerSuggestionRow dataclass → API DockerSuggestionRow model."""
    return DockerSuggestionRow(
        id=row.id,
        kind=row.kind,
        deduplication_key=row.deduplication_key,
        state=row.state,
        created_at=row.created_at,
        updated_at=row.updated_at,
        container_id=row.container_id,
        container_name=row.container_name,
        image_ref=row.image_ref,
        labels=row.labels,
        compose_project=row.compose_project,
        compose_service=row.compose_service,
        compose_file_path=row.compose_file_path,
        detection_reason=row.detection_reason,
    )
