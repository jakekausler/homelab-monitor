"""GET /api/integrations/docker/containers — session-auth.

Single endpoint returning ContainerRow[] (matches UI contract in
apps/ui/src/routes/integrations/types.ts). Cadvisor fields (cpu_pct, mem_mib)
come from the SQLite cache populated by DockerSocketCollector's VM merge
step (T-MERGE-LOCATION) — sub-10ms read, no live VM query.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.api.dependencies import get_repo, require_session
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.db.repositories.probe_targets_repository import (
    ProbeTargetRow,
    ProbeTargetsRepository,
)
from homelab_monitor.kernel.db.repositories.suggestions_repository import (
    ALLOWED_STATES,
    SuggestionsRepository,
)
from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository

router = APIRouter(prefix="/integrations/docker", tags=["docker"])


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


def _get_targets_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> TargetsRepository:
    """Construct a TargetsRepository from the injected SqliteRepository."""
    return TargetsRepository(repo)


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
    kind: str  # 'docker_container_discovered' | 'docker_label_collision'
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


def _get_suggestions_repo(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> SuggestionsRepository:
    return SuggestionsRepository(repo)


_SUGGESTION_STATUS_QUERY = Literal["pending", "accepted", "ignored", "container_gone", "all"]
_DEFAULT_SUGGESTION_PAGE_SIZE: int = 50
_MAX_SUGGESTION_PAGE_SIZE: int = 200


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


class ListProbesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    probes: list[ProbeRow]


class ProbeSummaryEntry(BaseModel):
    """Per-container probe counts."""

    model_config = ConfigDict(extra="ignore")

    container_name: str
    active: int  # count of enabled probes
    failing: int  # count of enabled probes with last_status='fail' or 'error'


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
    _user: Annotated[User, Depends(require_session())],
    probes_repo: Annotated[ProbeTargetsRepository, Depends(_get_probe_targets_repo)],
) -> ProbeSummaryResponse:
    """Return active + failing probe counts grouped by container.

    Single query; intended for the docker grid's per-row badge to avoid an N+1
    query pattern. Containers with zero probes are NOT returned (caller
    interprets absence as 'no probes').
    """
    summaries = await probes_repo.summarize_by_container()
    return ProbeSummaryResponse(
        summaries=[
            ProbeSummaryEntry(
                container_name=s.container_name,
                active=s.active,
                failing=s.failing,
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
    )
