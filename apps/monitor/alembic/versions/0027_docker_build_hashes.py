"""STAGE-003-009: docker_build_hashes table.

Per-container build-context source hashes for locally-built images.
Keyed by container_name. Survives target re-keys (precedent:
image_update_state, STAGE-003-008).

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "docker_build_hashes" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE docker_build_hashes ("
            "  container_name TEXT NOT NULL PRIMARY KEY, "
            "  compose_service TEXT NOT NULL, "
            "  build_context_path TEXT NOT NULL, "
            "  last_source_hash TEXT NULL, "
            "  last_checked_at TEXT NULL, "
            "  check_failed_at TEXT NULL, "
            "  check_error_reason TEXT NULL, "
            "  update_available INTEGER NOT NULL DEFAULT 0, "
            "  baseline_source_hash TEXT NULL, "
            "  baseline_image_id TEXT NULL, "
            "  CHECK (check_error_reason IS NULL OR check_error_reason IN ("
            "'compose_unreadable', 'context_missing', 'context_too_large', "
            "'permission_denied', 'unknown'))"
            ")"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS docker_build_hashes"))
