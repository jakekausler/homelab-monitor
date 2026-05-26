"""STAGE-003-010: compose_actions table.

Audit log for `docker compose pull && up -d` actions. One row per attempt
(running → terminal). Composite index on (container_name, started_at DESC)
supports "list recent actions for container" reads.

CHECK constraints enforce the action + state enums; widen later when new
action types (pull-only, restart-only, bulk pull) are added.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "compose_actions" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE compose_actions ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  action TEXT NOT NULL, "
            "  container_name TEXT NOT NULL, "
            "  compose_service TEXT NOT NULL, "
            "  before_image TEXT NULL, "
            "  before_digest TEXT NULL, "
            "  after_image TEXT NULL, "
            "  after_digest TEXT NULL, "
            "  command TEXT NOT NULL, "
            "  stdout TEXT NULL, "
            "  stderr TEXT NULL, "
            "  exit_code INTEGER NULL, "
            "  state TEXT NOT NULL, "
            "  error_reason TEXT NULL, "
            "  started_at TEXT NOT NULL, "
            "  ended_at TEXT NULL, "
            "  duration_seconds REAL NULL, "
            "  who TEXT NOT NULL, "
            "  client_ip TEXT NULL, "
            # audit_log_id has no FK constraint because audit_log uses string ids
            # (uuid7) and SQLite's FK enforcement is opt-in; weak link is intentional.
            # Future: if audit_log adopts a stronger schema, add FK here.
            "  audit_log_id TEXT NULL, "
            "  CHECK (action IN ('pull_and_restart')), "
            "  CHECK (state IN ('running', 'success', 'failed', 'timeout', 'killed'))"
            ")"
        )
    )
    op.execute(
        text(
            "CREATE INDEX ix_compose_actions_container_started "
            "ON compose_actions(container_name, started_at DESC)"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_compose_actions_container_started"))
    op.execute(text("DROP TABLE IF EXISTS compose_actions"))
