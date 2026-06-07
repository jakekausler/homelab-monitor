"""STAGE-004-035: log_signatures.first_seen_severity column.

The severity recorded at the signature's FIRST appearance (persisted on INSERT
only, preserved on UPDATE — mirrors first_seen_at). NULL for rows that predate
this migration. NewSignatureCollector reads this for severity scoping.

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("log_signatures")}
    if "first_seen_severity" not in existing_cols:
        op.execute(text("ALTER TABLE log_signatures ADD COLUMN first_seen_severity TEXT"))


def downgrade() -> None:
    # SQLite has no clean DROP COLUMN -> rebuild the table without the column.
    # Mirrors the 0036 targets_docker rebuild. Recreate the exact pre-0039
    # log_signatures shape (from 0034) + its 3 indexes.
    op.execute(text("DROP INDEX IF EXISTS ix_log_signatures_last_seen_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_log_signatures_status"))
    op.execute(text("DROP INDEX IF EXISTS ix_log_signatures_service_key"))
    op.execute(text("DROP TABLE IF EXISTS log_signatures_old"))
    op.execute(text("ALTER TABLE log_signatures RENAME TO log_signatures_old"))
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
    op.execute(
        text(
            "INSERT INTO log_signatures "
            "  (template_hash, service_key, template_str, label, status, "
            "   first_seen_at, last_seen_at, total_count) "
            "SELECT template_hash, service_key, template_str, label, status, "
            "   first_seen_at, last_seen_at, total_count "
            "FROM log_signatures_old"
        )
    )
    op.execute(text("DROP TABLE log_signatures_old"))
    op.execute(text("CREATE INDEX ix_log_signatures_service_key ON log_signatures(service_key)"))
    op.execute(text("CREATE INDEX ix_log_signatures_status ON log_signatures(status)"))
    op.execute(text("CREATE INDEX ix_log_signatures_last_seen_at ON log_signatures(last_seen_at)"))
