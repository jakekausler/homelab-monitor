"""STAGE-003-005: rebuild suggestions table + add suggestions_docker sidecar.

The pre-existing `suggestions` stub had only (id, kind, created_at) and zero
rows in production (it was a placeholder). We rebuild it to the final shape
(adds deduplication_key, state, updated_at) with NOT NULL + DEFAULT columns
SQLite can't add via ALTER. Sidecar `suggestions_docker` carries Docker-specific
fields keyed by suggestion_id (FK CASCADE).

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())

    # 1. Rebuild suggestions via temp-table swap.
    if "suggestions" in table_names:
        existing_cols = {c["name"] for c in inspector.get_columns("suggestions")}
    else:
        existing_cols = set()

    op.execute(text("DROP TABLE IF EXISTS suggestions_new"))
    op.create_table(
        "suggestions_new",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("deduplication_key", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("kind", "deduplication_key", name="ux_suggestions_kind_dedup"),
    )

    if "suggestions" in table_names and {"id", "kind", "created_at"}.issubset(existing_cols):
        # Copy any pre-existing rows. The legacy stub had no
        # deduplication_key — synthesize from id to satisfy NOT NULL.
        # state defaults to 'pending'; updated_at = created_at.
        op.execute(
            text(
                "INSERT INTO suggestions_new "
                "  (id, kind, deduplication_key, state, created_at, updated_at) "
                "SELECT id, kind, id AS deduplication_key, 'pending' AS state, "
                "       created_at, created_at AS updated_at "
                "FROM suggestions"
            )
        )

    if "suggestions" in table_names:
        op.execute(text("DROP TABLE suggestions"))
    op.execute(text("ALTER TABLE suggestions_new RENAME TO suggestions"))

    # 2. Create suggestions_docker sidecar.
    if "suggestions_docker" not in inspect(bind).get_table_names():
        op.create_table(
            "suggestions_docker",
            sa.Column("suggestion_id", sa.Text(), primary_key=True, nullable=False),
            sa.Column("container_id", sa.Text(), nullable=False),
            sa.Column("container_name", sa.Text(), nullable=False),
            sa.Column("image_ref", sa.Text(), nullable=False),
            sa.Column(
                "labels_json",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column("compose_project", sa.Text(), nullable=True),
            sa.Column("compose_service", sa.Text(), nullable=True),
            sa.Column("detection_reason", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(
                ["suggestion_id"],
                ["suggestions.id"],
                ondelete="CASCADE",
                name="fk_suggestions_docker_suggestion_id",
            ),
        )

    # 3. Indexes.
    existing_indexes = {ix["name"] for ix in inspect(bind).get_indexes("suggestions")}
    if "idx_suggestions_state_kind" not in existing_indexes:
        op.create_index(
            "idx_suggestions_state_kind",
            "suggestions",
            ["state", "kind"],
        )
    existing_docker_indexes = {ix["name"] for ix in inspect(bind).get_indexes("suggestions_docker")}
    if "idx_suggestions_docker_container_id" not in existing_docker_indexes:
        op.create_index(
            "idx_suggestions_docker_container_id",
            "suggestions_docker",
            ["container_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    names = set(inspector.get_table_names())
    if "suggestions_docker" in names:
        op.drop_index(
            "idx_suggestions_docker_container_id",
            table_name="suggestions_docker",
        )
        op.drop_table("suggestions_docker")
    if "suggestions" in names:
        existing_indexes = {ix["name"] for ix in inspector.get_indexes("suggestions")}
        if "idx_suggestions_state_kind" in existing_indexes:
            op.drop_index("idx_suggestions_state_kind", table_name="suggestions")
        # Restore the 3-column stub so older code paths keep working.
        op.execute(text("DROP TABLE IF EXISTS suggestions_old"))
        op.execute(text("ALTER TABLE suggestions RENAME TO suggestions_old"))
        op.create_table(
            "suggestions",
            sa.Column("id", sa.Text(), primary_key=True, nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("created_at", sa.Text(), nullable=False),
        )
        op.execute(
            text(
                "INSERT INTO suggestions (id, kind, created_at) "
                "SELECT id, kind, created_at FROM suggestions_old"
            )
        )
        op.execute(text("DROP TABLE suggestions_old"))
