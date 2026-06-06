"""STAGE-004-029: log_signature_annotations table.

Timestamped plain-text notes per signature, one row per note. `author` is the
denormalized session username at creation time (Decision A2 — no created_by FK).
`created_at` is an ISO-8601 UTC TEXT (repo convention via utc_now_iso). The
composite FK (template_hash, service_key) -> log_signatures cascades on delete.

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "log_signature_annotations" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE log_signature_annotations ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  template_hash TEXT NOT NULL, "
            "  service_key TEXT NOT NULL, "
            "  note TEXT NOT NULL, "
            "  author TEXT NOT NULL, "
            "  created_at TEXT NOT NULL, "
            "  FOREIGN KEY (template_hash, service_key) "
            "    REFERENCES log_signatures(template_hash, service_key) "
            "    ON DELETE CASCADE"
            ")"
        )
    )
    op.execute(
        text(
            "CREATE INDEX ix_log_signature_annotations_sig "
            "ON log_signature_annotations(template_hash, service_key)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX ix_log_signature_annotations_created_at "
            "ON log_signature_annotations(created_at)"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_log_signature_annotations_created_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_log_signature_annotations_sig"))
    op.execute(text("DROP TABLE IF EXISTS log_signature_annotations"))
