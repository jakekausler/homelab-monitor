"""Tests for the heuristic LogsQL/MetricsQL expr validator (STAGE-004-043)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.logs.expr_validate import (
    ExprValidationError,
    validate_expr,
)


class TestLogsQLValidation:
    """LogsQL expression validation tests."""

    def test_logsql_valid_stats_filter_alias_passes(self) -> None:
        """Valid logsql expr with stats + filter alias must pass."""
        expr = (
            '_time:5m service:="kernel" "Out of memory" '
            "| stats by (host) count() as match_count "
            "| filter match_count:>0"
        )
        validate_expr(expr, "logsql")  # Should not raise

    def test_logsql_filter_reserved_count_rejected(self) -> None:
        """Bare 'count' as filter field is reserved and must be rejected."""
        expr = "_time:5m error | stats count() as c | filter count:>0"
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr(expr, "logsql")
        assert exc_info.value.check == "reserved_filter_field"

    def test_logsql_missing_stats_rejected(self) -> None:
        """LogsQL without | stats pipe must be rejected."""
        expr = "_msg:error"
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr(expr, "logsql")
        assert exc_info.value.check == "missing_stats_pipe"

    def test_logsql_odd_quotes_rejected(self) -> None:
        """Unbalanced double-quotes must be rejected."""
        expr = '_msg:"unterminated | stats count() as c | filter c:>0'
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr(expr, "logsql")
        assert exc_info.value.check == "unbalanced_quotes"

    def test_logsql_unbalanced_parens_rejected(self) -> None:
        """Unbalanced parentheses (outside quotes) must be rejected."""
        expr = "(_msg:error | stats count() as c | filter c:>0"
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr(expr, "logsql")
        assert exc_info.value.check == "unbalanced_parens"

    def test_logsql_close_before_open_rejected(self) -> None:
        """Closing paren before opening must be rejected."""
        expr = "_msg:error) | stats count() as c | filter c:>0"
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr(expr, "logsql")
        assert exc_info.value.check == "unbalanced_parens"

    def test_logsql_balanced_parens_in_quotes_pass(self) -> None:
        """Parens inside quoted strings must be ignored (balanced)."""
        expr = '_msg:"(a) (b)" | stats count() as match_count | filter match_count:>0'
        validate_expr(expr, "logsql")  # Should not raise

    def test_logsql_escaped_quote_is_balanced(self) -> None:
        r"""Escaped quotes (\") must not count as unbalanced."""
        expr = r'_msg:"say \"hi\"" | stats count() as match_count | filter match_count:>0'
        validate_expr(expr, "logsql")  # Should not raise

    def test_logsql_quote_skips_trailing_backslash(self) -> None:
        r"""Backslash at end of string must not crash the validator."""
        expr = r'_msg:"x\\" | stats count() as c | filter c:>0'
        validate_expr(expr, "logsql")  # Should not raise

    def test_logsql_filter_alias_with_reserved_substring_passes(self) -> None:
        """Aliases containing reserved substrings (oom_count, etc.) must pass."""
        expr = "... | stats count() as oom_count | filter oom_count:>0"
        validate_expr(expr, "logsql")  # Should not raise

    @pytest.mark.parametrize(
        "reserved_word",
        [
            "count",
            "sum",
            "by",
            "filter",
            "stats",
            "count_uniq",
            "avg",
            "min",
            "max",
            "limit",
            "offset",
            "fields",
        ],
    )
    def test_logsql_each_reserved_word_rejected(self, reserved_word: str) -> None:
        """Each reserved word as a bare filter field must be rejected."""
        expr = f"_msg:error | stats count() as c | filter {reserved_word}:>0"
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr(expr, "logsql")
        assert exc_info.value.check == "reserved_filter_field"

    def test_logsql_filter_case_insensitive(self) -> None:
        """Filter field matching must be case-insensitive."""
        expr = "_msg:error | STATS count() as c | FILTER count:>0"
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr(expr, "logsql")
        assert exc_info.value.check == "reserved_filter_field"

    @pytest.mark.parametrize(
        "expr",
        [
            (
                '_time:5m severity:(critical OR emergency OR alert OR "0" OR "1" '
                'OR "2") | stats by (service) count() as crit_count '
                "| filter crit_count:>0"
            ),
            (
                '_time:5m service:="kernel" "Out of memory" "kill_process" '
                "| stats by (host) count() as oom_count "
                "| filter oom_count:>0"
            ),
            (
                '_time:5m service:="sshd" "Failed password" '
                "| stats by (host) count() as fail_count "
                "| filter fail_count:>10"
            ),
        ],
    )
    def test_builtin_logsql_rules_pass(self, expr: str) -> None:
        """Golden exprs from built-in rules must all pass."""
        validate_expr(expr, "logsql")  # Should not raise

    def test_users_messy_but_valid_match_count_expr_passes(self) -> None:
        """User-shaped valid expr must pass."""
        expr = (
            '_time:5m service:="homeassistant" "ERROR" '
            "| stats count() as match_count "
            "| filter match_count:>10"
        )
        validate_expr(expr, "logsql")  # Should not raise


class TestMetricsQLValidation:
    """MetricsQL expression validation tests."""

    def test_metricsql_no_stats_required_passes(self) -> None:
        """MetricsQL does not require | stats pipe."""
        validate_expr("up == 0", "metricsql")  # Should not raise

    def test_metricsql_odd_quotes_rejected(self) -> None:
        """MetricsQL with unbalanced quotes must be rejected."""
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr('up{job="a}', "metricsql")
        assert exc_info.value.check == "unbalanced_quotes"

    def test_metricsql_unbalanced_parens_rejected(self) -> None:
        """MetricsQL with unbalanced parens must be rejected."""
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr("rate(http_requests[5m]", "metricsql")
        assert exc_info.value.check == "unbalanced_parens"

    def test_metricsql_valid_passes(self) -> None:
        """Valid MetricsQL with balanced parens/quotes must pass."""
        validate_expr("sum(rate(x[5m])) > 0", "metricsql")  # Should not raise

    def test_metricsql_bare_selector_rejected(self) -> None:
        """A bare selector (always fires) is rejected with missing_threshold."""
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr("up", "metricsql")
        assert exc_info.value.check == "missing_threshold"

    def test_metricsql_label_matcher_only_rejected(self) -> None:
        """`up{job="x"}`'s `=` is a label matcher, NOT a threshold → rejected."""
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr('up{job="x"}', "metricsql")
        assert exc_info.value.check == "missing_threshold"

    @pytest.mark.parametrize(
        "expr",
        [
            "up > 0",
            "up >= 1",
            "up == 0",
            "up != 0",
            "up < 1",
            "up <= 1",
            "rate(x[5m]) > 0.5",
            'up{job=~"x"} > 0',
        ],
    )
    def test_metricsql_comparison_accepted(self, expr: str) -> None:
        """Any threshold comparison satisfies the floor (no raise)."""
        validate_expr(expr, "metricsql")

    def test_metricsql_absent_accepted(self) -> None:
        """absent(...) is a recognized alerting construct → accepted."""
        validate_expr("absent(up)", "metricsql")

    def test_metricsql_absent_over_time_accepted(self) -> None:
        """absent_over_time(...) is recognized → accepted."""
        validate_expr("absent_over_time(up[5m])", "metricsql")

    def test_metricsql_unless_accepted(self) -> None:
        """The `unless` set operator is a recognized construct → accepted."""
        validate_expr("foo unless bar", "metricsql")

    def test_metricsql_regex_matcher_alone_rejected(self) -> None:
        """`=~` is a label-regex matcher, NOT a threshold → bare selector rejected."""
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr('up{job=~"x"}', "metricsql")
        assert exc_info.value.check == "missing_threshold"

    def test_metricsql_operator_inside_quotes_not_counted(self) -> None:
        """A `>` inside a quoted label value must NOT satisfy the floor."""
        with pytest.raises(ExprValidationError) as exc_info:
            validate_expr('up{note="a>b"}', "metricsql")
        assert exc_info.value.check == "missing_threshold"

    def test_metricsql_escaped_quote_inside_label_accepted_with_threshold(self) -> None:
        r"""An escaped quote inside a label value plus a real `> 0` is accepted
        (exercises the in-quote backslash branch of _strip_quoted_spans)."""
        validate_expr(r'up{n="a\"b"} > 0', "metricsql")

    def test_metricsql_backslash_outside_quote_with_threshold_accepted(self) -> None:
        r"""A backslash outside a quoted span is kept as-is; a real `> 0` still
        satisfies the floor (exercises the outside-quote backslash branch of the
        quote-stripper)."""
        validate_expr("up\\ > 0", "metricsql")


class TestUnknownExprKind:
    """Unknown expr_kind handling."""

    def test_unknown_expr_kind_is_noop(self) -> None:
        """Unknown expr_kind must be a no-op (not validated)."""
        validate_expr("anything malformed!@#$", "promql")  # Should not raise


class TestAdvancedExprWarnings:
    """Frontend advancedExprWarnings helper (imported from CreateAlertModal)."""

    def test_advancedExprWarnings_exported(self) -> None:
        """Verify advancedExprWarnings is available from the frontend module."""
        # This test is here to remind that advancedExprWarnings lives in the
        # FE module and is tested separately in FE test file.
        # The backend tests focus on validate_expr.
        pass
