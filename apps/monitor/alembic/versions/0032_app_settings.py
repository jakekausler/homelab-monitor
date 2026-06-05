"""STAGE-004-022: app_settings key/value table.

Generic single-row-per-key settings store. First consumer: vl_retention_days
(the desired VictoriaLogs retention, applied at next restart). value is TEXT
(callers serialize/deserialize). updated_at is ISO-8601 UTC TEXT (repo
convention, NOT unix ms).

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "app_settings" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE app_settings ("
            "  key TEXT PRIMARY KEY, "
            "  value TEXT NOT NULL, "
            "  updated_at TEXT NOT NULL"
            ")"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS app_settings"))
