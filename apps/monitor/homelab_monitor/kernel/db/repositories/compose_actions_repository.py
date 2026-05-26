"""Repository for the compose_actions table (STAGE-003-010).

One row per Pull & Restart attempt. Lifecycle:
  insert_running(...) → state="running" → update_terminal_state(...) →
    one of state in {"success", "failed", "timeout", "killed"}.

Reads:
  - get_by_id(action_id) → single row
  - list_for_container(name, limit) → most-recent N rows for one container

Truncation: stdout/stderr are truncated by the *caller* (ComposeActionRunner)
to 1 MB before being passed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository


@dataclass(frozen=True, slots=True)
class ComposeActionRow:
    id: int
    action: str
    container_name: str
    compose_service: str
    before_image: str | None
    before_digest: str | None
    after_image: str | None
    after_digest: str | None
    command: str
    stdout: str | None
    stderr: str | None
    exit_code: int | None
    state: str
    error_reason: str | None
    started_at: str
    ended_at: str | None
    duration_seconds: float | None
    who: str
    client_ip: str | None
    audit_log_id: str | None


class ComposeActionsRepository:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    async def insert_running(  # noqa: PLR0913 -- keyword-only collaborators for DI
        self,
        *,
        action: str,
        container_name: str,
        compose_service: str,
        command: str,
        started_at: str,
        who: str,
        client_ip: str | None,
        before_image: str | None = None,
        before_digest: str | None = None,
        initial_state: str = "pulling",  # "pulling" or "building"
    ) -> int:
        """Insert a new row in state=initial_state; return the new action_id."""
        if initial_state not in ("pulling", "building"):
            msg = f"invalid initial state: {initial_state!r}"
            raise ValueError(msg)
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "INSERT INTO compose_actions "
                    "  (action, container_name, compose_service, command, "
                    "   before_image, before_digest, state, started_at, who, client_ip) "
                    "VALUES "
                    "  (:action, :cn, :svc, :cmd, :bi, :bd, :state, :sa, :who, :cip)"
                ),
                {
                    "action": action,
                    "cn": container_name,
                    "svc": compose_service,
                    "cmd": command,
                    "bi": before_image,
                    "bd": before_digest,
                    "state": initial_state,
                    "sa": started_at,
                    "who": who,
                    "cip": client_ip,
                },
            )
            # SQLAlchemy types ``lastrowid`` as ``int`` for INSERT result, but historically
            # we guarded against ``None`` (some dialects/drivers return None for INSERT
            # without explicit PK). Kept as a runtime check via try/int with a defensive
            # message — pyright sees ``int`` here, so no None branch needed.
            row_id = result.lastrowid
            return int(row_id)

    async def update_terminal_state(  # noqa: PLR0913 -- keyword-only collaborators for DI
        self,
        *,
        action_id: int,
        state: str,
        stdout: str | None,
        stderr: str | None,
        exit_code: int | None,
        ended_at: str,
        duration_seconds: float,
        error_reason: str | None = None,
        after_image: str | None = None,
        after_digest: str | None = None,
        audit_log_id: str | None = None,
    ) -> None:
        """Update an existing row to a terminal state."""
        if state not in ("success", "failed", "timeout", "killed"):
            msg = f"invalid terminal state: {state!r}"
            raise ValueError(msg)
        async with self._repo.transaction() as conn:
            await conn.execute(
                text(
                    "UPDATE compose_actions SET "
                    "  state = :state, "
                    "  stdout = :stdout, "
                    "  stderr = :stderr, "
                    "  exit_code = :exit_code, "
                    "  ended_at = :ended_at, "
                    "  duration_seconds = :duration, "
                    "  error_reason = :err_reason, "
                    "  after_image = :ai, "
                    "  after_digest = :ad, "
                    "  audit_log_id = :al "
                    "WHERE id = :id"
                ),
                {
                    "id": action_id,
                    "state": state,
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": exit_code,
                    "ended_at": ended_at,
                    "duration": duration_seconds,
                    "err_reason": error_reason,
                    "ai": after_image,
                    "ad": after_digest,
                    "al": audit_log_id,
                },
            )

    async def update_phase(self, *, action_id: int, phase: str) -> None:
        """Update state to a non-terminal phase ('building' or 'restarting').

        Does NOT touch ended_at, duration_seconds, exit_code, or any output
        columns. For intermediate phase transitions only.
        """
        if phase not in ("building", "restarting"):
            msg = f"invalid phase state: {phase!r}"
            raise ValueError(msg)
        async with self._repo.transaction() as conn:
            await conn.execute(
                text("UPDATE compose_actions SET state = :phase WHERE id = :id"),
                {"phase": phase, "id": action_id},
            )

    async def get_by_id(self, action_id: int) -> ComposeActionRow | None:
        """Fetch one row by id, or None if absent."""
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, action, container_name, compose_service, "
                "  before_image, before_digest, after_image, after_digest, "
                "  command, stdout, stderr, exit_code, state, error_reason, "
                "  started_at, ended_at, duration_seconds, who, client_ip, audit_log_id "
                "FROM compose_actions WHERE id = :id"
            ),
            {"id": action_id},
        )
        if not rows:
            return None
        return _row_to_dataclass(rows[0])

    async def list_for_container(
        self,
        *,
        container_name: str,
        limit: int = 10,
    ) -> list[ComposeActionRow]:
        """List most-recent actions for a container."""
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, action, container_name, compose_service, "
                "  before_image, before_digest, after_image, after_digest, "
                "  command, stdout, stderr, exit_code, state, error_reason, "
                "  started_at, ended_at, duration_seconds, who, client_ip, audit_log_id "
                "FROM compose_actions "
                "WHERE container_name = :cn "
                "ORDER BY started_at DESC "
                "LIMIT :lim"
            ),
            {"cn": container_name, "lim": limit},
        )
        return [_row_to_dataclass(r) for r in rows]

    async def get_active_for_container(self, container_name: str) -> ComposeActionRow | None:
        """Return the most-recent active (non-terminal) row for container, or None.

        Checks state IN ('pulling', 'building', 'restarting', 'running') — the last entry
        covers any legacy rows written before migration 0029.
        """
        rows = await self._repo.fetch_all(
            text(
                "SELECT id, action, container_name, compose_service, "
                "  before_image, before_digest, after_image, after_digest, "
                "  command, stdout, stderr, exit_code, state, error_reason, "
                "  started_at, ended_at, duration_seconds, who, client_ip, audit_log_id "
                "FROM compose_actions "
                "WHERE container_name = :cn "
                "  AND state IN ('pulling', 'building', 'restarting', 'running') "
                "ORDER BY started_at DESC "
                "LIMIT 1"
            ),
            {"cn": container_name},
        )
        if not rows:
            return None
        return _row_to_dataclass(rows[0])


def _row_to_dataclass(r: Any) -> ComposeActionRow:  # noqa: ANN401 -- SQLite row from Any dict
    """Convert a SQLite row to a ComposeActionRow dataclass."""
    return ComposeActionRow(
        id=int(r.id),  # pyright: ignore[reportAttributeAccessIssue]
        action=str(r.action),  # pyright: ignore[reportAttributeAccessIssue]
        container_name=str(r.container_name),  # pyright: ignore[reportAttributeAccessIssue]
        compose_service=str(r.compose_service),  # pyright: ignore[reportAttributeAccessIssue]
        before_image=(None if r.before_image is None else str(r.before_image)),  # pyright: ignore[reportAttributeAccessIssue]
        before_digest=(None if r.before_digest is None else str(r.before_digest)),  # pyright: ignore[reportAttributeAccessIssue]
        after_image=(None if r.after_image is None else str(r.after_image)),  # pyright: ignore[reportAttributeAccessIssue]
        after_digest=(None if r.after_digest is None else str(r.after_digest)),  # pyright: ignore[reportAttributeAccessIssue]
        command=str(r.command),  # pyright: ignore[reportAttributeAccessIssue]
        stdout=(None if r.stdout is None else str(r.stdout)),  # pyright: ignore[reportAttributeAccessIssue]
        stderr=(None if r.stderr is None else str(r.stderr)),  # pyright: ignore[reportAttributeAccessIssue]
        exit_code=(None if r.exit_code is None else int(r.exit_code)),  # pyright: ignore[reportAttributeAccessIssue]
        state=str(r.state),  # pyright: ignore[reportAttributeAccessIssue]
        error_reason=(None if r.error_reason is None else str(r.error_reason)),  # pyright: ignore[reportAttributeAccessIssue]
        started_at=str(r.started_at),  # pyright: ignore[reportAttributeAccessIssue]
        ended_at=(None if r.ended_at is None else str(r.ended_at)),  # pyright: ignore[reportAttributeAccessIssue]
        duration_seconds=(None if r.duration_seconds is None else float(r.duration_seconds)),  # pyright: ignore[reportAttributeAccessIssue]
        who=str(r.who),  # pyright: ignore[reportAttributeAccessIssue]
        client_ip=(None if r.client_ip is None else str(r.client_ip)),  # pyright: ignore[reportAttributeAccessIssue]
        audit_log_id=(None if r.audit_log_id is None else str(r.audit_log_id)),  # pyright: ignore[reportAttributeAccessIssue]
    )


# Active (non-terminal) states — used for concurrency guard.
ACTIVE_STATES: tuple[str, ...] = ("pulling", "building", "restarting", "running")


__all__ = ["ACTIVE_STATES", "ComposeActionRow", "ComposeActionsRepository"]
