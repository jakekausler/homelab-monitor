"""STAGE-004-033: container_healthcheck_enrichments table + targets_docker healthcheck-edge cols.

Poll-based healthcheck-unhealthy enrichment. Each row is one detected episode (a
(logical_key, healthcheck_changed_at) pair), carrying the VictoriaLogs window
snapshot as lines_json. Deduped by a UNIQUE index on (logical_key,
healthcheck_changed_at) so the reconciler's INSERT OR IGNORE is idempotent across
ticks. Also adds targets_docker.healthcheck_changed_at + previous_healthcheck (the
collector-side transition stamp source).

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # (a)+(b) targets_docker healthcheck-edge columns — guarded (may pre-exist).
    existing_cols = {c["name"] for c in inspector.get_columns("targets_docker")}
    if "healthcheck_changed_at" not in existing_cols:
        op.execute(text("ALTER TABLE targets_docker ADD COLUMN healthcheck_changed_at TEXT"))
    if "previous_healthcheck" not in existing_cols:
        op.execute(text("ALTER TABLE targets_docker ADD COLUMN previous_healthcheck TEXT"))

    # (c) container_healthcheck_enrichments — guarded.
    if "container_healthcheck_enrichments" not in inspector.get_table_names():
        op.execute(
            text(
                "CREATE TABLE container_healthcheck_enrichments ("
                "  incident_id TEXT PRIMARY KEY, "
                "  logical_key TEXT NOT NULL, "
                "  container_name TEXT NOT NULL, "
                "  container_id TEXT, "
                "  previous_healthcheck TEXT, "
                "  new_state TEXT NOT NULL, "
                "  healthcheck_changed_at TEXT NOT NULL, "
                "  image_name TEXT, "
                "  compose_project TEXT, "
                "  compose_service TEXT, "
                "  lines_json TEXT NOT NULL, "
                "  line_count INTEGER NOT NULL, "
                "  truncated INTEGER NOT NULL DEFAULT 0, "
                "  degraded INTEGER NOT NULL DEFAULT 0, "
                "  window_start TEXT NOT NULL, "
                "  window_end TEXT NOT NULL, "
                "  created_at TEXT NOT NULL"
                ")"
            )
        )
        # (d) UNIQUE dedup index.
        op.execute(
            text(
                "CREATE UNIQUE INDEX ux_hc_enrich_logical_changed "
                "ON container_healthcheck_enrichments(logical_key, healthcheck_changed_at)"
            )
        )
        # (e) per-container lookup index.
        op.execute(
            text(
                "CREATE INDEX ix_hc_enrich_container "
                "ON container_healthcheck_enrichments(container_name, healthcheck_changed_at)"
            )
        )


def downgrade() -> None:
    # Drop the enrichments table + its indexes.
    op.execute(text("DROP INDEX IF EXISTS ix_hc_enrich_container"))
    op.execute(text("DROP INDEX IF EXISTS ux_hc_enrich_logical_changed"))
    op.execute(text("DROP TABLE IF EXISTS container_healthcheck_enrichments"))

    # Drop the two targets_docker healthcheck columns via the SQLite-safe
    # table-rebuild (mirrors the 0036 downgrade pattern). Read the LIVE column
    # list MINUS the two 0037 columns so a downgrade chain stays clean.
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = [
        c["name"]
        for c in inspector.get_columns("targets_docker")
        if c["name"] not in {"healthcheck_changed_at", "previous_healthcheck"}
    ]
    col_list = ", ".join(cols)

    # Pre-0037 targets_docker (post-0036) shape: the 0036-downgrade 16-column base
    # plus finished_at (added in 0036).
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
            "  compose_project TEXT NULL, "
            "  compose_service TEXT NULL, "
            "  compose_file_path TEXT NULL, "
            "  restart_count_24h_cached INTEGER NULL, "
            "  finished_at TEXT NULL, "
            "  FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE"
            ")"
        )
    )
    op.execute(
        text(f"INSERT INTO targets_docker ({col_list}) SELECT {col_list} FROM targets_docker_old")
    )
    op.execute(text("DROP TABLE targets_docker_old"))
    # Recreate the indexes that existed at 0036 (added in 0021).
    op.execute(
        text(
            "CREATE INDEX idx_targets_docker_previous_container_id "
            "ON targets_docker (previous_container_id)"
        )
    )
    op.execute(text("CREATE INDEX idx_targets_docker_container_id ON targets_docker(container_id)"))
