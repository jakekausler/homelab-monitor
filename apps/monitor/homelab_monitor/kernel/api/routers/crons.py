"""GET/PATCH/DELETE /api/crons — cron registry CRUD (session-auth).

Authentication: session-only (operator dashboard surface). State-changing
methods (PATCH/DELETE) inherit CSRF enforcement from
``require_session()`` automatically — there is no separate
``require_session_with_csrf`` factory in this codebase.

Audit verbs (recorded by ``CronRepo``):
- ``crons.update`` (PATCH that changes a non-hidden_at field)
- ``crons.hide`` (PATCH hidden_at non-null OR DELETE)
- ``crons.unhide`` (PATCH hidden_at -> null)

Preview endpoints (read-only, GET):
- ``GET /api/crons/{fingerprint}/preview-runs?count=N`` — saved cron
- ``GET /api/crons/preview-runs?expr=<cron>&count=N`` — unsaved expression.
  Both go through the SAME croniter helper so UI preview cannot drift from
  server-side validation.
"""

from __future__ import annotations

import asyncio
import os
import time as _time
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import PlainTextResponse
from starlette.responses import Response as _FastApiResponse

from homelab_monitor.kernel.api.dependencies import (
    get_cron_repo,
    require_session,
    require_token_scope,
)
from homelab_monitor.kernel.api.errors import (
    DependencyUnavailableProblem,
    NotFoundProblem,
    TooManyRequestsProblem,
)
from homelab_monitor.kernel.auth.models import ApiToken, User
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.cron.discovery_types import CronScanResult
from homelab_monitor.kernel.cron.repository import CronRepo, CronWithState
from homelab_monitor.kernel.cron.schedule import (
    InvalidCronExpression,
    compute_next_runs,
)
from homelab_monitor.kernel.cron.schemas import (
    CronListQuery,
    CronListResponse,
    CronOut,
    CrontabDiffOut,
    CronUpdate,
    CronWithStateOut,
    HeartbeatStateOut,
    InstallWrapperPreview,
    InstallWrapperRequest,
    InstallWrapperResult,
    PreviewRunsQuery,
    PreviewRunsResponse,
    UninstallWrapperPreview,
    UninstallWrapperRequest,
    UninstallWrapperResult,
    cron_record_to_out,
)
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.heartbeat.schemas import query_model
from homelab_monitor.plugins.discoverers.cron_discoverer import resolve_hostname

router = APIRouter(prefix="/crons", tags=["crons"])

# Throttle state for discover-now endpoint
_DISCOVER_NOW_THROTTLE_SECONDS = 10.0
_discover_now_lock = asyncio.Lock()
_discover_now_last_call: float = 0.0


def _client_ip(request: Request) -> str | None:
    if request.client is not None:
        return request.client.host
    return None  # pragma: no cover -- defensive


def _with_state_to_out(joined: CronWithState, *, local_hostname: str) -> CronWithStateOut:
    state_out: HeartbeatStateOut | None = None
    if joined.state is not None:  # pragma: no cover
        s = joined.state
        state_out = HeartbeatStateOut(
            cron_fingerprint=s.cron_fingerprint,
            current_state=s.current_state,  # type: ignore[arg-type]
            last_start_at=s.last_start_at,
            last_ok_at=s.last_ok_at,
            last_fail_at=s.last_fail_at,
            current_streak=s.current_streak,
            expected_next_at=s.expected_next_at,
            last_duration_seconds=s.last_duration_seconds,
            last_exit_code=s.last_exit_code,
            updated_at=s.updated_at,
        )
    return CronWithStateOut(
        cron=cron_record_to_out(joined.cron, local_hostname=local_hostname),
        state=state_out,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=CronListResponse)
