"""Tests for alembic migration 0042_seed_user_rule_presets."""

from __future__ import annotations

import pytest
from sqlalchemy import bindparam, text

from homelab_monitor.kernel.db.migrations import run_migrations
from tests.conftest import make_engine

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
async def test_upgrade_seeds_six_disabled_presets() -> None:
    """Migration 0042 seeds 6 preset rows: disabled, source_kind=preset, metricsql."""
    engine = make_engine()
    try:
        await run_migrations(engine)
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT rule_name, expr_kind, source_kind, enabled, for_duration "
                    "FROM log_user_rules WHERE source_kind = 'preset'"
                )
            )
            rows = result.fetchall()
        assert len(rows) == _EXPECTED_COUNT
        names = {r.rule_name for r in rows}
        assert names == _EXPECTED_PRESET_NAMES
        for r in rows:
            assert r.expr_kind == "metricsql"
            assert r.source_kind == "preset"
            assert r.enabled == 0
            assert r.for_duration == "15m"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_is_idempotent() -> None:
    """Running migrations twice does not duplicate presets (alembic runs once)."""
    engine = make_engine()
    try:
        await run_migrations(engine)
        await run_migrations(engine)
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT COUNT(*) AS n FROM log_user_rules WHERE source_kind = 'preset'")
            )
            row = result.first()
        assert row is not None
        assert row.n == _EXPECTED_COUNT
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_deletes_presets_by_name() -> None:
    """A delete by the preset rule_names removes exactly the seeded rows (downgrade shape)."""
    engine = make_engine()
    try:
        await run_migrations(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM log_user_rules WHERE rule_name IN :names").bindparams(
                    bindparam("names", value=sorted(_EXPECTED_PRESET_NAMES), expanding=True)
                )
            )
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT COUNT(*) AS n FROM log_user_rules WHERE source_kind = 'preset'")
            )
            row = result.first()
        assert row is not None
        assert row.n == 0
    finally:
        await engine.dispose()
