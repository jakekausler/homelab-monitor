"""STAGE-004-034: cron_run_failure_enrichments table.

One row per failed cron run, keyed by a UUID failure_id but deduped on the
UNIQUE (cron_fingerprint, run_id) pair so the reconciler's INSERT OR IGNORE is
idempotent across ticks. lines_json is the persisted last-N VictoriaLogs window
(a JSON array of LogLine.model_dump() dicts). Independent 30d retention
(D-CRON-RETAIN-30D) outlives the cron_runs lifecycle row's own prune.

This migration adds NO columns to any existing table, so downgrade simply drops
the new table + its indexes (no table-rebuild needed).

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "cron_run_failure_enrichments" not in inspector.get_table_names():
        op.execute(
            text(
                "CREATE TABLE cron_run_failure_enrichments ("
                "  failure_id TEXT PRIMARY KEY, "
                "  cron_fingerprint TEXT NOT NULL, "
                "  run_id TEXT NOT NULL, "
                "  exit_code INTEGER, "
                "  started_at TEXT, "
                "  ended_at TEXT, "
                "  lines_json TEXT NOT NULL, "
                "  line_count INTEGER NOT NULL, "
                "  truncated INTEGER NOT NULL DEFAULT 0, "
                "  degraded INTEGER NOT NULL DEFAULT 0, "
                "  window_start TEXT, "
                "  window_end TEXT, "
                "  created_at TEXT NOT NULL"
                ")"
            )
        )
        # UNIQUE dedup index: one enrichment per (cron_fingerprint, run_id).
        op.execute(
            text(
                "CREATE UNIQUE INDEX ux_cron_failure_enrich_fp_run "
                "ON cron_run_failure_enrichments(cron_fingerprint, run_id)"
            )
        )
        # Per-fingerprint lookup / prune index.
        op.execute(
            text(
                "CREATE INDEX ix_cron_failure_enrich_fp "
                "ON cron_run_failure_enrichments(cron_fingerprint, created_at)"
            )
        )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_cron_failure_enrich_fp"))
    op.execute(text("DROP INDEX IF EXISTS ux_cron_failure_enrich_fp_run"))
    op.execute(text("DROP TABLE IF EXISTS cron_run_failure_enrichments"))
