"""STAGE-004-013: log_saved_queries table.

Named, persisted Logs Explorer states (LogsQL + selected services + range/preset
+ advanced mode). Single-user; UNIQUE on name. Timestamps are ISO-8601 UTC TEXT
(repo convention, NOT unix ms).

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "log_saved_queries" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE log_saved_queries ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  name TEXT NOT NULL UNIQUE, "
            "  logs_ql TEXT NOT NULL, "
            "  selected_services TEXT NOT NULL, "
            "  since_preset TEXT NULL, "
            "  range_start_iso TEXT NULL, "
            "  range_end_iso TEXT NULL, "
            "  advanced_mode INTEGER NOT NULL DEFAULT 0, "
            "  created_at TEXT NOT NULL, "
            "  updated_at TEXT NOT NULL"
            ")"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS log_saved_queries"))
