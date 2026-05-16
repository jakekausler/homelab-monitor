"""STAGE-002-008: B-mode log-scrape schema.

Adds:
- crons.log_match_key TEXT + index ix_crons_host_log_match_key on
  (host, log_match_key). Backfilled for existing rows from canonical_log_key(command).
- heartbeats_state.observed_runs_total INTEGER NOT NULL DEFAULT 0 — lifetime count
  of neutral "observed run" events from B-mode log evidence.
- heartbeats_state.last_observed_run_at TEXT NULL — timestamp of the most recent
  observed run.
- cron_log_cursors table (journal_cursor PRIMARY KEY, processed_at) — idempotency
  ledger for the /api/internal/cron-events ingest endpoint.

NO last_log_watermark / recent_log_hashes column — Option D (Vector push +
cursor dedup) removed the need for a content-hash watermark.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op
from homelab_monitor.kernel.cron.log_match import canonical_log_key

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    crons_cols = {c["name"] for c in inspector.get_columns("crons")}
    if "log_match_key" not in crons_cols:
        with op.batch_alter_table("crons") as batch_op:
            batch_op.add_column(sa.Column("log_match_key", sa.Text(), nullable=True))

    crons_indexes = {ix["name"] for ix in inspector.get_indexes("crons")}
    if "ix_crons_host_log_match_key" not in crons_indexes:
        op.create_index("ix_crons_host_log_match_key", "crons", ["host", "log_match_key"])

    hb_cols = {c["name"] for c in inspector.get_columns("heartbeats_state")}
    if "observed_runs_total" not in hb_cols:
        with op.batch_alter_table("heartbeats_state") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "observed_runs_total",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
    if "last_observed_run_at" not in hb_cols:
        with op.batch_alter_table("heartbeats_state") as batch_op:
            batch_op.add_column(sa.Column("last_observed_run_at", sa.Text(), nullable=True))

    existing_tables = set(inspector.get_table_names())
    if "cron_log_cursors" not in existing_tables:
        op.create_table(
            "cron_log_cursors",
            sa.Column("journal_cursor", sa.Text(), primary_key=True, nullable=False),
            sa.Column("processed_at", sa.Text(), nullable=False),
        )

    # Backfill log_match_key for existing rows. The stored command is already
    # scrubbed; canonical_log_key re-scrubs (idempotent no-op) then canonicalizes.
    # Row-by-row backfill: the crons table is homelab-scale (tens of rows), so
    # an unbatched UPDATE-per-row loop is acceptable. Revisit if the table grows.
    rows = bind.execute(
        text("SELECT fingerprint, command FROM crons WHERE log_match_key IS NULL")
    ).fetchall()
    for row in rows:
        key = canonical_log_key(str(row.command))
        bind.execute(
            text("UPDATE crons SET log_match_key = :k WHERE fingerprint = :fp"),
            {"k": key, "fp": str(row.fingerprint)},
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "cron_log_cursors" in set(inspector.get_table_names()):
        op.drop_table("cron_log_cursors")

    hb_cols = {c["name"] for c in inspector.get_columns("heartbeats_state")}
    if "last_observed_run_at" in hb_cols:
        with op.batch_alter_table("heartbeats_state") as batch_op:
            batch_op.drop_column("last_observed_run_at")
    if "observed_runs_total" in hb_cols:
        with op.batch_alter_table("heartbeats_state") as batch_op:
            batch_op.drop_column("observed_runs_total")

    crons_indexes = {ix["name"] for ix in inspector.get_indexes("crons")}
    if "ix_crons_host_log_match_key" in crons_indexes:
        op.drop_index("ix_crons_host_log_match_key", table_name="crons")
    crons_cols = {c["name"] for c in inspector.get_columns("crons")}
    if "log_match_key" in crons_cols:
        with op.batch_alter_table("crons") as batch_op:
            batch_op.drop_column("log_match_key")
