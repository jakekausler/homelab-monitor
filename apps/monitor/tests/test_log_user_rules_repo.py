"""Tests for ``kernel.logs.user_rules_repo``: repository CRUD, validation, rendering."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.user_rules_repo import (
    DuplicateRuleNameError,
    LogUserRulesRepository,
    UserRuleValidationError,
)


async def test_create_returns_row_with_defaults(repo: SqliteRepository) -> None:
    """create() returns a row with id>0, ISO timestamps, defaults."""
    user_repo = LogUserRulesRepository(repo)
    rule = await user_repo.create(
        rule_name="TestAlert",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="A test alert",
    )
    assert rule.id > 0
    assert rule.rule_name == "TestAlert"
    assert rule.expr == "_msg:error"
    assert rule.expr_kind == "logsql"
    assert rule.enabled is True
    assert rule.source_kind == "manual"
    assert rule.source_ref is None
    assert rule.description == ""
    assert rule.for_duration == "0s"
    assert "T" in rule.created_at  # ISO format
    assert "T" in rule.updated_at


async def test_create_with_metricsql_kind(repo: SqliteRepository) -> None:
    """create() round-trips expr_kind=metricsql."""
    user_repo = LogUserRulesRepository(repo)
    rule = await user_repo.create(
        rule_name="MetricsAlert",
        expr="up == 0",
        expr_kind="metricsql",
        severity="critical",
        summary="Host down",
    )
    assert rule.expr_kind == "metricsql"


async def test_get_by_name_finds_rule(repo: SqliteRepository) -> None:
    """get_by_name() finds a created rule."""
    user_repo = LogUserRulesRepository(repo)
    created = await user_repo.create(
        rule_name="FindMe",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="Find me",
    )
    found = await user_repo.get_by_name("FindMe")
    assert found is not None
    assert found.id == created.id
    assert found.rule_name == "FindMe"


async def test_get_by_id_finds_rule(repo: SqliteRepository) -> None:
    """get() by id finds the rule."""
    user_repo = LogUserRulesRepository(repo)
    created = await user_repo.create(
        rule_name="ById",
        expr="_msg:boom",
        expr_kind="logsql",
        severity="info",
        summary="By ID",
    )
    found = await user_repo.get(created.id)
    assert found is not None
    assert found.id == created.id


async def test_get_nonexistent_returns_none(repo: SqliteRepository) -> None:
    """get(9999) returns None when absent."""
    user_repo = LogUserRulesRepository(repo)
    found = await user_repo.get(9999)
    assert found is None


async def test_duplicate_rule_name_raises(repo: SqliteRepository) -> None:
    """create() with duplicate rule_name raises DuplicateRuleNameError."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="Duplicate",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="First",
    )
    with pytest.raises(DuplicateRuleNameError):
        await user_repo.create(
            rule_name="Duplicate",
            expr="_msg:different",
            expr_kind="logsql",
            severity="info",
            summary="Second",
        )


async def test_invalid_rule_name_raises(repo: SqliteRepository) -> None:
    """create() with invalid rule_name (not identifier) raises UserRuleValidationError."""
    user_repo = LogUserRulesRepository(repo)
    with pytest.raises(UserRuleValidationError):
        await user_repo.create(
            rule_name="1bad-name",  # Starts with digit
            expr="_msg:error",
            expr_kind="logsql",
            severity="warning",
            summary="Bad name",
        )


async def test_invalid_expr_kind_raises(repo: SqliteRepository) -> None:
    """create() with invalid expr_kind raises UserRuleValidationError."""
    user_repo = LogUserRulesRepository(repo)
    with pytest.raises(UserRuleValidationError):
        await user_repo.create(
            rule_name="BadKind",
            expr="_msg:error",
            expr_kind="promql",  # Invalid kind
            severity="warning",
            summary="Bad kind",
        )


async def test_empty_expr_raises(repo: SqliteRepository) -> None:
    """create() with empty/whitespace expr raises UserRuleValidationError."""
    user_repo = LogUserRulesRepository(repo)
    with pytest.raises(UserRuleValidationError):
        await user_repo.create(
            rule_name="EmptyExpr",
            expr="   ",  # Whitespace only
            expr_kind="logsql",
            severity="warning",
            summary="Empty expr",
        )


