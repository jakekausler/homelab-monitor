"""STAGE-004-032: container_crash_enrichments table + targets_docker.finished_at.

Poll-based crash enrichment. Each row is one detected crash (a (logical_key,
finished_at) pair), carrying the VictoriaLogs window snapshot as lines_json.
Deduped by a UNIQUE index on (logical_key, finished_at) so the reconciler's
INSERT OR IGNORE is idempotent across ticks. Also adds targets_docker.finished_at
(the Docker State.FinishedAt crash anchor source).

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # (a) targets_docker.finished_at — guarded (column may pre-exist on re-run).
    existing_cols = {c["name"] for c in inspector.get_columns("targets_docker")}
    if "finished_at" not in existing_cols:
        op.execute(text("ALTER TABLE targets_docker ADD COLUMN finished_at TEXT"))

    # (b) container_crash_enrichments — guarded.
    if "container_crash_enrichments" not in inspector.get_table_names():
        op.execute(
            text(
                "CREATE TABLE container_crash_enrichments ("
                "  crash_id TEXT PRIMARY KEY, "
                "  logical_key TEXT NOT NULL, "
                "  container_name TEXT NOT NULL, "
                "  container_id TEXT, "
                "  exit_code INTEGER NOT NULL, "
                "  finished_at TEXT NOT NULL, "
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
        op.execute(
            text(
                "CREATE UNIQUE INDEX ux_crash_enrich_logical_finished "
                "ON container_crash_enrichments(logical_key, finished_at)"
            )
        )
        op.execute(
            text(
                "CREATE INDEX ix_crash_enrich_container "
                "ON container_crash_enrichments(container_name, finished_at)"
            )
        )


def downgrade() -> None:
    # Drop the enrichments table + its indexes.
    op.execute(text("DROP INDEX IF EXISTS ix_crash_enrich_container"))
    op.execute(text("DROP INDEX IF EXISTS ux_crash_enrich_logical_finished"))
    op.execute(text("DROP TABLE IF EXISTS container_crash_enrichments"))

    # Drop targets_docker.finished_at via the SQLite-safe table-rebuild
    # (mirrors the 0022 downgrade pattern). targets_docker MUST be restored to
    # its exact pre-0036 shape: 0022.downgrade() reads the LIVE column list to
    # build its INSERT ... SELECT, so a leftover finished_at column there makes
    # 0022 emit "INSERT INTO ... finished_at ... SELECT finished_at" against a
    # rebuilt table that lacks the column -> OperationalError on downgrade chains.
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = [
        c["name"] for c in inspector.get_columns("targets_docker") if c["name"] != "finished_at"
    ]
    col_list = ", ".join(cols)

    # Pre-0036 targets_docker (post-0022) shape: the 0022 base columns plus the
    # four compose / restart columns 0022 added.
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
            "  FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE"
            ")"
        )
    )
    op.execute(
        text(f"INSERT INTO targets_docker ({col_list}) SELECT {col_list} FROM targets_docker_old")
    )
    op.execute(text("DROP TABLE targets_docker_old"))
    # Recreate the indexes that existed at 0035 (added in 0021).
    op.execute(
        text(
            "CREATE INDEX idx_targets_docker_previous_container_id "
            "ON targets_docker (previous_container_id)"
        )
    )
    op.execute(text("CREATE INDEX idx_targets_docker_container_id ON targets_docker(container_id)"))
