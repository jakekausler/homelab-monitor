"""Unit tests for render_signature_spike_rule (STAGE-004-036).

Binding gate for deploy/vmalert/metrics/signature_spike.yml.tmpl: the rendered
output must parse as YAML, carry the correct labels/for, keep vmalert Go-template
directives un-substituted, and encode both baseline branches + the min-baseline
floor. Validation paths raise ValueError. 100% coverage of the render module.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest
import yaml

from homelab_monitor.kernel.logs.signature_spike_render import (
    _TEMPLATE,  # pyright: ignore[reportPrivateUsage]
    render_signature_spike_rule,
)

#: Repo-relative path to the canonical template artifact (asserted byte-identical
#: to the module's embedded constant).
_TMPL_PATH = (
    Path(__file__).resolve().parents[3]
    / "deploy"
    / "vmalert"
    / "metrics"
    / "signature_spike.yml.tmpl"
)


def _first_rule(rendered: str) -> dict[str, object]:
    """Parse rendered YAML and return the single rule dict (pyright-narrowed)."""
    doc = cast("dict[str, object]", yaml.safe_load(rendered))
    groups = cast("list[dict[str, object]]", doc["groups"])
    rules = cast("list[dict[str, object]]", groups[0]["rules"])
    return rules[0]


def test_renders_and_parses_as_yaml() -> None:
    rendered = render_signature_spike_rule(template_hash="abc12345def", service_key="svcA")
    doc = yaml.safe_load(rendered)  # must not raise
    assert isinstance(doc, dict)
    assert "groups" in doc


def test_substitutes_parameters_into_expr() -> None:
    rendered = render_signature_spike_rule(
        template_hash="abc12345def",
        service_key="mysvc",
        multiplier=7,
        window="10m",
        min_baseline=20,
    )
    rule = _first_rule(rendered)
    expr = cast("str", rule["expr"])
    assert 'service_key="mysvc"' in expr
    assert 'template_hash="abc12345def"' in expr
    assert "[10m]" in expr  # window substituted
    assert "7 * max(" in expr  # multiplier substituted
    # min_baseline floor substituted: it renders as the 2nd arg of max(...) on
    # its own line, so collapse whitespace before asserting "max( ..., 20 )".
    assert ", 20 )" in " ".join(expr.split())  # min_baseline floor substituted
    # No unsubstituted sentinels remain in the expr:
    for token in (
        "__SERVICE_KEY__",
        "__TEMPLATE_HASH__",
        "__WINDOW__",
        "__MULTIPLIER__",
        "__MIN_BASELINE__",
    ):
        assert token not in rendered
    assert "__ALERT_SLUG__" not in rendered


def test_alert_slug_sanitized() -> None:
    rendered = render_signature_spike_rule(
        template_hash="DEADBEEFcafe1234", service_key="svc/foo-1"
    )
    rule = _first_rule(rendered)
    # service_key sanitized to svc_foo_1; hash truncated to first 8 chars.
    assert rule["alert"] == "SignatureSpike_svc_foo_1_DEADBEEF"


def test_labels_block_complete() -> None:
    rendered = render_signature_spike_rule(template_hash="abc12345", service_key="svcA")
    rule = _first_rule(rendered)
    labels = cast("dict[str, object]", rule["labels"])
    assert labels["severity"] == "warning"
    assert labels["source_tool"] == "vmalert-metrics"
    assert labels["category"] == "log-anomaly"
    assert labels["anomaly_kind"] == "signature_spike"
    assert labels["target_kind"] == "log_signature"
    assert labels["template_hash"] == "abc12345"
    assert labels["service_key"] == "svcA"
    assert rule["for"] == "1m"


def test_go_template_directives_survive() -> None:
    rendered = render_signature_spike_rule(template_hash="abc12345", service_key="svcA")
    # vmalert Go-template directives must NOT be substituted by the renderer.
    assert "{{ $labels.service_key }}" in rendered
    assert "{{ $labels.template_hash }}" in rendered
    assert "{{ $value }}" in rendered
    # Deep-link uses both label directives:
    assert "/logs/signatures#{{ $labels.template_hash }}/{{ $labels.service_key }}" in rendered


def test_cold_start_branches_present() -> None:
    rendered = render_signature_spike_rule(template_hash="abc12345", service_key="svcA")
    rule = _first_rule(rendered)
    expr = cast("str", rule["expr"])
    assert ">= 604800" in expr  # mature-branch age guard
    assert "< 604800" in expr  # cold-start age guard
    assert "/ 1e9" in expr  # ns -> s conversion
    assert "[7d:" in expr  # 7-day baseline (mature)
    assert "[1h:" in expr  # 1-hour baseline (cold-start)


def test_min_baseline_floor_present() -> None:
    rendered = render_signature_spike_rule(
        template_hash="abc12345", service_key="svcA"
    )  # default min_baseline=10
    rule = _first_rule(rendered)
    expr = cast("str", rule["expr"])
    assert "max(" in expr
    # The floor renders as the 2nd arg of max(...) on its own line; collapse
    # whitespace before asserting "max( ..., 10 )". BOTH branches (7d mature + 1h
    # cold-start) must carry the floor — assert it appears exactly twice so a
    # single-branch floor removal is caught here, not only by the drift test.
    collapsed = " ".join(expr.split())
    assert collapsed.count(", 10 )") == 2  # noqa: PLR2004 -- floor in both branches


def test_rejects_quote_in_service_key() -> None:
    with pytest.raises(ValueError, match="must not contain"):
        render_signature_spike_rule(template_hash="abc12345", service_key='sv"c')


def test_rejects_backslash_in_service_key() -> None:
    with pytest.raises(ValueError, match="must not contain"):
        render_signature_spike_rule(template_hash="abc12345", service_key="sv\\c")


def test_rejects_quote_in_template_hash() -> None:
    with pytest.raises(ValueError, match="must not contain"):
        render_signature_spike_rule(template_hash='ab"cd', service_key="svcA")


def test_rejects_backslash_in_template_hash() -> None:
    with pytest.raises(ValueError, match="must not contain"):
        render_signature_spike_rule(template_hash="ab\\cd", service_key="svcA")


def test_rejects_multiplier_below_one() -> None:
    with pytest.raises(ValueError, match="multiplier must be >= 1"):
        render_signature_spike_rule(template_hash="abc12345", service_key="svcA", multiplier=0)


def test_rejects_min_baseline_below_one() -> None:
    with pytest.raises(ValueError, match="min_baseline must be >= 1"):
        render_signature_spike_rule(template_hash="abc12345", service_key="svcA", min_baseline=0)


def test_rejects_bad_window() -> None:
    with pytest.raises(ValueError, match="window must match"):
        render_signature_spike_rule(template_hash="abc12345", service_key="svcA", window="5x")


def test_tmpl_file_matches_embedded_constant() -> None:
    """The on-disk .tmpl artifact must be byte-identical to the embedded template."""
    disk = _TMPL_PATH.read_text(encoding="utf-8")
    assert disk == _TEMPLATE


def test_rejects_newline_in_service_key() -> None:
    with pytest.raises(ValueError, match="control character"):
        render_signature_spike_rule(template_hash="abc12345", service_key="foo\nbar")


def test_rejects_newline_in_template_hash() -> None:
    with pytest.raises(ValueError, match="control character"):
        render_signature_spike_rule(template_hash="ab\ncd", service_key="svcA")


def test_slug_collision_disambiguated_by_labels() -> None:
    a = _first_rule(render_signature_spike_rule(template_hash="abcd1234ZZ", service_key="foo/bar"))
    b = _first_rule(render_signature_spike_rule(template_hash="abcd1234YY", service_key="foo_bar"))
    assert a["alert"] == b["alert"]  # slug collides (documented limitation)
    a_labels = cast("dict[str, object]", a["labels"])
    b_labels = cast("dict[str, object]", b["labels"])
    assert a_labels["service_key"] == "foo/bar"
    assert b_labels["service_key"] == "foo_bar"  # labels disambiguate


def test_slug_is_valid_alertname_for_adversarial_keys() -> None:
    name_re = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    for sk in ("", "/-./", "9foo", "a b"):
        rule = _first_rule(render_signature_spike_rule(template_hash="ab", service_key=sk))
        assert name_re.match(cast("str", rule["alert"]))


__all__ = []
