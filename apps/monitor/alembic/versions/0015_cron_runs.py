"""STAGE-002-011: cron_runs table — one row per cron invocation.

Per-invocation run history. A-mode (wrapper) rows are written synchronously by
the heartbeat receiver; B-mode (logscrape) rows and VL-enrichment columns land
in later stages (STAGE-002-013 / 014). This migration creates the table and ALL
its columns + ALL three indexes up front so there is exactly one cron_runs
migration.

Indexes:
- ix_cron_runs_fingerprint_started — (cron_fingerprint, started_at DESC); run-history list.
- ix_cron_runs_enrich_queue — partial on (enriched_at IS NULL AND state != 'running');
  reconciler enrich work-queue.
- ix_cron_runs_fingerprint_state — (cron_fingerprint, state); reconciler open-run scan.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "cron_runs" in set(inspector.get_table_names()):
        return  # idempotent: table already exists

    op.create_table(
        "cron_runs",
        sa.Column("run_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("cron_fingerprint", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("ended_at", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("vl_window_start", sa.Text(), nullable=True),
        sa.Column("vl_window_end", sa.Text(), nullable=True),
        sa.Column("overlapping", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enriched_at", sa.Text(), nullable=True),
        sa.Column("line_count", sa.Integer(), nullable=True),
        sa.Column("byte_count", sa.Integer(), nullable=True),
        sa.Column("content_digest", sa.Text(), nullable=True),
        sa.Column("anomaly_flags", sa.Text(), nullable=False, server_default=""),
    )

    # (cron_fingerprint, started_at DESC) — run-history list query.
    op.create_index(
        "ix_cron_runs_fingerprint_started",
        "cron_runs",
        ["cron_fingerprint", sa.text("started_at DESC")],
    )
    # Partial index — reconciler enrich work-queue.
    op.create_index(
        "ix_cron_runs_enrich_queue",
        "cron_runs",
        ["enriched_at"],
        sqlite_where=text("enriched_at IS NULL AND state != 'running'"),
    )
    # (cron_fingerprint, state) — reconciler open-run scan.
    op.create_index(
        "ix_cron_runs_fingerprint_state",
        "cron_runs",
        ["cron_fingerprint", "state"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "cron_runs" not in set(inspector.get_table_names()):
        return  # idempotent: table already absent
    op.drop_index("ix_cron_runs_fingerprint_state", table_name="cron_runs")
    op.drop_index("ix_cron_runs_enrich_queue", table_name="cron_runs")
    op.drop_index("ix_cron_runs_fingerprint_started", table_name="cron_runs")
    op.drop_table("cron_runs")
