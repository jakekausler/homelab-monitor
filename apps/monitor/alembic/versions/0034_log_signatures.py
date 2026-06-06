"""STAGE-004-028: log_signatures catalog table.

One row per (template_hash, service_key). Composite PK: the SAME template hash may
exist under multiple service buckets. `label` + `status` are USER-owned (never
touched by drain sync). `total_count` is a monotonic accumulation of per-cycle line
deltas. All *_at columns are unix-ms INTEGER.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "log_signatures" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE log_signatures ("
            "  template_hash TEXT NOT NULL, "
            "  service_key TEXT NOT NULL, "
            "  template_str TEXT NOT NULL, "
            "  label TEXT, "
            "  status TEXT NOT NULL DEFAULT 'active', "
            "  first_seen_at INTEGER NOT NULL, "
            "  last_seen_at INTEGER NOT NULL, "
            "  total_count INTEGER NOT NULL DEFAULT 0, "
            "  PRIMARY KEY (template_hash, service_key)"
            ")"
        )
    )
    op.execute(text("CREATE INDEX ix_log_signatures_service_key ON log_signatures(service_key)"))
    op.execute(text("CREATE INDEX ix_log_signatures_status ON log_signatures(status)"))
    op.execute(text("CREATE INDEX ix_log_signatures_last_seen_at ON log_signatures(last_seen_at)"))


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_log_signatures_last_seen_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_log_signatures_status"))
    op.execute(text("DROP INDEX IF EXISTS ix_log_signatures_service_key"))
    op.execute(text("DROP TABLE IF EXISTS log_signatures"))
