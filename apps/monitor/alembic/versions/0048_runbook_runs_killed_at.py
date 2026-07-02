"""STAGE-009-007: add runbook_runs.killed_at for the kill-switch mid-run kill.

killed_at is set by AutoFixOrchestrator.kill_inflight when the kill-switch is
flipped off during an in-flight real exec. It complements ended_at (which is
stamped by _exec_claude's natural completion path); a row may have both when
the SIGKILL races the natural exit, or only killed_at when the exec unwinds
via SIGKILL. Downstream reporting distinguishes the two.

Additive TEXT NULL column — SQLite `ALTER TABLE ... ADD COLUMN` is supported
without batch_alter_table. Downgrade uses batch_alter_table (SQLite lacks
DROP COLUMN in older versions; alembic's batch mode emulates it).

Revision ID: 0048
Revises: 0047
"""

from __future__ import annotations

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE runbook_runs ADD COLUMN killed_at TEXT NULL")


def downgrade() -> None:
    with op.batch_alter_table("runbook_runs") as batch_op:
        batch_op.drop_column("killed_at")
