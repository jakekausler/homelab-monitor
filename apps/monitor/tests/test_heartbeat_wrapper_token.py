"""Tests for kernel/cron/heartbeat_wrapper_token.py (STAGE-002-009).

Mirrors the ensure_cron_events_token tests in test_api_lifespan.py.
100% branch coverage required for the four branches in ensure_heartbeat_wrapper_token.
"""

from __future__ import annotations

import pytest
import structlog
from sqlalchemy import text

from homelab_monitor.kernel.auth.api_tokens import make_api_token
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.cron.heartbeat_wrapper_token import (
    BOOTSTRAP_WHO,
    SECRET_NAME,
    TOKEN_NAME,
    ensure_heartbeat_wrapper_token,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_when_absent(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Absent token + absent secret → mints fresh pair with HEARTBEAT_WRITE scope."""
    auth_repo = AuthRepository(repo)
    log = structlog.get_logger()

    token = await ensure_heartbeat_wrapper_token(auth_repo, secrets_repo, log=log)

    assert isinstance(token, str)
    assert len(token) > 0

    # Secret stored
    stored = await secrets_repo.get(SECRET_NAME)
    assert stored == token

    # Token row exists with HEARTBEAT_WRITE scope
    row = await repo.fetch_one(
        text("SELECT scopes FROM api_tokens WHERE name = :n"),
        {"n": TOKEN_NAME},
    )
    assert row is not None
    # scopes stored as space-separated string (e.g. "heartbeat:write")
    assert Scope.HEARTBEAT_WRITE in str(row[0])


@pytest.mark.asyncio
async def test_idempotent_returns_same_token(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Second call returns the SAME plaintext, no new rows minted."""
    auth_repo = AuthRepository(repo)
    log = structlog.get_logger()

    first = await ensure_heartbeat_wrapper_token(auth_repo, secrets_repo, log=log)
    second = await ensure_heartbeat_wrapper_token(auth_repo, secrets_repo, log=log)

    assert first == second

    # Still exactly 1 row
    row = await repo.fetch_one(
        text("SELECT COUNT(*) FROM api_tokens WHERE name = :n"),
        {"n": TOKEN_NAME},
    )
    assert row is not None
    assert row[0] == 1


@pytest.mark.asyncio
async def test_half_pair_token_only(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Half-pair: token row present, secret missing → deletes token, mints fresh."""
    auth_repo = AuthRepository(repo)
    log = structlog.get_logger()

    # Seed a stale token row without a secret
    plaintext, _ = make_api_token()
    await auth_repo.create_api_token(
        name=TOKEN_NAME,
        scopes={Scope.HEARTBEAT_WRITE},
        plaintext_token=plaintext,
    )
    assert await secrets_repo.get(SECRET_NAME) is None

    fresh = await ensure_heartbeat_wrapper_token(auth_repo, secrets_repo, log=log)

    assert isinstance(fresh, str)
    assert len(fresh) > 0

    # Secret now present and matches returned value
    stored = await secrets_repo.get(SECRET_NAME)
    assert stored == fresh

    # Exactly 1 token row
    row = await repo.fetch_one(
        text("SELECT COUNT(*) FROM api_tokens WHERE name = :n"),
        {"n": TOKEN_NAME},
    )
    assert row is not None
    assert row[0] == 1


@pytest.mark.asyncio
async def test_half_pair_secret_only(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
) -> None:
    """Half-pair: secret present, token row missing → deletes secret, mints fresh."""
    auth_repo = AuthRepository(repo)
    log = structlog.get_logger()

    # Seed a stale secret without a token row
    await secrets_repo.set(SECRET_NAME, "stale-plaintext", who=BOOTSTRAP_WHO)
    assert await auth_repo.get_api_token_by_name(TOKEN_NAME) is None

    fresh = await ensure_heartbeat_wrapper_token(auth_repo, secrets_repo, log=log)

    assert isinstance(fresh, str)
    assert fresh != "stale-plaintext"

    stored = await secrets_repo.get(SECRET_NAME)
    assert stored == fresh
