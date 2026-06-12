"""Acceptance test: HA-value preset user-rules (STAGE-005-016).

The 6 presets are seeded by migration 0042 into the migrated test DB (the `repo`
fixture runs alembic_upgrade_head). These tests assert: all 6 exist with
enabled=0 / source_kind='preset' / expr_kind='metricsql'; each preset's expr
RENDERS cleanly through the REAL render path (render_yaml); and editing a preset's
threshold via repo.update() changes the rendered vmalert YAML.
"""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.user_rules_render import render_yaml
from homelab_monitor.kernel.logs.user_rules_repo import LogUserRulesRepository

_EXPECTED_PRESET_NAMES = {
    "PresetFreezerTooWarm",
    "PresetFridgeTooWarm",
    "PresetIndoorTempLow",
    "PresetIndoorTempHigh",
    "PresetHumidityHigh",
    "PresetHumidityLow",
}
_EXPECTED_COUNT = 6


@pytest.mark.asyncio
async def test_all_six_presets_seeded_disabled(repo: SqliteRepository) -> None:
    """All 6 presets exist with enabled=False, source_kind=preset, expr_kind=metricsql."""
    user_repo = LogUserRulesRepository(repo)
    presets = [r for r in await user_repo.list_all() if r.source_kind == "preset"]
    assert len(presets) == _EXPECTED_COUNT
    assert {p.rule_name for p in presets} == _EXPECTED_PRESET_NAMES
    for p in presets:
        assert p.enabled is False
        assert p.source_kind == "preset"
        assert p.expr_kind == "metricsql"
        assert p.for_duration == "15m"


@pytest.mark.asyncio
async def test_each_preset_renders_cleanly(repo: SqliteRepository) -> None:
    """Every preset's expr renders without error through the real render_yaml path."""
    user_repo = LogUserRulesRepository(repo)
    presets = [r for r in await user_repo.list_all() if r.source_kind == "preset"]
    assert len(presets) == _EXPECTED_COUNT
    for p in presets:
        rendered = render_yaml([p])
        # Renders a valid metrics group containing the alert name + expr.
        assert f"- alert: {p.rule_name}" in rendered
        assert "user-rules-metrics" in rendered


@pytest.mark.asyncio
async def test_disabled_presets_excluded_from_render_all_path(repo: SqliteRepository) -> None:
    """list_enabled() (what render_all reads) excludes the disabled presets."""
    user_repo = LogUserRulesRepository(repo)
    enabled_names = {r.rule_name for r in await user_repo.list_enabled()}
    assert _EXPECTED_PRESET_NAMES.isdisjoint(enabled_names)


@pytest.mark.asyncio
async def test_editing_preset_threshold_changes_render(repo: SqliteRepository) -> None:
    """Editing a preset's expr via update() changes the rendered vmalert YAML."""
    user_repo = LogUserRulesRepository(repo)
    preset = await user_repo.get_by_name("PresetFreezerTooWarm")
    assert preset is not None

    before = render_yaml([preset])

    new_expr = 'homelab_ha_sensor_value{entity_id="sensor.freezer_temp"} > -10'
    updated = await user_repo.update(preset.id, expr=new_expr)
    assert updated is not None
    assert updated.expr == new_expr

    after = render_yaml([updated])
    assert before != after
    assert "> -10" in after
