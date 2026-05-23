"""STAGE-003-007: docker_override_ownership table.

Tracks which container_names are owned by the file-override loader.
DockerDiscoverer reads this table at the start of each tick to skip
the label-upsert path for owned containers (D-OWNERSHIP-TOTAL-PER-CONTAINER,
D-OWNERSHIP-COORDINATION-VIA-SQLITE).

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "docker_override_ownership" in inspector.get_table_names():
        return
    op.execute(
        text(
            "CREATE TABLE docker_override_ownership ("
            "  container_name TEXT NOT NULL PRIMARY KEY, "
            "  claimed_at TEXT NOT NULL"
            ")"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS docker_override_ownership"))
