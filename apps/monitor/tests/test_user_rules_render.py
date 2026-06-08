"""Tests for ``kernel.logs.user_rules_render``: pure render, atomic write, render_all."""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
import yaml

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.user_rules_render import (
    _atomic_write,  # pyright: ignore[reportPrivateUsage]
    render_all,
    render_paths_from_env,
    render_yaml,
)
from homelab_monitor.kernel.logs.user_rules_repo import LogUserRule, LogUserRulesRepository


def _rule(name: str, kind: str = "logsql", **kw: object) -> LogUserRule:
    """Build a LogUserRule directly for pure-render tests."""
    base: dict[str, object] = dict(
        id=1,
        rule_name=name,
        expr='_msg:"boom"',
        expr_kind=kind,
        severity="warning",
        summary="A summary",
        description="A description",
        for_duration="5m",
        source_kind="manual",
        source_ref=None,
        enabled=True,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    base.update(kw)
    return LogUserRule(**base)  # type: ignore[arg-type]


def test_render_yaml_empty_list_returns_groups_list() -> None:
    """render_yaml([]) returns exactly 'groups: []\\n'."""
    result = render_yaml([])
    assert result == "groups: []\n"


def test_render_yaml_logsql_rule_parses_and_has_correct_structure() -> None:
    """render_yaml([logsql_rule]) emits valid YAML with correct structure."""
    rule = _rule("TestAlert")
    yaml_str = render_yaml([rule])
    doc = yaml.safe_load(yaml_str)
    assert "groups" in doc
    assert len(doc["groups"]) == 1
    group = doc["groups"][0]
    assert group["name"] == "user-rules-logs"
    assert group["type"] == "vlogs"  # Logs rules require group-level type: vlogs
    assert group["interval"] == "60s"
    assert "rules" in group
    assert len(group["rules"]) == 1
    alert = group["rules"][0]
    assert alert["alert"] == "TestAlert"
    assert alert["expr"] is not None
    assert alert["for"] == "5m"
    assert alert["labels"]["severity"] == "warning"
    assert alert["labels"]["source_tool"] == "user"
    assert alert["labels"]["category"] == "user-rule"
    assert "summary" in alert["annotations"]
    assert "description" in alert["annotations"]


def test_render_yaml_metricsql_rule_has_correct_group_name() -> None:
    """render_yaml([metricsql_rule]) has group name 'user-rules-metrics'."""
    rule = _rule("MetricsAlert", kind="metricsql")
    yaml_str = render_yaml([rule])
    doc = yaml.safe_load(yaml_str)
    group = doc["groups"][0]
    assert group["name"] == "user-rules-metrics"
    assert "type" not in group  # Metrics rules omit type (default prometheus)


def test_render_yaml_deterministic_ordering() -> None:
    """render_yaml sorts rules by rule_name; multiple calls are deterministic."""
    rules = [_rule("Zebra"), _rule("Alpha"), _rule("Beta")]
    yaml_str = render_yaml(rules)
    doc = yaml.safe_load(yaml_str)
    alerts = [r["alert"] for r in doc["groups"][0]["rules"]]
    assert alerts == ["Alpha", "Beta", "Zebra"]


def test_render_yaml_mixed_expr_kinds_raises() -> None:
    """render_yaml with mixed expr_kinds raises ValueError."""
    rules = [_rule("Rule1", kind="logsql"), _rule("Rule2", kind="metricsql")]
    with pytest.raises(ValueError, match="single expr_kind"):
        render_yaml(rules)


def test_render_yaml_expr_with_yaml_hostile_chars_round_trips() -> None:
    """render_yaml with expr containing ':', '\"' round-trips via block scalar."""
    expr = '_msg:"a: b" AND foo:"x"'
    rule = _rule("SpecialChars", expr=expr)
    yaml_str = render_yaml([rule])
    doc = yaml.safe_load(yaml_str)
    rendered_expr = doc["groups"][0]["rules"][0]["expr"]
    # Block scalar with stripped newlines should preserve the content
    assert rendered_expr.strip() == expr


def test_render_yaml_forbidden_inline_char_in_rule_name_raises() -> None:
    """render_yaml with forbidden inline char in rule_name raises ValueError."""
    rule = _rule('BadName"Quote')
    with pytest.raises(ValueError, match="forbidden inline character"):
        render_yaml([rule])


def test_render_yaml_forbidden_control_char_in_expr_raises() -> None:
    """render_yaml with control char in expr raises ValueError."""
    rule = _rule("ControlChar", expr='_msg:"bad\x00expr"')
    with pytest.raises(ValueError, match="forbidden control character"):
        render_yaml([rule])


def test_render_yaml_forbidden_control_char_in_summary_raises() -> None:
    """render_yaml with control char in summary raises ValueError."""
    rule = _rule("BadSummary", summary="Bad\x00summary")
    with pytest.raises(ValueError, match="forbidden control character"):
        render_yaml([rule])


def test_render_yaml_forbidden_control_char_in_description_raises() -> None:
    """render_yaml with control char in description raises ValueError."""
    rule = _rule("BadDesc", description="Bad\x00description")
    with pytest.raises(ValueError, match="forbidden control character"):
        render_yaml([rule])


async def test_render_all_writes_both_files(repo: SqliteRepository, tmp_path: Path) -> None:
    """render_all writes both logs.yaml and metrics.yaml."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="LogsRule",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="Logs",
    )
    await user_repo.create(
        rule_name="MetricsRule",
        expr="up == 0",
        expr_kind="metricsql",
        severity="critical",
        summary="Metrics",
    )
    logs_path = tmp_path / "logs.yaml"
    metrics_path = tmp_path / "metrics.yaml"
    await render_all(user_repo, logs_path, metrics_path)
    assert logs_path.exists()
    assert metrics_path.exists()


async def test_render_all_correct_rules_in_files(repo: SqliteRepository, tmp_path: Path) -> None:
    """render_all writes only enabled rules of the correct kind to each file."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="EnabledLogs",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="Enabled logs",
        enabled=True,
    )
    await user_repo.create(
        rule_name="DisabledLogs",
        expr="_msg:error",
        expr_kind="logsql",
        severity="info",
        summary="Disabled logs",
        enabled=False,
    )
    await user_repo.create(
        rule_name="EnabledMetrics",
        expr="up == 0",
        expr_kind="metricsql",
        severity="critical",
        summary="Enabled metrics",
        enabled=True,
    )
    logs_path = tmp_path / "logs.yaml"
    metrics_path = tmp_path / "metrics.yaml"
    await render_all(user_repo, logs_path, metrics_path)
    logs_doc = yaml.safe_load(logs_path.read_text())
    metrics_doc = yaml.safe_load(metrics_path.read_text())
    logs_alerts = [r["alert"] for r in logs_doc["groups"][0]["rules"]] if logs_doc["groups"] else []
    metrics_alerts = (
        [r["alert"] for r in metrics_doc["groups"][0]["rules"]] if metrics_doc["groups"] else []
    )
    assert "EnabledLogs" in logs_alerts
    assert "DisabledLogs" not in logs_alerts
    assert "EnabledMetrics" in metrics_alerts


