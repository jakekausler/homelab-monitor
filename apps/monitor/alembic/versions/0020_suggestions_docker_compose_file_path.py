"""STAGE-003-005 Refinement: add compose_file_path to suggestions_docker.

Captures com.docker.compose.project.config_files label value so the UI
can display the compose file path alongside the project/service name.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE suggestions_docker ADD COLUMN compose_file_path TEXT NULL")


def downgrade() -> None:
    # SQLite does not support DROP COLUMN in older versions; use temp-table swap.
    from sqlalchemy import inspect, text  # noqa: PLC0415

    bind = op.get_bind()
    inspector = inspect(bind)
    cols = [
        c["name"]
        for c in inspector.get_columns("suggestions_docker")
        if c["name"] != "compose_file_path"
    ]

    col_list = ", ".join(cols)
    op.execute(text("DROP TABLE IF EXISTS suggestions_docker_old"))
    op.execute(text("ALTER TABLE suggestions_docker RENAME TO suggestions_docker_old"))
    op.execute(
        text(
            "CREATE TABLE suggestions_docker ("
            "  suggestion_id TEXT NOT NULL PRIMARY KEY, "
            "  container_id TEXT NOT NULL, "
            "  container_name TEXT NOT NULL, "
            "  image_ref TEXT NOT NULL, "
            "  labels_json TEXT NOT NULL DEFAULT '{}', "
            "  compose_project TEXT NULL, "
            "  compose_service TEXT NULL, "
            "  detection_reason TEXT NOT NULL, "
            "  FOREIGN KEY (suggestion_id) REFERENCES suggestions(id) ON DELETE CASCADE"
            ")"
        )
    )
    op.execute(
        text(
            f"INSERT INTO suggestions_docker ({col_list}) "
            f"SELECT {col_list} FROM suggestions_docker_old"
        )
    )
    op.execute(text("DROP TABLE suggestions_docker_old"))
    # The table-swap above dropped the index that migration 0019 created on this
    # table; recreate it so downgrade to 0019 leaves the same schema 0019 produced.
    op.execute(
        text(
            "CREATE INDEX idx_suggestions_docker_container_id ON suggestions_docker (container_id)"
        )
    )
