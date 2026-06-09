"""Tests for ``kernel.logs.user_rules_render``: pure render, atomic write, render_all."""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
import yaml

import homelab_monitor.kernel.logs.user_rules_render as _user_rules_render_mod
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.user_rules_render import (
    _atomic_write,  # pyright: ignore[reportPrivateUsage]
    _render_kind_dir,  # pyright: ignore[reportPrivateUsage]
    render_all,
    render_dirs_from_env,
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


async def test_render_all_writes_per_rule_files(repo: SqliteRepository, tmp_path: Path) -> None:
    """render_all writes one file per rule, named <rule_name>.yaml, per dir."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="LogsRule",
        expr="_msg:error | stats count() as match_count | filter match_count:>0",
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
    logs_dir = tmp_path / "logs"
    metrics_dir = tmp_path / "metrics"
    await render_all(user_repo, logs_dir, metrics_dir)
    assert (logs_dir / "LogsRule.yaml").exists()
    assert (metrics_dir / "MetricsRule.yaml").exists()
    # No aggregate files remain.
    assert not (logs_dir / "logs.yaml").exists()
    assert not (metrics_dir / "metrics.yaml").exists()


async def test_render_all_correct_rules_in_files(repo: SqliteRepository, tmp_path: Path) -> None:
    """render_all writes a file only for enabled rules of the correct kind."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="EnabledLogs",
        expr="_msg:error | stats count() as match_count | filter match_count:>0",
        expr_kind="logsql",
        severity="warning",
        summary="Enabled logs",
        enabled=True,
    )
    await user_repo.create(
        rule_name="DisabledLogs",
        expr="_msg:error | stats count() as match_count | filter match_count:>0",
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
    logs_dir = tmp_path / "logs"
    metrics_dir = tmp_path / "metrics"
    await render_all(user_repo, logs_dir, metrics_dir)
    assert (logs_dir / "EnabledLogs.yaml").exists()
    assert not (logs_dir / "DisabledLogs.yaml").exists()
    assert (metrics_dir / "EnabledMetrics.yaml").exists()
    # Each file is a valid one-rule group.
    logs_doc = yaml.safe_load((logs_dir / "EnabledLogs.yaml").read_text())
    assert logs_doc["groups"][0]["name"] == "user-rules-logs"
    assert [r["alert"] for r in logs_doc["groups"][0]["rules"]] == ["EnabledLogs"]
    metrics_doc = yaml.safe_load((metrics_dir / "EnabledMetrics.yaml").read_text())
    assert metrics_doc["groups"][0]["name"] == "user-rules-metrics"
    assert [r["alert"] for r in metrics_doc["groups"][0]["rules"]] == ["EnabledMetrics"]


async def test_render_all_empty_kind_leaves_dir_without_files(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """render_all with no enabled rules of a kind leaves that dir with no *.yaml."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="OnlyLogs",
        expr="_msg:error | stats count() as match_count | filter match_count:>0",
        expr_kind="logsql",
        severity="warning",
        summary="Only logs",
    )
    logs_dir = tmp_path / "logs"
    metrics_dir = tmp_path / "metrics"
    await render_all(user_repo, logs_dir, metrics_dir)
    assert (logs_dir / "OnlyLogs.yaml").exists()
    # metrics_dir exists (mkdir) but has no rule files.
    assert metrics_dir.is_dir()
    assert list(metrics_dir.glob("*.yaml")) == []


async def test_render_all_delete_then_re_render_removes_file(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """render_all unlinks a rule's file once the rule is deleted (orphan reconcile)."""
    user_repo = LogUserRulesRepository(repo)
    rule = await user_repo.create(
        rule_name="Ephemeral",
        expr="_msg:error | stats count() as match_count | filter match_count:>0",
        expr_kind="logsql",
        severity="warning",
        summary="Ephemeral",
    )
    logs_dir = tmp_path / "logs"
    metrics_dir = tmp_path / "metrics"
    await render_all(user_repo, logs_dir, metrics_dir)
    assert (logs_dir / "Ephemeral.yaml").exists()
    await user_repo.delete(rule.id)
    await render_all(user_repo, logs_dir, metrics_dir)
    assert not (logs_dir / "Ephemeral.yaml").exists()
    assert list(logs_dir.glob("*.yaml")) == []


async def test_render_all_removes_orphan_and_old_aggregate_files(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    """render_all unlinks pre-existing *.yaml that are not desired rule files.

    Covers the migration case: a stale aggregate `logs.yaml` from the previous
    scheme (and an orphan from a renamed rule) is removed on re-render.
    """
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="Keeper",
        expr="_msg:error | stats count() as match_count | filter match_count:>0",
        expr_kind="logsql",
        severity="warning",
        summary="Keeper",
    )
    logs_dir = tmp_path / "logs"
    metrics_dir = tmp_path / "metrics"
    logs_dir.mkdir(parents=True)
    # Simulate leftovers from the old aggregate scheme + a renamed rule.
    (logs_dir / "logs.yaml").write_text("groups: []\n")
    (logs_dir / "OldName.yaml").write_text("groups: []\n")
    await render_all(user_repo, logs_dir, metrics_dir)
    assert (logs_dir / "Keeper.yaml").exists()
    assert not (logs_dir / "logs.yaml").exists()
    assert not (logs_dir / "OldName.yaml").exists()


async def test_render_all_returns_true_on_success(repo: SqliteRepository, tmp_path: Path) -> None:
    """render_all returns True when all per-rule files are written successfully."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="LogsRule",
        expr="_msg:test | stats count() as match_count | filter match_count:>0",
        expr_kind="logsql",
        severity="warning",
        summary="Test rule",
    )
    logs_dir = tmp_path / "logs"
    metrics_dir = tmp_path / "metrics"
    result = await render_all(user_repo, logs_dir, metrics_dir)
    assert result is True


def test_atomic_write_rejects_non_yaml_extension() -> None:
    """_atomic_write rejects paths not ending in .yaml."""
    log = structlog.get_logger()
    with pytest.raises(ValueError, match=r"must end in \.yaml"):
        _atomic_write(Path("rules.yml"), "groups: []\n", log)  # pyright: ignore[reportPrivateUsage]


async def test_render_all_round_trip_semantics(repo: SqliteRepository, tmp_path: Path) -> None:
    """Round-trip: create rule -> render_all -> parse per-rule file -> verify alert."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="RoundTripAlert",
        expr="_msg:test | stats count() as match_count | filter match_count:>0",
        expr_kind="logsql",
        severity="critical",
        summary="Round trip test",
        description="Testing round-trip",
    )
    logs_dir = tmp_path / "logs"
    metrics_dir = tmp_path / "metrics"
    await render_all(user_repo, logs_dir, metrics_dir)
    doc = yaml.safe_load((logs_dir / "RoundTripAlert.yaml").read_text())
    assert len(doc["groups"]) == 1
    rendered_alert = doc["groups"][0]["rules"][0]
    assert rendered_alert["alert"] == "RoundTripAlert"
    assert rendered_alert["labels"]["severity"] == "critical"


def test_render_dirs_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """render_dirs_from_env with no env set returns the default dirs."""
    monkeypatch.delenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", raising=False)
    monkeypatch.delenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", raising=False)
    logs_dir, metrics_dir = render_dirs_from_env()
    assert str(logs_dir) == "/var/vmalert-user-logs"
    assert str(metrics_dir) == "/var/vmalert-user-metrics"


def test_render_dirs_from_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """render_dirs_from_env with env overrides returns the custom dirs."""
    logs_dir = tmp_path / "custom-logs"
    metrics_dir = tmp_path / "custom-metrics"
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", str(logs_dir))
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", str(metrics_dir))
    got_logs, got_metrics = render_dirs_from_env()
    assert str(got_logs) == str(logs_dir)
    assert str(got_metrics) == str(metrics_dir)


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


def test_render_kind_dir_skips_unsafe_rule_name(tmp_path: Path) -> None:
    """_render_kind_dir skips a rule whose name is not a safe identifier and returns False."""
    log = structlog.get_logger()
    bad = _rule("../escape")  # not matched by the identifier guard
    ok = _render_kind_dir(tmp_path, [bad], log)  # pyright: ignore[reportPrivateUsage]
    assert ok is False
    # No file was written under the dir (and no traversal escaped it).
    assert list(tmp_path.glob("*.yaml")) == []


def test_render_kind_dir_unlink_failure_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_render_kind_dir returns False when unlinking an orphan raises OSError."""
    log = structlog.get_logger()
    orphan = tmp_path / "Stale.yaml"
    orphan.write_text("groups: []\n")

    def _boom(self: Path) -> None:
        raise OSError("unlink denied")

    monkeypatch.setattr(Path, "unlink", _boom)
    # No desired rules -> the orphan is targeted for removal -> unlink raises.
    ok = _render_kind_dir(tmp_path, [], log)  # pyright: ignore[reportPrivateUsage]
    assert ok is False


def test_render_kind_dir_write_failure_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_render_kind_dir returns False when _atomic_write raises OSError for a rule."""
    log = structlog.get_logger()

    def _boom(output_path: Path, content: str, log: object) -> None:
        raise OSError("write denied")

    monkeypatch.setattr(_user_rules_render_mod, "_atomic_write", _boom)
    rule = _rule("WriteFailRule")
    ok = _render_kind_dir(tmp_path, [rule], log)  # pyright: ignore[reportPrivateUsage]
    assert ok is False


def test_render_kind_dir_mkdir_failure_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_render_kind_dir returns False immediately when out_dir.mkdir raises OSError."""
    log = structlog.get_logger()
    unwritable = tmp_path / "no_perms"
    original_mkdir = Path.mkdir

    def _boom(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if self == unwritable:
            raise OSError("mkdir denied")
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", _boom)
    rule = _rule("SomeRule")
    ok = _render_kind_dir(unwritable, [rule], log)  # pyright: ignore[reportPrivateUsage]
    assert ok is False


__all__: list[str] = []