async def list_crons(
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
    query: Annotated[CronListQuery, Depends(query_model(CronListQuery))],
) -> CronListResponse:
    """Paginated list of crons with optional filters."""
    page = await repo.list_crons(
        page=query.page,
        page_size=query.page_size,
        host=query.host,
        enabled=query.enabled,
        state=query.state,
        q=query.q,
        include_hidden=query.include_hidden,
        include_soft_deleted=query.include_soft_deleted,
        wrapper_installed=query.wrapper_installed,
    )
    local_hostname = resolve_hostname()
    return CronListResponse(
        items=[cron_record_to_out(r, local_hostname=local_hostname) for r in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


# Preview endpoints come BEFORE the {fingerprint} routes so FastAPI matches
# `/preview-runs` as a literal segment instead of trying to bind it as fingerprint.


@router.get("/preview-runs", response_model=PreviewRunsResponse)
async def preview_runs_unsaved(
    _user: Annotated[User, Depends(require_session())],
    query: Annotated[PreviewRunsQuery, Depends(query_model(PreviewRunsQuery))],
) -> PreviewRunsResponse:
    """Preview the next N fire times for an unsaved cron expression.

    Used by UI clients to validate or preview an unsaved cron expression. ``expr``
    is REQUIRED for this endpoint (the saved-cron variant omits it).
    """
    if query.expr is None:
        # query_model + Field validator rejects only if expr is supplied AND
        # invalid. Missing expr is a routing error here.
        raise NotFoundProblem(
            message="missing required query parameter: expr",
        )
    try:
        runs = compute_next_runs(query.expr, count=query.count)
    except (
        InvalidCronExpression
    ) as exc:  # pragma: no cover -- defense in depth, validator rejects bad expr first
        # Defense in depth: PreviewRunsQuery already validates, but if a
        # caller bypasses that path we surface 422-shaped error.
        raise NotFoundProblem(message=str(exc)) from exc
    return PreviewRunsResponse(runs=runs)


@router.get("/{fingerprint}/preview-runs", response_model=PreviewRunsResponse)
async def preview_runs_saved(
    fingerprint: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
    query: Annotated[PreviewRunsQuery, Depends(query_model(PreviewRunsQuery))],
) -> PreviewRunsResponse:
    """Preview the next N fire times for a saved cron's schedule.

    404 if the cron is missing OR has no schedule (cadence-only cron has no
    schedule to preview)."""
    cron = await repo.get_cron(fingerprint, include_hidden=False)
    if cron is None:
        raise NotFoundProblem(message=f"cron not found: {fingerprint}")
    if not cron.schedule:
        raise NotFoundProblem(
            message=f"cron has no schedule (cadence-only): {fingerprint}",
        )
    runs = compute_next_runs(cron.schedule, count=query.count)
    return PreviewRunsResponse(runs=runs)


@router.get("/wrapper-template", response_class=PlainTextResponse)
async def get_wrapper_template(
    _token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
) -> PlainTextResponse:
    """Return the raw cron heartbeat-wrapper script template (text/plain).

    Served so the standalone remote installer can fetch the canonical
    template instead of embedding its own divergent copy. The template
    carries no secrets; HEARTBEAT_WRITE scope is required for uniform API
    authentication (the remote installer already holds that token).

    The four placeholders ({{INSTALL_DATE}}, {{FINGERPRINT}},
    {{HEARTBEAT_URL_BASE}}, {{TOKEN_FILE_PATH}}) are substituted by the
    caller, identically to install.py:_build_wrapper_content().
    """
    template_text = (
        _resource_files("homelab_monitor")
        .joinpath("data", "cron-with-heartbeat.sh.tmpl")
        .read_text(encoding="utf-8")
    )
    return PlainTextResponse(content=template_text)


@router.get("/{fingerprint}", response_model=CronWithStateOut)
async def get_cron(
    fingerprint: str,
    _user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
    include_hidden: bool = False,
) -> CronWithStateOut:
    """Return a single cron + its joined heartbeat state.

    By default hidden crons return 404; pass ``?include_hidden=true``
    for admin recovery flows. Soft-deleted crons are ALWAYS returned by this
    endpoint (direct fetch is unfiltered for soft-delete; STAGE-002-007A).
    """
    joined = await repo.get_cron_with_state(fingerprint, include_hidden=include_hidden)
    if joined is None:
        raise NotFoundProblem(message=f"cron not found: {fingerprint}")
    return _with_state_to_out(joined, local_hostname=resolve_hostname())


@router.patch("/{fingerprint}", response_model=CronOut)
async def update_cron(
    fingerprint: str,
    payload: CronUpdate,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
) -> CronOut:
    """Partial update of a cron. Empty diff returns 200 with no audit row."""
    try:
        rec = await repo.update_cron(
            fingerprint, payload, who=user.username, ip=_client_ip(request)
        )
    except LookupError as exc:
        raise NotFoundProblem(message=str(exc)) from exc
    return cron_record_to_out(rec, local_hostname=resolve_hostname())


@router.delete("/{fingerprint}", status_code=204)
async def delete_cron(
    fingerprint: str,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
) -> None:
    """Soft-delete (hide) a cron. 404 if missing OR already hidden."""
    try:
        await repo.soft_delete_cron(fingerprint, who=user.username, ip=_client_ip(request))
    except LookupError as exc:
        raise NotFoundProblem(message=str(exc)) from exc


@router.post(
    "/{fingerprint}/install-wrapper",
    responses={
        200: {"description": "Dry-run preview OR install result"},
        400: {"description": "Cron is on a remote host or public URL not configured"},
        404: {"description": "Cron not found"},
        409: {"description": "Crontab line not found, or already wrapped"},
        503: {"description": "Host-side cron-apply executor unavailable"},
        500: {"description": "Install failed; rollback performed"},
    },
)
async def install_wrapper(  # noqa: PLR0912 -- explicit per-exception-type HTTP status mapping (4xx/5xx) for each install failure mode
    fingerprint: str,
    payload: InstallWrapperRequest,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
) -> InstallWrapperPreview | InstallWrapperResult:
    """Install (or dry-run preview) the heartbeat wrapper for a local cron.

    confirm=false → InstallWrapperPreview (no file modifications).
    confirm=true  → performs the install, returns InstallWrapperResult.
    Session-auth; CSRF enforced automatically by require_session() on POST.
    """
    from homelab_monitor.kernel.config import get_public_url  # noqa: PLC0415
    from homelab_monitor.kernel.cron.install import (  # noqa: PLC0415
        AlreadyWrappedError,
        CronApplyUnavailableError,
        CronLineNotFoundError,
        CrontabWriteError,
        RemoteHostError,
        build_install_kit,
        install_wrapper_local,
    )

    bound_log = structlog.get_logger().bind(fingerprint=fingerprint)

    # Resolve local hostname and public URL
    local_hostname = resolve_hostname()
    public_url = get_public_url()
    if not public_url:
        raise HTTPException(
            status_code=400,
            detail="HOMELAB_MONITOR_PUBLIC_URL is not configured",
        )

    # Fetch cron
    cron = await repo.get_cron(fingerprint, include_hidden=True)
    if cron is None:
        raise NotFoundProblem(message=f"cron not found: {fingerprint}")

    # Check host
    if cron.host != local_hostname:
        raise HTTPException(
            status_code=400,
            detail=f"cron is on remote host {cron.host!r}; remote wrapping ships in EPIC-017",
        )

    # Resolve host root and install date
    host_root = Path(os.environ.get("HM_CRON_HOST_ROOT", "/host"))
    install_date = utc_now_iso()[:10]

    # Dry-run path (confirm=false)
    if not payload.confirm:
        try:
            kit = await build_install_kit(
                cron, host_root=host_root, public_url=public_url, install_date=install_date
            )
        except CronLineNotFoundError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AlreadyWrappedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RemoteHostError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return InstallWrapperPreview(
            fingerprint=kit.fingerprint,
            wrapper_path=kit.wrapper_path,
            wrapper_content=kit.wrapper_content,
            token_file_path=kit.token_file_path,
            crontab_diff=CrontabDiffOut(
                source_path=kit.crontab_diff.source_path,
                old_line=kit.crontab_diff.old_line,
                new_line=kit.crontab_diff.new_line,
            ),
        )

    # Confirm path (confirm=true)
    # Need auth_repo and secrets_repo from app.state
    auth_repo = getattr(request.app.state, "auth_repo", None)
    secrets_repo = getattr(request.app.state, "secrets_repo", None)

    if auth_repo is None:
        raise DependencyUnavailableProblem(
            code="auth_repo_unavailable",
            message="auth repository not available",
        )
    if secrets_repo is None:
        raise DependencyUnavailableProblem(
            code="secrets_repo_unavailable",
            message="secrets repository not available",
        )

    try:
        updated_cron = await install_wrapper_local(
            fingerprint,
            cron_repo=repo,
            auth_repo=auth_repo,
            secrets_repo=secrets_repo,
            host_root=host_root,
            public_url=public_url,
            local_hostname=local_hostname,
            who=user.username,
            ip=_client_ip(request),
            log=bound_log,
        )
    except RemoteHostError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CronLineNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AlreadyWrappedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CronApplyUnavailableError as exc:
        bound_log.error("install_wrapper.executor_unavailable", error=str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CrontabWriteError as exc:
        bound_log.error("install_wrapper.failed", error=str(exc))
        raise HTTPException(status_code=500, detail="install failed; rollback performed") from exc

    bound_log.info("install_wrapper.success")
    return InstallWrapperResult(
        cron=cron_record_to_out(updated_cron, local_hostname=local_hostname)
    )


@router.post(
    "/{fingerprint}/uninstall-wrapper",
    responses={
        200: {"description": "Dry-run preview OR uninstall result"},
        400: {"description": "Cron is on a remote host"},
        404: {"description": "Cron not found"},
        409: {"description": "Crontab line not found, or not wrapped"},
        503: {"description": "Host-side cron-apply executor unavailable"},
        500: {"description": "Uninstall failed; rollback performed"},
    },
)
async def uninstall_wrapper(
    fingerprint: str,
    payload: UninstallWrapperRequest,
    request: Request,
    user: Annotated[User, Depends(require_session())],
    repo: Annotated[CronRepo, Depends(get_cron_repo)],
) -> UninstallWrapperPreview | UninstallWrapperResult:
    """Remove (or dry-run preview) the heartbeat wrapper for a local cron.

    confirm=false → UninstallWrapperPreview (no file modifications).
    confirm=true  → performs the uninstall, returns UninstallWrapperResult.
    Session-auth; CSRF enforced automatically by require_session() on POST.

    Uninstall is a pure crontab-line edit: the shared wrapper script and the
    shared token file are NEVER touched (D1/D2).
    """
    from homelab_monitor.kernel.cron.install import (  # noqa: PLC0415
        CronApplyUnavailableError,
        CronLineNotFoundError,
        CrontabWriteError,
        NotWrappedError,
        RemoteHostError,
        build_uninstall_kit,
        uninstall_wrapper_local,
    )

    bound_log = structlog.get_logger().bind(fingerprint=fingerprint)

    local_hostname = resolve_hostname()

    # Fetch cron
    cron = await repo.get_cron(fingerprint, include_hidden=True)
    if cron is None:
        raise NotFoundProblem(message=f"cron not found: {fingerprint}")

    # Check host
    if cron.host != local_hostname:
        raise HTTPException(
            status_code=400,
            detail=f"cron is on remote host {cron.host!r}; remote unwrapping ships in EPIC-017",
        )

    host_root = Path(os.environ.get("HM_CRON_HOST_ROOT", "/host"))

    # Dry-run path (confirm=false)
    if not payload.confirm:
        try:
            kit = await build_uninstall_kit(cron, host_root=host_root)
        except CronLineNotFoundError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except NotWrappedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return UninstallWrapperPreview(
            fingerprint=kit.fingerprint,
            crontab_diff=CrontabDiffOut(
                source_path=kit.crontab_diff.source_path,
                old_line=kit.crontab_diff.old_line,
                new_line=kit.crontab_diff.new_line,
            ),
        )

    # Confirm path (confirm=true)
    try:
        updated_cron = await uninstall_wrapper_local(
            fingerprint,
            cron_repo=repo,
            host_root=host_root,
            local_hostname=local_hostname,
            who=user.username,
            ip=_client_ip(request),
            log=bound_log,
        )
    except RemoteHostError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CronLineNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotWrappedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CronApplyUnavailableError as exc:
        bound_log.error("uninstall_wrapper.executor_unavailable", error=str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CrontabWriteError as exc:
        bound_log.error("uninstall_wrapper.failed", error=str(exc))
        raise HTTPException(status_code=500, detail="uninstall failed; rollback performed") from exc

    bound_log.info("uninstall_wrapper.success")
    return UninstallWrapperResult(
        cron=cron_record_to_out(updated_cron, local_hostname=local_hostname)
    )


@router.post("/discover-now", status_code=202)
async def discover_now(
    request: Request,
    response: _FastApiResponse,
    user: Annotated[User, Depends(require_session())],
) -> dict[str, object]:
    """Trigger an ad-hoc cron discovery scan. Throttled to once per 10s.

    Admin-only via session auth. 429 with Retry-After when called within
    the throttle window. 202 Accepted on success, with a JSON summary of
    the scan result.
    """
    global _discover_now_last_call  # noqa: PLW0603
    async with _discover_now_lock:
        now = _time.monotonic()
        elapsed = now - _discover_now_last_call
        if elapsed < _DISCOVER_NOW_THROTTLE_SECONDS:
            retry_after = max(1, int(_DISCOVER_NOW_THROTTLE_SECONDS - elapsed))
            raise TooManyRequestsProblem(
                code="discover_now_throttled",
                message=f"discovery scan recently triggered; retry in {retry_after}s",
                details={"retry_after_seconds": retry_after},
            )
        _discover_now_last_call = now

        discoverer = getattr(request.app.state, "cron_discoverer", None)
        if discoverer is None:
            raise DependencyUnavailableProblem(
                code="cron_discoverer_unavailable",
                message="cron-discoverer plugin not registered",
            )
        cron_repo = getattr(request.app.state, "cron_repo", None)
        if cron_repo is None:
            raise DependencyUnavailableProblem(
                code="cron_repo_unavailable",
                message="cron-repo not registered",
            )
        # use the lifespan logger instead — fetch from app.state if available

        bound_log = structlog.stdlib.get_logger().bind(component="discover-now", who=user.username)
        result: CronScanResult = await discoverer.scan(cron_repo, log=bound_log)
        try:
            soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
                host=result.host,
                clean_paths=result.clean_source_paths,
                found_by_path=result.found_by_source_path,
                now=utc_now_iso(),
            )
        except Exception as exc:
            soft_deleted, restored = 0, 0
            bound_log.warning("discover_now.reconcile_failed", error=str(exc))
        return {
            "found_count": len(result.found_fingerprints),
            "inserted_count": result.inserted_count,
            "updated_count": result.updated_count,
            "bump_only_count": result.bump_only_count,
            "soft_deleted_count": soft_deleted,
            "restored_count": restored,
            "partial": result.partial,
            "error_count": len(result.errors),
            "errors": [
                {"host_source_path": e.host_source_path, "error": e.error} for e in result.errors
            ],
        }
