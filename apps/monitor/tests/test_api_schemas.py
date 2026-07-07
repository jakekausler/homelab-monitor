"""Smoke tests for EPIC-010 Pydantic response models in ``kernel.api.schemas``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from homelab_monitor.kernel.api.schemas import Recommendation, ToolScorecard


def test_pydantic_models_construct() -> None:
    """ToolScorecard and Recommendation construct with valid field values."""
    scorecard = ToolScorecard(
        id="sc-1",
        dimension="source_tool",
        dimension_value="vmalert",
        window="7d",
        alerts_emitted=10,
        action_rate=0.5,
        dedup_overlap=0.1,
        unique_share=0.9,
        computed_at="2026-07-06T00:00:00Z",
    )
    assert scorecard.dimension == "source_tool"

    recommendation = Recommendation(
        id="rec-1",
        dimension="source_tool",
        dimension_value="vmalert",
        rule_name="high-noise-rule",
        message="This rule fires often with low action rate.",
        severity="warning",
        status="pending",
        created_at="2026-07-06T00:00:00Z",
    )
    assert recommendation.status == "pending"
    assert recommendation.decided_at is None
    assert recommendation.decided_by is None
    assert recommendation.applied_action_id is None


def test_tool_scorecard_rejects_invalid_literals() -> None:
    """Literal-typed fields reject values outside the locked vocabulary."""
    with pytest.raises(ValidationError):
        ToolScorecard.model_validate(
            {
                "id": "sc-x",
                "dimension": "not_a_dimension",
                "dimension_value": "x",
                "window": "7d",
                "alerts_emitted": 0,
                "action_rate": 0.0,
                "dedup_overlap": 0.0,
                "unique_share": 0.0,
                "computed_at": "2026-07-06T00:00:00Z",
            }
        )
    with pytest.raises(ValidationError):
        ToolScorecard.model_validate(
            {
                "id": "sc-y",
                "dimension": "source_tool",
                "dimension_value": "x",
                "window": "14d",
                "alerts_emitted": 0,
                "action_rate": 0.0,
                "dedup_overlap": 0.0,
                "unique_share": 0.0,
                "computed_at": "2026-07-06T00:00:00Z",
            }
        )


def test_recommendation_rejects_invalid_severity_and_status() -> None:
    """severity and status Literals reject values outside their locked vocabulary."""
    base = {
        "id": "rec-x",
        "dimension": "source_tool",
        "dimension_value": "vmalert",
        "rule_name": "r",
        "message": "m",
        "created_at": "2026-07-06T00:00:00Z",
    }
    with pytest.raises(ValidationError):
        Recommendation.model_validate({**base, "severity": "critical", "status": "pending"})
    with pytest.raises(ValidationError):
        Recommendation.model_validate({**base, "severity": "warning", "status": "acknowledged"})