async def test_invalid_for_duration_raises(repo: SqliteRepository) -> None:
    """create() with invalid for_duration raises UserRuleValidationError."""
    user_repo = LogUserRulesRepository(repo)
    with pytest.raises(UserRuleValidationError):
        await user_repo.create(
            rule_name="BadDuration",
            expr="_msg:error",
            expr_kind="logsql",
            severity="warning",
            summary="Bad duration",
            for_duration="5x",  # Invalid duration format
        )


async def test_valid_durations_accepted(repo: SqliteRepository) -> None:
    """create() accepts '0s', '5m', '1h', '1d' durations."""
    user_repo = LogUserRulesRepository(repo)
    for i, duration in enumerate(["0s", "5m", "1h", "1d"]):
        rule = await user_repo.create(
            rule_name=f"Duration{i}_{duration}",
            expr="_msg:error",
            expr_kind="logsql",
            severity="warning",
            summary=f"Duration {duration}",
            for_duration=duration,
        )
        assert rule.for_duration == duration


async def test_invalid_severity_raises(repo: SqliteRepository) -> None:
    """create() with severity not in {info, warning, critical} raises UserRuleValidationError."""
    user_repo = LogUserRulesRepository(repo)
    with pytest.raises(UserRuleValidationError):
        await user_repo.create(
            rule_name="BadSeverity",
            expr="_msg:error",
            expr_kind="logsql",
            severity="debug",  # Invalid severity
            summary="Bad severity",
        )


async def test_forbidden_control_char_in_summary_raises(repo: SqliteRepository) -> None:
    """create() with control char in summary raises UserRuleValidationError."""
    user_repo = LogUserRulesRepository(repo)
    with pytest.raises(UserRuleValidationError):
        await user_repo.create(
            rule_name="BadSummary",
            expr="_msg:error",
            expr_kind="logsql",
            severity="warning",
            summary="Bad\x00summary",  # NUL control char
        )


async def test_list_all_orders_by_rule_name(repo: SqliteRepository) -> None:
    """list_all() returns rules sorted by rule_name ASC."""
    user_repo = LogUserRulesRepository(repo)
    for name in ["Zebra", "Alpha", "Beta"]:
        await user_repo.create(
            rule_name=name,
            expr="_msg:error",
            expr_kind="logsql",
            severity="warning",
            summary=f"{name} alert",
        )
    rules = await user_repo.list_all()
    names = [r.rule_name for r in rules]
    assert names == ["Alpha", "Beta", "Zebra"]


async def test_list_enabled_excludes_disabled(repo: SqliteRepository) -> None:
    """list_enabled() excludes disabled rules."""
    user_repo = LogUserRulesRepository(repo)
    await user_repo.create(
        rule_name="Enabled",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="Enabled",
        enabled=True,
    )
    await user_repo.create(
        rule_name="Disabled",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="Disabled",
        enabled=False,
    )
    enabled_rules = await user_repo.list_enabled()
    assert len(enabled_rules) == 1
    assert enabled_rules[0].rule_name == "Enabled"


async def test_update_partial_changes_fields(repo: SqliteRepository) -> None:
    """update() with partial args changes only those fields, leaves others unchanged."""
    user_repo = LogUserRulesRepository(repo)
    created = await user_repo.create(
        rule_name="UpdateTest",
        expr="_msg:original",
        expr_kind="logsql",
        severity="warning",
        summary="Original",
    )
    original_created_at = created.created_at
    updated = await user_repo.update(created.id, expr="_msg:modified")
    assert updated is not None
    assert updated.expr == "_msg:modified"
    assert updated.rule_name == "UpdateTest"  # Unchanged
    assert updated.expr_kind == "logsql"  # Unchanged
    assert updated.severity == "warning"  # Unchanged
    assert updated.created_at == original_created_at  # Unchanged
    assert updated.updated_at > original_created_at  # Changed


