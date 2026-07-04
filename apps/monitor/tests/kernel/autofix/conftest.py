"""Fixtures for autofix tests (STAGE-009-011)."""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest_asyncio
from sqlalchemy import text

from homelab_monitor.kernel.autofix.types import FeedbackKind, RunMode
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso


@pytest_asyncio.fixture
async def seed_runbook(repo: SqliteRepository) -> str:
    """Create and return a test runbook ID."""
    runbook_id = uuid7()
    async with repo.transaction() as conn:
        await conn.execute(
            __import__("sqlalchemy").text(
                "INSERT INTO runbooks "
                "(id, path, created_at, alert_match_patterns, risk_tag, "
                " dry_run_required, rate_limit_per_hour, cooldown_seconds, "
                " enabled, auto_trigger, content_hash) "
                "VALUES (:id, :path, :created_at, :alert_match_patterns, :risk_tag, "
                " :dry_run_required, :rate_limit_per_hour, :cooldown_seconds, "
                " :enabled, :auto_trigger, :content_hash)"
            ),
            {
                "id": runbook_id,
                "path": f"test/{runbook_id}.yaml",
                "created_at": utc_now_iso(),
                "alert_match_patterns": "[]",
                "risk_tag": "safe",
                "dry_run_required": False,
                "rate_limit_per_hour": None,
                "cooldown_seconds": None,
                "enabled": True,
                "auto_trigger": False,
                "content_hash": None,
            },
        )
    return runbook_id


@pytest_asyncio.fixture
async def another_runbook(repo: SqliteRepository) -> str:
    """Create and return a second test runbook ID."""
    runbook_id = uuid7()
    async with repo.transaction() as conn:
        await conn.execute(
            __import__("sqlalchemy").text(
                "INSERT INTO runbooks "
                "(id, path, created_at, alert_match_patterns, risk_tag, "
                " dry_run_required, rate_limit_per_hour, cooldown_seconds, "
                " enabled, auto_trigger, content_hash) "
                "VALUES (:id, :path, :created_at, :alert_match_patterns, :risk_tag, "
                " :dry_run_required, :rate_limit_per_hour, :cooldown_seconds, "
                " :enabled, :auto_trigger, :content_hash)"
            ),
            {
                "id": runbook_id,
                "path": f"test/{runbook_id}.yaml",
                "created_at": utc_now_iso(),
                "alert_match_patterns": "[]",
                "risk_tag": "safe",
                "dry_run_required": False,
                "rate_limit_per_hour": None,
                "cooldown_seconds": None,
                "enabled": True,
                "auto_trigger": False,
                "content_hash": None,
            },
        )
    return runbook_id


async def insert_run(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    runbook_id: str,
    mode: RunMode,
    initiated_by: Literal["alert", "operator"],
    alert_id: str | None = None,
    prompt: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    exit_code: int | None = None,
    killed_at: str | None = None,
    transcript_path: str | None = None,
    fixer_user: str = "test-user",
    host: str = "test-host",
    runbook_hash: str | None = None,
) -> str:
    """Insert a run and return its ID."""
    run_id = uuid7()
    if started_at is None:
        started_at = utc_now_iso()

    async with repo.transaction() as conn:
        await conn.execute(
            __import__("sqlalchemy").text(
                "INSERT INTO runbook_runs "
                "(id, runbook_id, created_at, alert_id, mode, prompt, started_at, "
                " ended_at, fixer_user, host, runbook_hash, transcript_path, "
                " exit_code, initiated_by, killed_at) "
                "VALUES (:id, :runbook_id, :created_at, :alert_id, :mode, :prompt, "
                " :started_at, :ended_at, :fixer_user, :host, :runbook_hash, "
                " :transcript_path, :exit_code, :initiated_by, :killed_at)"
            ),
            {
                "id": run_id,
                "runbook_id": runbook_id,
                "created_at": utc_now_iso(),
                "alert_id": alert_id,
                "mode": mode.value,
                "prompt": prompt,
                "started_at": started_at,
                "ended_at": ended_at,
                "fixer_user": fixer_user,
                "host": host,
                "runbook_hash": runbook_hash,
                "transcript_path": transcript_path,
                "exit_code": exit_code,
                "initiated_by": initiated_by,
                "killed_at": killed_at,
            },
        )
    return run_id


async def insert_feedback(
    repo: SqliteRepository,
    *,
    runbook_run_id: str,
    kind: FeedbackKind,
    suggestion_text: str,
    structured_hint: dict[str, Any] | None = None,
) -> str:
    """Insert feedback and return its ID."""
    feedback_id = uuid7()
    hint_json = json.dumps(structured_hint) if structured_hint is not None else None

    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO runbook_run_feedback "
                "(id, runbook_run_id, kind, suggestion_text, structured_hint, created_at) "
                "VALUES (:id, :runbook_run_id, :kind, :suggestion_text, :structured_hint, "
                ":created_at)"
            ),
            {
                "id": feedback_id,
                "runbook_run_id": runbook_run_id,
                "kind": kind.value,
                "suggestion_text": suggestion_text,
                "structured_hint": hint_json,
                "created_at": utc_now_iso(),
            },
        )
    return feedback_id


async def seed_extra_runbook(repo: SqliteRepository) -> str:
    """Insert one throwaway runbook row and return its id.

    Convenience for tests that need N distinct runbook_ids beyond the
    ``seed_runbook`` / ``another_runbook`` fixtures.
    """
    runbook_id = uuid7()
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO runbooks "
                "(id, path, created_at, alert_match_patterns, risk_tag, "
                " dry_run_required, rate_limit_per_hour, cooldown_seconds, "
                " enabled, auto_trigger, content_hash) "
                "VALUES (:id, :path, :created_at, :alert_match_patterns, :risk_tag, "
                " :dry_run_required, :rate_limit_per_hour, :cooldown_seconds, "
                " :enabled, :auto_trigger, :content_hash)"
            ),
            {
                "id": runbook_id,
                "path": f"test/{runbook_id}.yaml",
                "created_at": utc_now_iso(),
                "alert_match_patterns": "[]",
                "risk_tag": "safe",
                "dry_run_required": False,
                "rate_limit_per_hour": None,
                "cooldown_seconds": None,
                "enabled": True,
                "auto_trigger": False,
                "content_hash": None,
            },
        )
    return runbook_id
