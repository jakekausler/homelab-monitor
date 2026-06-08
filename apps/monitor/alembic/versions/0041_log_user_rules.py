"""STAGE-004-042: log_user_rules table.

User-authored vmalert rules. One row = one alerting rule the user defined. The
render layer (kernel/logs/user_rules_render.py) reads enabled rows and writes two
aggregate YAML files (logs.yaml / metrics.yaml) onto a shared volume that vmalert
globs. created_at/updated_at are ISO-8601 UTC TEXT (repo convention via
utc_now_iso). rule_name is UNIQUE (the alert: name in the rendered YAML).
source_kind/source_ref are source-tracking fields for STAGE-043/044 (manual in v1).

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "log_user_rules" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE log_user_rules ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  rule_name TEXT NOT NULL UNIQUE, "
            "  expr TEXT NOT NULL, "
            "  expr_kind TEXT NOT NULL, "
            "  severity TEXT NOT NULL, "
            "  summary TEXT NOT NULL, "
            "  description TEXT NOT NULL DEFAULT '', "
            "  for_duration TEXT NOT NULL DEFAULT '0s', "
            "  source_kind TEXT NOT NULL DEFAULT 'manual', "
            "  source_ref TEXT, "
            "  enabled INTEGER NOT NULL DEFAULT 1, "
            "  created_at TEXT NOT NULL, "
            "  updated_at TEXT NOT NULL"
            ")"
        )
    )
    op.execute(text("CREATE UNIQUE INDEX ix_log_user_rules_rule_name ON log_user_rules(rule_name)"))


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_log_user_rules_rule_name"))
    op.execute(text("DROP TABLE IF EXISTS log_user_rules"))
