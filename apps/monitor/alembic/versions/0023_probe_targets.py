"""STAGE-003-006: probe_targets table.

Holds label-derived probe configurations. One row per
(container_name, kind, name). Soft-deletion via hidden_at.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "probe_targets" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE probe_targets ("
            "  id TEXT NOT NULL PRIMARY KEY, "
            "  container_name TEXT NOT NULL, "
            "  kind TEXT NOT NULL, "
            "  name TEXT NOT NULL, "
            "  target_value TEXT NOT NULL, "
            "  config_source TEXT NOT NULL, "
            "  enabled INTEGER NOT NULL DEFAULT 1, "
            "  interval_seconds INTEGER NOT NULL, "
            "  timeout_seconds INTEGER NOT NULL, "
            "  last_run_at TEXT NULL, "
            "  last_status TEXT NULL, "
            "  last_error TEXT NULL, "
            "  created_at TEXT NOT NULL, "
            "  hidden_at TEXT NULL"
            ")"
        )
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX ux_probe_targets_container_kind_name "
            "ON probe_targets (container_name, kind, name)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX idx_probe_targets_enabled "
            "ON probe_targets (container_name) "
            "WHERE enabled = 1 AND hidden_at IS NULL"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS probe_targets"))
