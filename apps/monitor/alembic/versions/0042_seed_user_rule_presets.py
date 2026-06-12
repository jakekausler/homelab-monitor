"""STAGE-005-016: seed 6 disabled HA-value preset user-rules.

Seeds six PRESET rows into log_user_rules. Each preset is a ready-to-use vmalert
metricsql rule over the homelab_ha_sensor_value metric (added this stage by the
ha_sensor_value collector), but uses a PLACEHOLDER entity_id (e.g.
sensor.freezer_temp) that may not exist on a given install. Presets ship DISABLED
(enabled=0) so they never render or fire until an operator edits the entity_id +
threshold and enables them.

Unlike the demo-cron seed (0008), this migration is NON-gated (no
HOMELAB_MONITOR_INCLUDE_DEMO_SEEDS guard): presets are a product feature that must
reach PRODUCTION, not a dev convenience. The migration is idempotent by alembic's
once-per-DB nature; it does NOT re-seed on startup, so an operator who deletes a
preset keeps it deleted.

source_kind='preset' marks these rows so the UI can distinguish them from
user-authored ('manual') rules. expr_kind='metricsql' routes them to the metrics
render dir. for_duration='15m' is a sane default debounce.

op.bulk_insert BYPASSES the repo's _validate_and_render_check, so each row's
(rule_name, expr, expr_kind, severity, summary, description, for_duration) is
hand-crafted to pass _validate_fields AND render via render_yaml. The
STAGE-005-016 acceptance test asserts every preset renders cleanly.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | None = None
depends_on: str | None = None

# Static seed timestamp keeps the migration deterministic (migrations cannot call
# utc_now_iso at import time meaningfully; a fixed ISO-8601 UTC literal is the
# convention, mirroring 0008's _DEMO_NOW).
_SEED_NOW = "2026-06-12T00:00:00+00:00"

_PRESET_DESCRIPTION = (
    "PRESET — edit entity_id to your actual sensor, adjust threshold, then "
    "enable. Disabled by default because the placeholder entity does not exist "
    "on your system."
)

# Each preset: rule_name (unique, Preset* prefix), expr (metricsql), severity.
_PRESETS: list[dict[str, object]] = [
    {
        "rule_name": "PresetFreezerTooWarm",
        "expr": 'homelab_ha_sensor_value{entity_id="sensor.freezer_temp"} > -15',
        "severity": "warning",
        "summary": "Freezer too warm (preset)",
    },
    {
        "rule_name": "PresetFridgeTooWarm",
        "expr": 'homelab_ha_sensor_value{entity_id="sensor.fridge_temp"} > 5',
        "severity": "warning",
        "summary": "Fridge too warm (preset)",
    },
    {
        "rule_name": "PresetIndoorTempLow",
        "expr": 'homelab_ha_sensor_value{entity_id="sensor.indoor_temp"} < 16',
        "severity": "warning",
        "summary": "Indoor temperature low (preset)",
    },
    {
        "rule_name": "PresetIndoorTempHigh",
        "expr": 'homelab_ha_sensor_value{entity_id="sensor.indoor_temp"} > 28',
        "severity": "warning",
        "summary": "Indoor temperature high (preset)",
    },
    {
        "rule_name": "PresetHumidityHigh",
        "expr": 'homelab_ha_sensor_value{entity_id="sensor.indoor_humidity"} > 65',
        "severity": "warning",
        "summary": "Indoor humidity high (preset)",
    },
    {
        "rule_name": "PresetHumidityLow",
        "expr": 'homelab_ha_sensor_value{entity_id="sensor.indoor_humidity"} < 30',
        "severity": "info",
        "summary": "Indoor humidity low (preset)",
    },
]

_PRESET_NAMES = [p["rule_name"] for p in _PRESETS]


def _seed_rows() -> list[dict[str, object]]:
    """Build the full row dicts (all NOT NULL + defaulted columns set explicitly)."""
    return [
        {
            "rule_name": p["rule_name"],
            "expr": p["expr"],
            "expr_kind": "metricsql",
            "severity": p["severity"],
            "summary": p["summary"],
            "description": _PRESET_DESCRIPTION,
            "for_duration": "15m",
            "source_kind": "preset",
            "source_ref": None,
            "enabled": 0,
            "created_at": _SEED_NOW,
            "updated_at": _SEED_NOW,
        }
        for p in _PRESETS
    ]


def upgrade() -> None:
    table = sa.table(
        "log_user_rules",
        sa.column("rule_name", sa.Text),
        sa.column("expr", sa.Text),
        sa.column("expr_kind", sa.Text),
        sa.column("severity", sa.Text),
        sa.column("summary", sa.Text),
        sa.column("description", sa.Text),
        sa.column("for_duration", sa.Text),
        sa.column("source_kind", sa.Text),
        sa.column("source_ref", sa.Text),
        sa.column("enabled", sa.Integer),
        sa.column("created_at", sa.Text),
        sa.column("updated_at", sa.Text),
    )
    op.bulk_insert(table, _seed_rows())


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM log_user_rules WHERE rule_name IN :names").bindparams(
            sa.bindparam("names", value=_PRESET_NAMES, expanding=True)
        )
    )