async def test_render_all_empty_kind_becomes_groups_empty_list(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """render_all with no enabled rules of a kind writes 'groups: []'."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="OnlyLogs",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="Only logs",
    )
    logs_path = tmp_path / "logs.yaml"
    metrics_path = tmp_path / "metrics.yaml"
    await render_all(user_repo, logs_path, metrics_path)
    logs_doc = yaml.safe_load(logs_path.read_text())
    metrics_doc = yaml.safe_load(metrics_path.read_text())
    assert logs_doc["groups"] and len(logs_doc["groups"][0]["rules"]) > 0
    assert metrics_doc["groups"] == []


async def test_render_all_delete_then_re_render_clears_file(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """render_all with no enabled rules clears the file (writes 'groups: []')."""
    user_repo = LogUserRulesRepository(repo)
    rule = await user_repo.create(
        rule_name="Ephemeral",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="Ephemeral",
    )
    logs_path = tmp_path / "logs.yaml"
    metrics_path = tmp_path / "metrics.yaml"
    await render_all(user_repo, logs_path, metrics_path)
    initial = yaml.safe_load(logs_path.read_text())
    assert len(initial["groups"][0]["rules"]) > 0
    await user_repo.delete(rule.id)
    await render_all(user_repo, logs_path, metrics_path)
    cleared = yaml.safe_load(logs_path.read_text())
    assert cleared["groups"] == []


async def test_render_all_returns_true_on_success(repo: SqliteRepository, tmp_path: Path) -> None:
    """render_all returns True when both files are written successfully."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="LogsRule",
        expr="_msg:test",
        expr_kind="logsql",
        severity="warning",
        summary="Test rule",
    )
    logs_path = tmp_path / "logs.yaml"
    metrics_path = tmp_path / "metrics.yaml"
    result = await render_all(user_repo, logs_path, metrics_path)
    assert result is True


