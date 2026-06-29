"""STAGE-009-001: Fill runbooks + runbook_runs auto-fix columns.

Expands the SCAFFOLDING-stage tables created in 0001_initial_schema.py to their
auto-fix shape (spec §7.4 / EPIC-009):

- runbooks: alert_match_patterns (canonical JSON), risk_tag, dry_run_required,
  rate_limit_per_hour, cooldown_seconds, enabled, auto_trigger, content_hash.
  Conservative DB defaults: risk_tag='risky', dry_run_required=1, enabled=0,
  auto_trigger=0 (a freshly-discovered runbook is disabled + risky until the
  operator opts in).
- runbook_runs: alert_id (FK->alerts.id), mode, prompt, transcript_path,
  exit_code, started_at, ended_at, fixer_user, host, runbook_hash.

Tables are empty stubs; no backfill needed. New columns are nullable except the
four boolean/text gates carrying server_defaults (so any pre-existing stub row
satisfies NOT NULL).

batch_alter_table is used for SQLite FK compatibility (cf. 0005). Downgrade
drops the added columns in reverse; under batch mode SQLite recreates each
table without them, which also drops the runbook_runs->alerts FK.

Revision ID: 0045
Revises: 0044
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ----- runbooks -----
    with op.batch_alter_table("runbooks") as batch_op:
        batch_op.add_column(sa.Column("alert_match_patterns", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "risk_tag",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'risky'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "dry_run_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch_op.add_column(sa.Column("rate_limit_per_hour", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("cooldown_seconds", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "auto_trigger",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(sa.Column("content_hash", sa.Text(), nullable=True))

    # ----- runbook_runs -----
    with op.batch_alter_table("runbook_runs") as batch_op:
        # Add the FK column inside the batch first, then create the FK (cf. 0005).
        batch_op.add_column(sa.Column("alert_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("mode", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("prompt", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("transcript_path", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("exit_code", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("started_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("ended_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("fixer_user", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("host", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("runbook_hash", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_runbook_runs_alert_id_alerts",
            "alerts",
            ["alert_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("runbook_runs") as batch_op:
        batch_op.drop_column("runbook_hash")
        batch_op.drop_column("host")
        batch_op.drop_column("fixer_user")
        batch_op.drop_column("ended_at")
        batch_op.drop_column("started_at")
        batch_op.drop_column("exit_code")
        batch_op.drop_column("transcript_path")
        batch_op.drop_column("prompt")
        batch_op.drop_column("mode")
        batch_op.drop_column("alert_id")

    with op.batch_alter_table("runbooks") as batch_op:
        batch_op.drop_column("content_hash")
        batch_op.drop_column("auto_trigger")
        batch_op.drop_column("enabled")
        batch_op.drop_column("cooldown_seconds")
        batch_op.drop_column("rate_limit_per_hour")
        batch_op.drop_column("dry_run_required")
        batch_op.drop_column("risk_tag")
        batch_op.drop_column("alert_match_patterns")
