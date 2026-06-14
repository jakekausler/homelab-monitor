"""Unit tests for kernel/ha/enrichment.py (STAGE-005-031).

Pure helper tests (no FastAPI, no network). Covers:
- build_states_index: empty, dedup, empty-entity_id skip
- attr_str: present str, None state, missing key, non-str value
- extract_issues: bare list, {"issues":[...]}, degenerate {}
- build_repairs_index: keyed tuple -> RepairEnrichment(description, learn_more_url),
  present -> value / missing -> None / non-str -> None / empty-str -> None (both
  fields), non-dict skip, missing domain/issue_id skip
"""

from __future__ import annotations

from homelab_monitor.kernel.ha.client import HaState
from homelab_monitor.kernel.ha.enrichment import (
    RepairEnrichment,
    attr_str,
    build_repairs_index,
    build_states_index,
    extract_issues,
)


def _state(entity_id: str, attributes: dict[str, object]) -> HaState:
    """Construct an HaState with the given id + attributes (other fields empty)."""
    return HaState(
        entity_id=entity_id,
        state="on",
        attributes=attributes,
        last_changed="",
        last_updated="",
    )


# ── build_states_index ──────────────────────────────────────────────────────


def test_build_states_index_empty() -> None:
    assert build_states_index([]) == {}


def test_build_states_index_keys_by_entity_id() -> None:
    a = _state("light.a", {"friendly_name": "A"})
    b = _state("sensor.b", {"friendly_name": "B"})
    index = build_states_index([a, b])
    assert index["light.a"] is a
    assert index["sensor.b"] is b


def test_build_states_index_dedup_last_wins() -> None:
    first = _state("light.a", {"friendly_name": "First"})
    second = _state("light.a", {"friendly_name": "Second"})
    index = build_states_index([first, second])
    assert index["light.a"] is second


def test_build_states_index_skips_empty_entity_id() -> None:
    blank = _state("", {"friendly_name": "no id"})
    ok = _state("light.a", {"friendly_name": "A"})
    index = build_states_index([blank, ok])
    assert "" not in index
    assert set(index) == {"light.a"}


# ── attr_str ────────────────────────────────────────────────────────────────


def test_attr_str_present_str() -> None:
    state = _state("light.a", {"friendly_name": "Living Room"})
    assert attr_str(state, "friendly_name") == "Living Room"


def test_attr_str_none_state() -> None:
    assert attr_str(None, "friendly_name") is None


def test_attr_str_missing_key() -> None:
    state = _state("light.a", {})
    assert attr_str(state, "friendly_name") is None


def test_attr_str_non_str_value() -> None:
    state = _state("light.a", {"friendly_name": 123, "other": {"nested": True}})
    assert attr_str(state, "friendly_name") is None
    assert attr_str(state, "other") is None


# ── extract_issues ──────────────────────────────────────────────────────────


def test_extract_issues_bare_list() -> None:
    issues: list[object] = [{"domain": "zwave", "issue_id": "i1"}]
    assert extract_issues(issues) == issues


def test_extract_issues_dict_wrapped() -> None:
    payload: dict[str, object] = {"issues": [{"domain": "zwave", "issue_id": "i1"}]}
    assert extract_issues(payload) == [{"domain": "zwave", "issue_id": "i1"}]


def test_extract_issues_degenerate_dict() -> None:
    assert extract_issues({}) == []


def test_extract_issues_dict_non_list_candidate() -> None:
    assert extract_issues({"issues": "not-a-list"}) == []


# ── build_repairs_index ─────────────────────────────────────────────────────


def test_build_repairs_index_keys_tuple_to_enrichment() -> None:
    issues: list[object] = [
        {
            "domain": "zwave",
            "issue_id": "battery_low",
            "description": "Battery is low",
            "learn_more_url": "https://example.com/zwave",
        },
    ]
    index = build_repairs_index(issues)
    enrichment = index[("zwave", "battery_low")]
    assert enrichment == RepairEnrichment(
        description="Battery is low",
        learn_more_url="https://example.com/zwave",
    )
    assert enrichment.description == "Battery is low"
    assert enrichment.learn_more_url == "https://example.com/zwave"


def test_build_repairs_index_missing_fields_are_none() -> None:
    """No description / no learn_more_url keys (stock HA) -> both None."""
    issues: list[object] = [{"domain": "mqtt", "issue_id": "conn"}]
    index = build_repairs_index(issues)
    enrichment = index[("mqtt", "conn")]
    assert enrichment.description is None
    assert enrichment.learn_more_url is None


def test_build_repairs_index_non_str_fields_are_none() -> None:
    """Non-str description / learn_more_url -> both None."""
    issues: list[object] = [
        {"domain": "mqtt", "issue_id": "conn", "description": 5, "learn_more_url": []},
    ]
    index = build_repairs_index(issues)
    enrichment = index[("mqtt", "conn")]
    assert enrichment.description is None
    assert enrichment.learn_more_url is None


def test_build_repairs_index_empty_str_fields_are_none() -> None:
    """Empty-string description / learn_more_url -> both None."""
    issues: list[object] = [
        {"domain": "mqtt", "issue_id": "conn", "description": "", "learn_more_url": ""},
    ]
    index = build_repairs_index(issues)
    enrichment = index[("mqtt", "conn")]
    assert enrichment.description is None
    assert enrichment.learn_more_url is None


def test_build_repairs_index_fields_degrade_independently() -> None:
    """description present + learn_more_url missing -> description set, url None."""
    issues: list[object] = [
        {"domain": "zwave", "issue_id": "i", "description": "prose only"},
    ]
    index = build_repairs_index(issues)
    enrichment = index[("zwave", "i")]
    assert enrichment.description == "prose only"
    assert enrichment.learn_more_url is None


def test_build_repairs_index_skips_non_dict() -> None:
    issues: list[object] = [
        "not-a-dict",
        {"domain": "z", "issue_id": "i", "description": "d", "learn_more_url": "u"},
    ]
    index = build_repairs_index(issues)
    assert index == {
        ("z", "i"): RepairEnrichment(description="d", learn_more_url="u"),
    }


def test_build_repairs_index_skips_missing_domain_or_issue_id() -> None:
    issues: list[object] = [
        {"issue_id": "no_domain", "description": "x"},
        {"domain": "has_domain", "description": "y"},
        {"domain": "", "issue_id": "empty_domain", "description": "z"},
    ]
    index = build_repairs_index(issues)
    assert index == {}