async def test_update_nonexistent_returns_none(repo: SqliteRepository) -> None:
    """update(9999, ...) returns None when absent."""
    user_repo = LogUserRulesRepository(repo)
    result = await user_repo.update(9999, expr="_msg:new")
    assert result is None


async def test_update_with_invalid_value_raises(repo: SqliteRepository) -> None:
    """update() validates the merged candidate; invalid value raises UserRuleValidationError."""
    user_repo = LogUserRulesRepository(repo)
    rule = await user_repo.create(
        rule_name="UpdateValidation",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="Test",
    )
    with pytest.raises(UserRuleValidationError):
        await user_repo.update(rule.id, for_duration="5x")  # Invalid duration


async def test_set_enabled_false_then_list_enabled_omits_it(repo: SqliteRepository) -> None:
    """set_enabled(id, enabled=False) disables; list_enabled() omits it."""
    user_repo = LogUserRulesRepository(repo)
    rule = await user_repo.create(
        rule_name="DisableTest",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="Test",
        enabled=True,
    )
    disabled = await user_repo.set_enabled(rule.id, enabled=False)
    assert disabled is not None
    assert disabled.enabled is False
    enabled_rules = await user_repo.list_enabled()
    assert all(r.rule_name != "DisableTest" for r in enabled_rules)


async def test_set_enabled_nonexistent_returns_none(repo: SqliteRepository) -> None:
    """set_enabled(9999, ...) returns None when absent."""
    user_repo = LogUserRulesRepository(repo)
    result = await user_repo.set_enabled(9999, enabled=True)
    assert result is None


async def test_delete_hit_returns_true_then_absent(repo: SqliteRepository) -> None:
    """delete(id) returns True when found; rule is then absent."""
    user_repo = LogUserRulesRepository(repo)
    rule = await user_repo.create(
        rule_name="DeleteTest",
        expr="_msg:error",
        expr_kind="logsql",
        severity="warning",
        summary="Test",
    )
    deleted = await user_repo.delete(rule.id)
    assert deleted is True
    found = await user_repo.get(rule.id)
    assert found is None


async def test_delete_nonexistent_returns_false(repo: SqliteRepository) -> None:
    """delete(9999) returns False when absent."""
    user_repo = LogUserRulesRepository(repo)
    deleted = await user_repo.delete(9999)
    assert deleted is False


async def test_rule_name_too_long_raises(repo: SqliteRepository) -> None:
    """create() with rule_name > 200 chars raises UserRuleValidationError."""
    user_repo = LogUserRulesRepository(repo)
    with pytest.raises(UserRuleValidationError, match="rule_name too long"):
        await user_repo.create(
            rule_name="x" * 201,
            expr="_msg:error",
            expr_kind="logsql",
            severity="warning",
            summary="Too long name",
        )


async def test_severity_empty_raises(repo: SqliteRepository) -> None:
    """create() with empty severity raises UserRuleValidationError."""
    user_repo = LogUserRulesRepository(repo)
    with pytest.raises(UserRuleValidationError, match="severity must be non-empty"):
        await user_repo.create(
            rule_name="EmptySeverity",
            expr="_msg:error",
            expr_kind="logsql",
            severity="",
            summary="Empty severity",
        )


async def test_expr_too_long_raises(repo: SqliteRepository) -> None:
    """create() with expr > 8192 chars raises UserRuleValidationError."""
    user_repo = LogUserRulesRepository(repo)
    with pytest.raises(UserRuleValidationError, match="expr too long"):
        await user_repo.create(
            rule_name="LongExpr",
            expr="_msg:error AND " + "x" * 8190,
            expr_kind="logsql",
            severity="warning",
            summary="Long expr",
        )


async def test_summary_too_long_raises(repo: SqliteRepository) -> None:
    """create() with summary > 1000 chars raises UserRuleValidationError."""
    user_repo = LogUserRulesRepository(repo)
    with pytest.raises(UserRuleValidationError, match="summary too long"):
        await user_repo.create(
            rule_name="LongSummary",
            expr="_msg:error",
            expr_kind="logsql",
            severity="warning",
            summary="x" * 1001,
        )


__all__: list[str] = []
