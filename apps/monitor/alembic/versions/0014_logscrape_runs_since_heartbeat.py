"""STAGE-002-010: add heartbeats_state.logscrape_runs_since_heartbeat column.

Counter of B-mode log-scraped runs observed since the last heartbeat
state-transition. Incremented by record_observed_run; reset to 0 by
record_ok / record_fail / record_start. Drives the WrapperPossiblyStale
vmalert rule (a wrapper that stopped phoning home while cron still fires).

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    hb_cols = {c["name"] for c in inspector.get_columns("heartbeats_state")}
    if "logscrape_runs_since_heartbeat" not in hb_cols:
        with op.batch_alter_table("heartbeats_state") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "logscrape_runs_since_heartbeat",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    hb_cols = {c["name"] for c in inspector.get_columns("heartbeats_state")}
    if "logscrape_runs_since_heartbeat" in hb_cols:
        with op.batch_alter_table("heartbeats_state") as batch_op:
            batch_op.drop_column("logscrape_runs_since_heartbeat")
