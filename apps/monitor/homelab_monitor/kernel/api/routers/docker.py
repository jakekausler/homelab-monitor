"""GET /api/integrations/docker/containers — session-auth.

Single endpoint returning ContainerRow[] (matches UI contract in
apps/ui/src/routes/integrations/types.ts). Cadvisor fields (cpu_pct, mem_mib)
come from the SQLite cache populated by DockerSocketCollector's VM merge
step (T-MERGE-LOCATION) — sub-10ms read, no live VM query.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.api.dependencies import (
    get_repo,
    require_session,
    require_user_or_token,
)
from homelab_monitor.kernel.auth.models import ApiToken, User
from homelab_monitor.kernel.auth.scopes import Scope
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
from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.compose_action_runner import ComposeActionRunner

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
