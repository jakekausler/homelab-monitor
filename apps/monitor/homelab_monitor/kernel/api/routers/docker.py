"""GET /api/integrations/docker/containers — session-auth.

Single endpoint returning ContainerRow[] (matches UI contract in
apps/ui/src/routes/integrations/types.ts). Cadvisor fields (cpu_pct, mem_mib)
come from the SQLite cache populated by DockerSocketCollector's VM merge
step (T-MERGE-LOCATION) — sub-10ms read, no live VM query.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.api.dependencies import get_repo, require_session
from homelab_monitor.kernel.auth.models import User
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
            )
            for row in rows
        ]
    )
