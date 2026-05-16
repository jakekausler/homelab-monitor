"""Boot-time mint of the cron-events ingest token (STAGE-002-008).

Mirrors kernel/alertmanager/render.py::ensure_ingest_token. Vector authenticates
to POST /api/internal/cron-events with this token.
"""

from __future__ import annotations

from typing import Final

from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.auth.api_tokens import make_api_token
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

SECRET_NAME: Final[str] = "cron-events-ingest-token"
TOKEN_NAME: Final[str] = "cron-events-ingest"
BOOTSTRAP_WHO: Final[str] = "system:cron-events-bootstrap"


async def ensure_cron_events_token(
    auth_repo: AuthRepository,
    secrets_repo: AsyncSecretsRepository,
    *,
    log: BoundLogger,
) -> str:
    """Return the cron-events ingest plaintext token; mint if absent.

    Idempotent. If a token row + secret are both present, returns the secret.
    Otherwise deletes any half-pair and mints fresh.
    """
    existing_token = await auth_repo.get_api_token_by_name(TOKEN_NAME)
    existing_secret = await secrets_repo.get(SECRET_NAME)
    if existing_token is not None and existing_secret is not None:
        log.info("cron_events.bootstrap.token_reused", token_name=TOKEN_NAME)
        return existing_secret

    if existing_token is not None:
        await auth_repo.delete_api_token_by_name(TOKEN_NAME)
    if existing_secret is not None:
        await secrets_repo.delete(SECRET_NAME)

    plaintext, _sha = make_api_token()
    await auth_repo.create_api_token(
        name=TOKEN_NAME,
        scopes={Scope.CRON_EVENTS_INGEST_WRITE},
        plaintext_token=plaintext,
        who=BOOTSTRAP_WHO,
    )
    await secrets_repo.set(SECRET_NAME, plaintext, who=BOOTSTRAP_WHO)
    log.info("cron_events.bootstrap.token_minted", token_name=TOKEN_NAME)
    return plaintext


__all__ = ["SECRET_NAME", "TOKEN_NAME", "ensure_cron_events_token"]
