"""STAGE-004-038: log_signature_silence_allowlist table.

Expected-silence allowlist. One row = one rule saying "this signature (or all
signatures of this service when template_hash IS NULL) is EXPECTED to be silent
according to schedule_kind/schedule_value". SilenceDetectionCollector consults
this table before emitting homelab_log_signature_silent. created_at/expires_at
are ISO-8601 UTC TEXT (repo convention via utc_now_iso). expires_at NULL = never
expires.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "log_signature_silence_allowlist" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE log_signature_silence_allowlist ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  template_hash TEXT, "
            "  service_key TEXT NOT NULL, "
            "  schedule_kind TEXT NOT NULL, "
            "  schedule_value TEXT NOT NULL DEFAULT '', "
            "  reason TEXT NOT NULL, "
            "  created_at TEXT NOT NULL, "
            "  expires_at TEXT"
            ")"
        )
    )
    op.execute(
        text(
            "CREATE INDEX ix_log_signature_silence_allowlist_service_key "
            "ON log_signature_silence_allowlist(service_key)"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_log_signature_silence_allowlist_service_key"))
    op.execute(text("DROP TABLE IF EXISTS log_signature_silence_allowlist"))
