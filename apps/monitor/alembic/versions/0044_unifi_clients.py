"""STAGE-007-003: unifi_clients registry + unifi_client_observations span table.

unifi_clients is the MAC-keyed canonical client inventory (current identity +
connection facts). unifi_client_observations records time-stamped IP<->MAC
SPAN rows (one row per (mac, ip); repeated sightings collapse, extending
last_seen) for EPIC-006's time-windowed IP-at-time join. All timestamps are
ISO-8601 UTC TEXT (utc_now_iso). Booleans are INTEGER 0/1 (project norm).

Revision ID: 0044
Revises: 0043
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = set(inspector.get_table_names())

    if "unifi_clients" not in existing:
        op.execute(
            text(
                "CREATE TABLE unifi_clients ("
                "  mac TEXT NOT NULL PRIMARY KEY, "
                "  ip TEXT, "
                "  hostname TEXT, "
                "  name TEXT, "
                "  oui TEXT, "
                "  network TEXT, "
                "  ap_mac TEXT, "
                "  sw_mac TEXT, "
                "  sw_port INTEGER, "
                "  use_fixedip INTEGER NOT NULL DEFAULT 0, "
                "  fixed_ip TEXT, "
                "  online INTEGER NOT NULL DEFAULT 0, "
                "  is_host INTEGER NOT NULL DEFAULT 0, "
                "  first_seen TEXT NOT NULL, "
                "  last_seen TEXT NOT NULL, "
                "  lease_expiry TEXT"
                ")"
            )
        )
        op.execute(text("CREATE INDEX ix_unifi_clients_last_seen ON unifi_clients(last_seen)"))
        op.execute(text("CREATE INDEX ix_unifi_clients_ip ON unifi_clients(ip)"))

    if "unifi_client_observations" not in existing:
        op.execute(
            text(
                "CREATE TABLE unifi_client_observations ("
                "  mac TEXT NOT NULL, "
                "  ip TEXT NOT NULL, "
                "  first_seen TEXT NOT NULL, "
                "  last_seen TEXT NOT NULL, "
                "  PRIMARY KEY (mac, ip)"
                ")"
            )
        )
        op.execute(
            text(
                "CREATE INDEX ix_unifi_client_observations_ip_first_seen "
                "ON unifi_client_observations(ip, first_seen)"
            )
        )
        op.execute(
            text(
                "CREATE INDEX ix_unifi_client_observations_mac_last_seen "
                "ON unifi_client_observations(mac, last_seen)"
            )
        )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_unifi_client_observations_mac_last_seen"))
    op.execute(text("DROP INDEX IF EXISTS ix_unifi_client_observations_ip_first_seen"))
    op.execute(text("DROP TABLE IF EXISTS unifi_client_observations"))
    op.execute(text("DROP INDEX IF EXISTS ix_unifi_clients_ip"))
    op.execute(text("DROP INDEX IF EXISTS ix_unifi_clients_last_seen"))
    op.execute(text("DROP TABLE IF EXISTS unifi_clients"))
