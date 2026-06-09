"""Heuristic LogsQL/MetricsQL expr validator (STAGE-004-043).

PURE Python — NO docker/vmalert/network. Identical behavior in dev/test/CI/prod.
CONSERVATIVE: only rejects patterns RIG-CONFIRMED to make vmalert reject a user
rule (one bad per-rule YAML file blocks the whole user-rules glob). False
negatives are tolerated (vmalert + STAGE-043A's exact dry-run catch them later);
false positives (blocking a valid rule) are the worse failure and are avoided.

The full exact-parser vmalert dry-run is deferred to STAGE-043A. This module is a
fast structural pre-filter only.
"""

from __future__ import annotations

import re
from typing import Final

from homelab_monitor.kernel.logs.user_rules_repo import UserRuleValidationError


class ExprValidationError(UserRuleValidationError):
    """Raised when a user-rule expr fails the heuristic loadability checks.

    Subclass of UserRuleValidationError so existing ``except
    UserRuleValidationError`` blocks still catch it, but the router catches THIS
    first to return code ``invalid_expr`` (vs ``invalid_rule`` for field errors).

    ``check`` names the heuristic that failed (machine-readable, goes into the
    400 ``details``); the message is user-readable.
    """

    def __init__(self, message: str, *, check: str) -> None:
        self.check = check
        super().__init__(message)


# A `| stats` pipe (case-insensitive, optional spaces after `|`). vmalert vlogs
# ALERTING requires a stats pipe to produce a numeric value; a bare filter expr
# loads as health=err (rig-proven).
_STATS_PIPE_RE: Final[re.Pattern[str]] = re.compile(r"\|\s*stats\b", re.IGNORECASE)

