"""STAGE-003-010: add 'building' to compose_actions state CHECK constraint.

SQLite does not support ALTER TABLE ADD CONSTRAINT. Use table-rename pattern.
No data migration needed — existing rows are already terminal states.

Downgrade Lossiness:
Downgrade maps state='building' to state='pulling'. Re-upgrade does NOT
restore the original 'building' value. This is intentional — the only
consumers (UI, vmalert) treat both as non-terminal "in-progress" states
and the downgrade-then-upgrade path is operator-initiated, not automatic.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = inspector.get_table_names()

    if "compose_actions_old" in table_names:
        op.execute(text("DROP TABLE compose_actions_old"))

    if "compose_actions" not in table_names:
        _create_new_table()
        return

    op.execute(text("ALTER TABLE compose_actions RENAME TO compose_actions_old"))
    _create_new_table()

    op.execute(
        text(
            "INSERT INTO compose_actions "
            "  (id, action, container_name, compose_service, "
            "   before_image, before_digest, after_image, after_digest, "
            "   command, stdout, stderr, exit_code, state, error_reason, "
            "   started_at, ended_at, duration_seconds, who, client_ip, audit_log_id) "
            "SELECT "
            "  id, action, container_name, compose_service, "
            "  before_image, before_digest, after_image, after_digest, "
            "  command, stdout, stderr, exit_code, state, "
            "  error_reason, started_at, ended_at, duration_seconds, "
            "  who, client_ip, audit_log_id "
            "FROM compose_actions_old"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_compose_actions_container_started "
            "ON compose_actions(container_name, started_at DESC)"
        )
    )
    op.execute(text("DROP TABLE compose_actions_old"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = inspector.get_table_names()
    if "compose_actions" not in table_names:
        return
    if "compose_actions_new" in table_names:
        op.execute(text("DROP TABLE compose_actions_new"))
    op.execute(text("ALTER TABLE compose_actions RENAME TO compose_actions_new"))
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
            "  audit_log_id TEXT NULL, "
            "  CHECK (action IN ('pull_and_restart')), "
            "  CHECK (state IN ('pulling','restarting','success','failed','timeout','killed'))"
            ")"
        )
    )
    op.execute(
        text(
            "INSERT INTO compose_actions "
            "  SELECT id, action, container_name, compose_service, "
            "  before_image, before_digest, after_image, after_digest, "
            "  command, stdout, stderr, exit_code, "
            "  CASE WHEN state = 'building' THEN 'pulling' ELSE state END, "
            "  error_reason, started_at, ended_at, duration_seconds, "
            "  who, client_ip, audit_log_id "
            "  FROM compose_actions_new"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_compose_actions_container_started "
            "ON compose_actions(container_name, started_at DESC)"
        )
    )
    op.execute(text("DROP TABLE compose_actions_new"))


def _create_new_table() -> None:
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
            "  audit_log_id TEXT NULL, "
            "  CHECK (action IN ('pull_and_restart')), "
            "  CHECK (state IN ('building','pulling','restarting','success','failed','timeout','killed'))"
            ")"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_compose_actions_container_started "
            "ON compose_actions(container_name, started_at DESC)"
        )
    )
