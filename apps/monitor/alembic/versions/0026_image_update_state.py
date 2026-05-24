"""STAGE-003-008: image_update_state table.

Per-container image-update check state. Keyed by container_name.
Survives target re-keys (D-STATE-SIDECAR-TABLE).

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "image_update_state" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE image_update_state ("
            "  container_name TEXT NOT NULL PRIMARY KEY, "
            "  last_local_digest TEXT NULL, "
            "  last_registry_digest TEXT NULL, "
            "  last_image_ref TEXT NOT NULL, "
            "  last_checked_at TEXT NULL, "
            "  check_failed_at TEXT NULL, "
            "  check_error_reason TEXT NULL, "
            "  update_available INTEGER NOT NULL DEFAULT 0, "
            "  CHECK (check_error_reason IS NULL OR check_error_reason IN ("
            "'parse_failed', 'network_error', 'auth_failed', 'rate_limited', 'not_found'))"
            ")"
        )
    )
    # TODO: Add index on (update_available) for future grid badge queries
    # when container count grows large. Currently O(n) full-table-scan is
    # acceptable for homelab scale (<100 containers). Track in future epic.


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS image_update_state"))
