"""POST /hb/{path} -- heartbeat ingest stub.

Auth-only stub for STAGE-001-021. The full heartbeat receiver behavior
(persisting the latest beat per registered key, aging-out detection, dispatch
to the alert path) lands in EPIC-002. This stage establishes the auth boundary
so the integration rig can assert 401 without token / 204 with token.

Auth: API token with Scope.HEARTBEAT_WRITE. No session path -- heartbeats are
machine-to-machine traffic.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from homelab_monitor.kernel.api.dependencies import require_token_scope
from homelab_monitor.kernel.auth.models import ApiToken
from homelab_monitor.kernel.auth.scopes import Scope

router = APIRouter(prefix="/hb", tags=["heartbeat"])


@router.post("/{_path:path}", status_code=204)
async def receive_heartbeat(
    _path: str,
    request: Request,
    _token: Annotated[ApiToken, Depends(require_token_scope(Scope.HEARTBEAT_WRITE))],
) -> Response:
    """Accept a heartbeat POST. Body is consumed but discarded (EPIC-002 will persist).

    Returns 204 No Content. Any path segment after /hb/ is accepted; in EPIC-002
    the path will be looked up in a registered-keys table.
    """
    # Drain the request body so clients with chunked transfer don't block on
    # the next request. The body is intentionally not used yet. EPIC-002 will
    # persist both the path and body.
    _ = await request.body()
    return Response(status_code=204)
