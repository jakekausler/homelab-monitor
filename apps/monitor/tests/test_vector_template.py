"""Tests for deploy/vector/vector.toml.template (STAGE-002-012).

Validates structural requirements of the Vector config template without needing
the `vector` binary. Uses tomllib to parse (after substituting the one token
placeholder) and asserts that the new hmrun branch is present and that the
drop_noise condition excludes hmrun lines.

Limitation: this is a structural/TOML-parse check only. Full Vector semantic
validation (type-correct inputs/outputs, routing correctness) is performed at
prod-rig refinement (3b) with a live `vector validate` run.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

_TEMPLATE_PATH = Path(__file__).parents[3] / "deploy" / "vector" / "vector.toml.template"


def _render_template() -> str:
    """Substitute the single token placeholder with a dummy value."""
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return text.replace("${CRON_EVENTS_INGEST_TOKEN}", "dummy-token-for-test")


@pytest.fixture(scope="module")
def parsed_config() -> dict[str, Any]:
    """Render + parse the vector.toml.template once for the module."""
    rendered = _render_template()
    return tomllib.loads(rendered)


def test_template_is_valid_toml(parsed_config: dict[str, Any]) -> None:
    """The rendered template must be valid TOML (tomllib.loads succeeds)."""
    assert isinstance(parsed_config, dict)


def test_hmrun_filter_transform_present(parsed_config: dict[str, Any]) -> None:
    """[transforms.hmrun_filter] table must exist."""
    transforms = parsed_config.get("transforms", {})
    assert "hmrun_filter" in transforms, (
        f"hmrun_filter missing from transforms; keys: {list(transforms.keys())}"
    )


def test_hmrun_shaped_transform_present(parsed_config: dict[str, Any]) -> None:
    """[transforms.hmrun_shaped] table must exist."""
    transforms = parsed_config.get("transforms", {})
    assert "hmrun_shaped" in transforms, (
        f"hmrun_shaped missing from transforms; keys: {list(transforms.keys())}"
    )


def test_sinks_vl_inputs_includes_hmrun_shaped(parsed_config: dict[str, Any]) -> None:
    """[sinks.vl].inputs must include 'hmrun_shaped'."""
    sinks = parsed_config.get("sinks", {})
    vl = sinks.get("vl", {})
    inputs = vl.get("inputs", [])
    assert "hmrun_shaped" in inputs, f"hmrun_shaped not in sinks.vl.inputs: {inputs}"


def test_drop_noise_excludes_hmrun(parsed_config: dict[str, Any]) -> None:
    """[transforms.drop_noise].condition must exclude hmrun lines."""
    transforms = parsed_config.get("transforms", {})
    drop_noise = transforms.get("drop_noise", {})
    condition = drop_noise.get("condition", "")
    assert 'SYSLOG_IDENTIFIER != "hmrun"' in condition, (
        f"drop_noise condition does not exclude hmrun:\n{condition}"
    )


def test_hmrun_shaped_source_contains_parse_regex(parsed_config: dict[str, Any]) -> None:
    """hmrun_shaped source VRL must contain parse_regex and run_id extraction."""
    transforms = parsed_config.get("transforms", {})
    hmrun_shaped = transforms.get("hmrun_shaped", {})
    source = hmrun_shaped.get("source", "")
    assert "parse_regex" in source, "parse_regex not found in hmrun_shaped source"
    assert "run_id" in source, "run_id not found in hmrun_shaped source"


def test_hmrun_shaped_source_has_uuid_pattern(parsed_config: dict[str, Any]) -> None:
    """hmrun_shaped VRL regex must have a 36-char UUID capture group."""
    transforms = parsed_config.get("transforms", {})
    hmrun_shaped = transforms.get("hmrun_shaped", {})
    source = hmrun_shaped.get("source", "")
    assert "{36}" in source, "36-char uuid pattern not found in hmrun_shaped source"