# `| filter <first-token>...` — capture the field/word immediately after a
# `| filter` pipe. The first token is the thing being compared; if it is a bare
# reserved LogsQL stats-function / pipe keyword (rather than a stats OUTPUT
# alias), vmalert rejects the rule (rig-confirmed: `| filter count:>N` fails;
# `| filter match_count:>N` is fine). Token = leading run of [A-Za-z0-9_].
_FILTER_PIPE_RE: Final[re.Pattern[str]] = re.compile(
    r"\|\s*filter\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)

# Reserved LogsQL words that must NOT be a bare `| filter` field. CONSERVATIVE,
# definitely-reserved set only. Rationale per word below; uncertain words are
# EXCLUDED (false negative is acceptable, false positive is not).
#
# Stats pipe function names (https://docs.victoriametrics.com/victorialogs/logsql/#stats-pipe-functions):
#   count, count_uniq, sum, avg, min, max, median, quantile, uniq_values,
#   values, row_min, row_max, row_any, histogram, count_empty, sum_len, rate.
# Pipe / structural keywords that are also reserved at the stats-output position:
#   stats, filter, by, sort, limit, offset, fields, format, unpack_json,
#   unpack_logfmt, extract, math, top, uniq.
# The built-in rules deliberately use NON-reserved aliases (oom_count, fail_count,
# crit_count, match_count) precisely to dodge this — those MUST pass.
#
# The single rig-confirmed failure is `count`. The rest are included because they
# are unambiguously reserved LogsQL identifiers (stats funcs / pipe keywords) and
# a user filtering on a BARE one of them (not an alias suffix like `count_*`) is
# almost certainly a mistake. We match the WHOLE first token only (anchored by the
# regex capture), so `match_count`, `oom_count`, `fail_count`, `crit_count`,
# `count_total` (alias containing a reserved substring) are NOT flagged.
_RESERVED_FILTER_WORDS: Final[frozenset[str]] = frozenset(
    {
        # stats pipe function names
        "count",
        "count_uniq",
        "sum",
        "avg",
        "min",
        "max",
        "median",
        "quantile",
        "uniq",
        "uniq_values",
        "values",
        "rate",
        "histogram",
        # pipe / structural keywords
        "stats",
        "filter",
        "by",
        "sort",
        "limit",
        "offset",
        "fields",
        "format",
        "math",
        "top",
    }
)


def _count_unescaped_double_quotes(expr: str) -> int:
    """Count `"` chars not immediately preceded by a backslash.

    A simple left-to-right scan: a `"` preceded by an ODD run of backslashes is
    escaped (skip); otherwise it counts. Even count = balanced; odd = a dangling
    quote that breaks parsing (rig case: a truncated `_msg:"..."`).
    """
    count = 0
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch == "\\":
            # Skip the escaped char (whatever it is).
            i += 2
            continue
        if ch == '"':
            count += 1
        i += 1
    return count


def _parens_balanced_outside_quotes(expr: str) -> bool:
    """True iff `(`/`)` are balanced, ignoring parens inside double-quoted spans.

    Quote-aware: a `"` toggles in/out of a quoted span (respecting `\\"` escapes);
    parens inside a quoted span are literal text and ignored. Returns False on any
    unbalanced state (close before open, or leftover opens). If quotes are
    themselves unbalanced this still returns a deterministic result, but the
    unbalanced-quote check (run first) is the authoritative signal there.
    """
    depth = 0
    in_quote = False
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            in_quote = not in_quote
            i += 1
            continue
        if not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    return False
        i += 1
    return depth == 0


def _check_balanced_quotes(expr: str) -> None:
    """Reject an odd number of unescaped double-quotes (dangling quote)."""
    if _count_unescaped_double_quotes(expr) % 2 != 0:
        raise ExprValidationError(
            'Expression has an unbalanced double-quote ("). '
            "Every opening quote needs a closing quote.",
            check="unbalanced_quotes",
        )


def _check_balanced_parens(expr: str) -> None:
    """Reject unbalanced parentheses outside quoted spans."""
    if not _parens_balanced_outside_quotes(expr):
        raise ExprValidationError(
            "Expression has unbalanced parentheses ( ).",
            check="unbalanced_parens",
        )


def _check_logsql_has_stats(expr: str) -> None:
    """Reject a logsql alerting expr with no `| stats` pipe (unloadable)."""
    if not _STATS_PIPE_RE.search(expr):
        raise ExprValidationError(
            "Log alert expressions need a `| stats ... | filter ...` pipe to "
            "produce a numeric value (a bare filter cannot fire an alert).",
            check="missing_stats_pipe",
        )


def _check_filter_field_not_reserved(expr: str) -> None:
    """Reject a `| filter <reserved-word>...` where the field is a bare reserved
    LogsQL stats/pipe keyword (rig-confirmed: `| filter count:>N` is rejected).

    Checks EVERY `| filter` clause in the expr. The comparison is on the WHOLE
    first token (case-insensitive); aliases like `match_count` / `oom_count` are
    NOT flagged.
    """
    for match in _FILTER_PIPE_RE.finditer(expr):
        field = match.group(1)
        if field.lower() in _RESERVED_FILTER_WORDS:
            raise ExprValidationError(
                f"`| filter {field}:...` uses the reserved LogsQL word "
                f"'{field}' as a field. Filter on the stats output alias "
                f"instead (e.g. `count() as match_count | filter match_count:>0`).",
                check="reserved_filter_field",
            )


def _validate_logsql(expr: str) -> None:
    """Run the logsql check order: quotes → parens → stats → filter-field."""
    _check_balanced_quotes(expr)
    _check_balanced_parens(expr)
    _check_logsql_has_stats(expr)
    _check_filter_field_not_reserved(expr)


def _validate_metricsql(expr: str) -> None:
    """metricsql: balanced quotes + parens only (best-effort minimal)."""
    _check_balanced_quotes(expr)
    _check_balanced_parens(expr)


def validate_expr(expr: str, expr_kind: str) -> None:
    """Heuristically validate an expr for vmalert loadability.

    Raises ExprValidationError on a RIG-CONFIRMED-invalid pattern. expr_kind is
    'logsql' or 'metricsql'. Assumes expr is already non-empty + length/control
    -char checked by _validate_fields (this runs AFTER those). For an UNKNOWN
    expr_kind, returns without checks (expr_kind validity is _validate_fields's
    job and raises there first).
    """
    if expr_kind == "logsql":
        _validate_logsql(expr)
    elif expr_kind == "metricsql":
        _validate_metricsql(expr)
    # else: unknown kind — _validate_fields already raised; nothing to do.


__all__ = [
    "ExprValidationError",
    "validate_expr",
]
