"""STAGE-003-010 refinement: widen compose_actions state to pulling/restarting.

SQLite does not support ALTER TABLE DROP CONSTRAINT. The canonical migration
pattern is: rename old table, create new with updated CHECK, copy data,
drop old.

Any legacy state='running' rows are migrated to state='pulling' so they
remain valid under the new constraint.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = inspector.get_table_names()

    # If already migrated (idempotency guard): check if 'pulling' is in the
    # CHECK constraint. SQLite doesn't expose CHECK constraints via inspector,
    # so we use a column probe: attempt to insert a sentinel then roll back.
    # Simpler: just try the rename; if compose_actions_old already exists,
    # a previous run failed mid-way — drop it and redo.
    if "compose_actions_old" in table_names:
        op.execute(text("DROP TABLE compose_actions_old"))

    if "compose_actions" not in table_names:
        # Fresh DB — create with new constraint directly.
        _create_new_table()
        return

    # Step 1: Rename existing table.
    op.execute(text("ALTER TABLE compose_actions RENAME TO compose_actions_old"))

    # Step 2: Create new table with widened CHECK.
    _create_new_table()

    # Step 3: Copy rows; migrate state='running' → 'pulling'.
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
            "  command, stdout, stderr, exit_code, "
            "  CASE WHEN state = 'running' THEN 'pulling' ELSE state END, "
            "  error_reason, started_at, ended_at, duration_seconds, "
            "  who, client_ip, audit_log_id "
            "FROM compose_actions_old"
        )
    )

    # Step 4: Rebuild index (dropped with old table).
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_compose_actions_container_started "
            "ON compose_actions(container_name, started_at DESC)"
        )
    )

    # Step 5: Drop old table.
    op.execute(text("DROP TABLE compose_actions_old"))


def downgrade() -> None:
    # Reverse: widen back to original (running,success,failed,timeout,killed).
    # Rows in state pulling/restarting become running.
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
            "  CHECK (state IN ('running', 'success', 'failed', 'timeout', 'killed'))"
            ")"
        )
    )
    op.execute(
        text(
            "INSERT INTO compose_actions "
            "  SELECT id, action, container_name, compose_service, "
            "  before_image, before_digest, after_image, after_digest, "
            "  command, stdout, stderr, exit_code, "
            "  CASE WHEN state IN ('pulling','restarting') THEN 'running' ELSE state END, "
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
            "  CHECK (state IN ('pulling','restarting','success','failed','timeout','killed'))"
            ")"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_compose_actions_container_started "
            "ON compose_actions(container_name, started_at DESC)"
        )
    )
