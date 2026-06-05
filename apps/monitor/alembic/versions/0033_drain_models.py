"""STAGE-004-025: drain_models table.

Per-model drain3 state snapshots for the log-signature engine. One row per
model_key (a service bucket or a cron fingerprint). `snapshot` is the
base64+zlib-compressed jsonpickle blob produced by drain3's TemplateMiner.
`first_seen_map` is a JSON object {template_hash: first_seen_unix_ms}.
All *_ts / updated_at columns are unix-ms INTEGER (NOT ISO strings).

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "drain_models" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE drain_models ("
            "  model_key TEXT PRIMARY KEY, "
            "  snapshot BLOB NOT NULL, "
            "  line_count INTEGER NOT NULL DEFAULT 0, "
            "  template_count INTEGER NOT NULL DEFAULT 0, "
            "  last_processed_ts INTEGER, "
            "  first_seen_map TEXT NOT NULL DEFAULT '{}', "
            "  updated_at INTEGER NOT NULL"
            ")"
        )
    )
    op.execute(text("CREATE INDEX ix_drain_models_updated_at ON drain_models(updated_at)"))


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_drain_models_updated_at"))
    op.execute(text("DROP TABLE IF EXISTS drain_models"))