def test_atomic_write_rejects_non_yaml_extension() -> None:
    """_atomic_write rejects paths not ending in .yaml."""
    log = structlog.get_logger()
    with pytest.raises(ValueError, match=r"must end in \.yaml"):
        _atomic_write(Path("rules.yml"), "groups: []\n", log)  # pyright: ignore[reportPrivateUsage]


async def test_render_all_round_trip_semantics(repo: SqliteRepository, tmp_path: Path) -> None:
    """Round-trip: create rule -> render_all -> parse YAML -> verify alert matches."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="RoundTripAlert",
        expr="_msg:test",
        expr_kind="logsql",
        severity="critical",
        summary="Round trip test",
        description="Testing round-trip",
    )
    logs_path = tmp_path / "logs.yaml"
    metrics_path = tmp_path / "metrics.yaml"
    await render_all(user_repo, logs_path, metrics_path)
    doc = yaml.safe_load(logs_path.read_text())
    assert doc["groups"] and len(doc["groups"]) > 0
    rendered_alert = None
    for rule in doc["groups"][0]["rules"]:
        if rule["alert"] == "RoundTripAlert":
            rendered_alert = rule
            break
    assert rendered_alert is not None
    assert rendered_alert["labels"]["severity"] == "critical"


def test_render_paths_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """render_paths_from_env with no env set returns defaults."""
    monkeypatch.delenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", raising=False)
    logs_path, metrics_path = render_paths_from_env()
    assert str(logs_path) == "/var/vmalert-user-logs/logs.yaml"
    assert str(metrics_path) == "/var/vmalert-user-metrics/metrics.yaml"


def test_render_paths_from_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """render_paths_from_env with env overrides uses the custom dirs."""
    logs_dir = tmp_path / "custom-logs"
    metrics_dir = tmp_path / "custom-metrics"
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(logs_dir))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(metrics_dir))
    logs_path, metrics_path = render_paths_from_env()
    assert str(logs_path) == str(logs_dir / "logs.yaml")
    assert str(metrics_path) == str(metrics_dir / "metrics.yaml")


def test_render_yaml_unknown_expr_kind_raises() -> None:
    """render_yaml with unknown expr_kind raises ValueError."""
    rule = _rule("TestAlert", kind="unknown")
    with pytest.raises(ValueError, match="unknown expr_kind"):
        render_yaml([rule])


def test_render_yaml_severity_with_forbidden_char_raises() -> None:
    """render_yaml with forbidden inline character in severity raises ValueError."""
    rule = _rule("TestAlert", severity="warn\ning")  # newline is forbidden
    with pytest.raises(ValueError, match="severity contains a forbidden inline character"):
        render_yaml([rule])


def test_render_yaml_expr_with_forbidden_char_raises() -> None:
    """render_yaml with forbidden block character in expr raises ValueError."""
    rule = _rule("TestAlert", expr='_msg:"boom\x00"')  # null byte is forbidden
    with pytest.raises(ValueError, match="expr contains a forbidden control character"):
        render_yaml([rule])


def test_render_yaml_summary_with_forbidden_char_raises() -> None:
    """render_yaml with forbidden block character in summary raises ValueError."""
    rule = _rule("TestAlert", summary="A summary\x00with null byte")
    with pytest.raises(ValueError, match="summary contains a forbidden control character"):
        render_yaml([rule])


def test_render_yaml_description_with_forbidden_char_raises() -> None:
    """render_yaml with forbidden block character in description raises ValueError."""
    rule = _rule("TestAlert", description="A description\x00with null byte")
    with pytest.raises(ValueError, match="description contains a forbidden control character"):
        render_yaml([rule])


__all__: list[str] = []
