"""STAGE-003-005 Refinement Q2: add compose columns to targets_docker.

Captures compose_project, compose_service, compose_file_path on the sidecar
table so the API can surface these without parsing the labels JSON blob.

Also adds restart_count_24h_cached (Q1) to cache the 24h restart delta from
VictoriaMetrics at collector tick time.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    docker_cols = {c["name"] for c in inspector.get_columns("targets_docker")}
    if "compose_project" not in docker_cols:
        op.execute(text("ALTER TABLE targets_docker ADD COLUMN compose_project TEXT NULL"))
    if "compose_service" not in docker_cols:
        op.execute(text("ALTER TABLE targets_docker ADD COLUMN compose_service TEXT NULL"))
    if "compose_file_path" not in docker_cols:
        op.execute(text("ALTER TABLE targets_docker ADD COLUMN compose_file_path TEXT NULL"))
    if "restart_count_24h_cached" not in docker_cols:
        op.execute(
            text("ALTER TABLE targets_docker ADD COLUMN restart_count_24h_cached INTEGER NULL")
        )


def downgrade() -> None:
    # SQLite cannot DROP COLUMN cleanly. Use temp-table swap (mirrors 0020 pattern).
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = [
        c["name"]
        for c in inspector.get_columns("targets_docker")
        if c["name"]
        not in {
            "compose_project",
            "compose_service",
            "compose_file_path",
            "restart_count_24h_cached",
        }
    ]
    col_list = ", ".join(cols)

    # Read the existing CREATE TABLE DDL so we can rebuild faithfully.
    # targets_docker after 0021 has:
    #   target_id TEXT PK, container_id TEXT, restart_count INT, exit_code INT,
    #   healthcheck TEXT, image TEXT, network_mode TEXT, cpu_pct_cached REAL,
    #   mem_mib_cached REAL, metrics_cached_at TEXT, previous_container_id TEXT,
    #   recreated_at TEXT
    op.execute(text("DROP TABLE IF EXISTS targets_docker_old"))
    op.execute(text("ALTER TABLE targets_docker RENAME TO targets_docker_old"))
    op.execute(
        text(
            "CREATE TABLE targets_docker ("
            "  target_id TEXT NOT NULL PRIMARY KEY, "
            "  container_id TEXT NULL, "
            "  restart_count INTEGER NULL, "
            "  exit_code INTEGER NULL, "
            "  healthcheck TEXT NULL, "
            "  image TEXT NULL, "
            "  network_mode TEXT NULL, "
            "  cpu_pct_cached REAL NULL, "
            "  mem_mib_cached REAL NULL, "
            "  metrics_cached_at TEXT NULL, "
            "  previous_container_id TEXT NULL, "
            "  recreated_at TEXT NULL, "
            "  FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE"
            ")"
        )
    )
    op.execute(
        text(f"INSERT INTO targets_docker ({col_list}) SELECT {col_list} FROM targets_docker_old")
    )
    op.execute(text("DROP TABLE targets_docker_old"))
    # Recreate indexes that existed after 0021
    op.execute(
        text(
            "CREATE INDEX idx_targets_docker_previous_container_id "
            "ON targets_docker (previous_container_id)"
        )
    )
    op.execute(text("CREATE INDEX idx_targets_docker_container_id ON targets_docker(container_id)"))
