"""Repository for the log_user_rules table (STAGE-004-042).

User-authored vmalert rules. One row = one alert rule. expr_kind partitions rows
into the two aggregate render targets (logs.yaml for 'logsql', metrics.yaml for
'metricsql'). rule_name is UNIQUE (it is the rendered `alert:` name). created_at/
updated_at are ISO-8601 UTC TEXT (utc_now_iso). source_kind/source_ref are
source-tracking fields populated by STAGE-043/044 (always 'manual'/NULL in v1).

Validation (D-VALIDATION-BEFORE-PERSIST): create/update validate field shapes AND
render the single rule via user_rules_render.render_yaml([rule]); if rendering
raises, the persist is rejected (UserRuleValidationError) so a row that cannot be
rendered never reaches the DB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.logs.vmalert_dryrun import DryRunRunner

#: Valid vmalert/Prometheus alertname identifier.
_RULE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
#: vmalert compound duration (one or more unit segments), OR the literal '0s'.
_DURATION_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+[smhd])+$|^0s$")
#: Chars that break a double-quoted PromQL/YAML inline value (quote, backslash,
#: ASCII control). Reused to guard label-bound values (rule_name, severity).
_FORBIDDEN_INLINE: Final[re.Pattern[str]] = re.compile(r'["\\]|[\x00-\x1f\x7f]')
#: Control chars to reject even in block scalars (summary/description) — newlines
#: are allowed in block scalars but other C0 controls + DEL are not.
_FORBIDDEN_BLOCK: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

VALID_EXPR_KINDS: Final[frozenset[str]] = frozenset({"logsql", "metricsql"})
VALID_SEVERITIES: Final[frozenset[str]] = frozenset({"info", "warning", "error", "critical"})

_MAX_RULE_NAME = 200
_MAX_EXPR = 8192
_MAX_SUMMARY = 1000
_MAX_DESCRIPTION = 4000


class UserRuleValidationError(ValueError):
    """Raised when a user rule fails validation or cannot be rendered."""


class DuplicateRuleNameError(Exception):
    """Raised when a rule_name collides with an existing row."""


@dataclass(frozen=True, slots=True)
class LogUserRule:
    id: int
    rule_name: str
    expr: str
    expr_kind: str
    severity: str
    summary: str
    description: str
    for_duration: str
    source_kind: str
    source_ref: str | None
    enabled: bool
    created_at: str
    updated_at: str


def _validate_text_length_and_control(
    field_name: str, value: str, max_len: int, allow_newlines: bool = False
) -> None:
    """Validate field length and forbidden control characters.

    Raises UserRuleValidationError if the value exceeds max_len or contains
    forbidden control characters. If allow_newlines=True, newlines are allowed
    (used for block scalars like summary/description).
    """
    if len(value) > max_len:
        msg = f"{field_name} too long (max {max_len})"
        raise UserRuleValidationError(msg)
    forbidden_pattern = _FORBIDDEN_BLOCK if allow_newlines else _FORBIDDEN_INLINE
    if forbidden_pattern.search(value):
        char_type = "control character" if allow_newlines else "character"
        msg = f"{field_name} contains a forbidden {char_type}"
        raise UserRuleValidationError(msg)


def _validate_fields(  # noqa: PLR0913
    *,
    rule_name: str,
    expr: str,
    expr_kind: str,
    severity: str,
    summary: str,
    description: str,
    for_duration: str,
) -> None:
    """Validate a user rule's fields. Raises UserRuleValidationError on any failure.

    Checks: rule_name identifier-shape + length + no forbidden inline chars;
    expr non-empty after strip + length; expr_kind in VALID_EXPR_KINDS; severity
    non-empty + in VALID_SEVERITIES + no forbidden inline chars; for_duration
    matches a simple duration or '0s'; summary/description length + no forbidden
    control chars (block scalars allow newlines).
    """
    name = rule_name
    if not _RULE_NAME_RE.match(name):
        msg = f"rule_name must match {_RULE_NAME_RE.pattern!r}, got {name!r}"
        raise UserRuleValidationError(msg)
    _validate_text_length_and_control("rule_name", name, _MAX_RULE_NAME)
    if not expr.strip():
        msg = "expr must be non-empty"
        raise UserRuleValidationError(msg)
    _validate_text_length_and_control("expr", expr, _MAX_EXPR, allow_newlines=True)
    if expr_kind not in VALID_EXPR_KINDS:
        msg = f"expr_kind must be one of {sorted(VALID_EXPR_KINDS)}, got {expr_kind!r}"
        raise UserRuleValidationError(msg)
    if not severity.strip():
        msg = "severity must be non-empty"
        raise UserRuleValidationError(msg)
    if severity not in VALID_SEVERITIES:
        msg = f"severity must be one of {sorted(VALID_SEVERITIES)}, got {severity!r}"
        raise UserRuleValidationError(msg)
    if not _DURATION_RE.match(for_duration):
        msg = f"for_duration must match {_DURATION_RE.pattern!r}, got {for_duration!r}"
        raise UserRuleValidationError(msg)
    _validate_text_length_and_control("summary", summary, _MAX_SUMMARY, allow_newlines=True)
    _validate_text_length_and_control(
        "description", description, _MAX_DESCRIPTION, allow_newlines=True
    )


def _validate_and_render_check(
    rule: LogUserRule, dryrun_runner: DryRunRunner | None = None
) -> None:
    """Validate fields, heuristically validate the expr, ensure the single rule
    renders, AND (optionally) run the vmalert exact-parser dry-run.

    Order: field-shape validation -> heuristic expr loadability check
    (ExprValidationError -> router 400 invalid_expr) -> render check -> (if a
    dryrun_runner is injected) vmalert -dryRun exact-parser check. The dry-run is
    fail-open: skipped results are ignored; only a definitive ok=False raises.
    """
    _validate_fields(
        rule_name=rule.rule_name,
        expr=rule.expr,
        expr_kind=rule.expr_kind,
        severity=rule.severity,
        summary=rule.summary,
        description=rule.description,
        for_duration=rule.for_duration,
    )

    # Heuristic expr loadability pre-filter (STAGE-004-043). Lazy import to avoid
    # a module-level cycle: expr_validate imports UserRuleValidationError from us.
    from homelab_monitor.kernel.logs.expr_validate import (  # noqa: PLC0415
        ExprValidationError,
        validate_expr,
    )

    validate_expr(rule.expr, rule.expr_kind)

    from homelab_monitor.kernel.logs.user_rules_render import render_yaml  # noqa: PLC0415

    try:
        rendered = render_yaml([rule])
    except ValueError as exc:  # pragma: no cover -- field validation catches all known cases
        msg = f"rule failed to render: {exc}"
        raise UserRuleValidationError(msg) from exc

    if dryrun_runner is not None:
        result = dryrun_runner(rendered)
        if not result.skipped and not result.ok:
            detail = result.stderr or "vmalert rejected the rule expression"
            raise ExprValidationError(detail, check="dryrun")


class LogUserRulesRepository:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    async def create(  # noqa: PLR0913
        self,
        *,
        rule_name: str,
        expr: str,
        expr_kind: str,
        severity: str,
        summary: str,
        description: str = "",
        for_duration: str = "0s",
        source_kind: str = "manual",
        source_ref: str | None = None,
        enabled: bool = True,
        dryrun_runner: DryRunRunner | None = None,
    ) -> LogUserRule:
        now = utc_now_iso()
        candidate = LogUserRule(
            id=0,
            rule_name=rule_name,
            expr=expr,
            expr_kind=expr_kind,
            severity=severity,
            summary=summary,
            description=description,
            for_duration=for_duration,
            source_kind=source_kind,
            source_ref=source_ref,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        _validate_and_render_check(candidate, dryrun_runner)
        existing = await self.get_by_name(rule_name)
        if existing is not None:
            msg = f"rule_name already exists: {rule_name}"
            raise DuplicateRuleNameError(msg)
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "INSERT INTO log_user_rules "
                    "  (rule_name, expr, expr_kind, severity, summary, description, "
                    "   for_duration, source_kind, source_ref, enabled, created_at, updated_at) "
                    "VALUES (:name, :expr, :kind, :sev, :sum, :desc, :for_d, "
                    "        :sk, :sr, :en, :now, :now)"
                ),
                {
                    "name": rule_name,
                    "expr": expr,
                    "kind": expr_kind,
                    "sev": severity,
                    "sum": summary,
                    "desc": description,
                    "for_d": for_duration,
                    "sk": source_kind,
                    "sr": source_ref,
                    "en": 1 if enabled else 0,
                    "now": now,
                },
            )
            new_id = int(result.lastrowid)
        created = await self.get(new_id)
        if created is None:  # pragma: no cover -- vanished after insert
            msg = f"user rule vanished after insert: id={new_id}"
            raise RuntimeError(msg)
        return created

    async def get(self, rule_id: int) -> LogUserRule | None:
        rows = await self._repo.fetch_all(
            text(f"SELECT {_COLUMNS} FROM log_user_rules WHERE id = :id"),
            {"id": rule_id},
        )
        return _row_to_rule(rows[0]) if rows else None

    async def get_by_name(self, rule_name: str) -> LogUserRule | None:
        rows = await self._repo.fetch_all(
            text(f"SELECT {_COLUMNS} FROM log_user_rules WHERE rule_name = :name"),
            {"name": rule_name},
        )
        return _row_to_rule(rows[0]) if rows else None

    async def list_all(self) -> list[LogUserRule]:
        rows = await self._repo.fetch_all(
            text(f"SELECT {_COLUMNS} FROM log_user_rules ORDER BY rule_name ASC")
        )
        return [_row_to_rule(r) for r in rows]

    async def list_enabled(self) -> list[LogUserRule]:
        rows = await self._repo.fetch_all(
            text(f"SELECT {_COLUMNS} FROM log_user_rules WHERE enabled = 1 ORDER BY rule_name ASC")
        )
        return [_row_to_rule(r) for r in rows]

    async def update(  # noqa: PLR0913
        self,
        rule_id: int,
        *,
        expr: str | None = None,
        severity: str | None = None,
        summary: str | None = None,
        description: str | None = None,
        for_duration: str | None = None,
        enabled: bool | None = None,
        dryrun_runner: DryRunRunner | None = None,
    ) -> LogUserRule | None:
        """Partial update. None args are left unchanged. Returns None if absent.

        rule_name and expr_kind are immutable (they bind identity + render target).
        Validates+render-checks the merged candidate before writing.
        """
        current = await self.get(rule_id)
        if current is None:
            return None
        merged = LogUserRule(
            id=current.id,
            rule_name=current.rule_name,
            expr=current.expr if expr is None else expr,
            expr_kind=current.expr_kind,
            severity=current.severity if severity is None else severity,
            summary=current.summary if summary is None else summary,
            description=current.description if description is None else description,
            for_duration=current.for_duration if for_duration is None else for_duration,
            source_kind=current.source_kind,
            source_ref=current.source_ref,
            enabled=current.enabled if enabled is None else enabled,
            created_at=current.created_at,
            updated_at=utc_now_iso(),
        )
        _validate_and_render_check(merged, dryrun_runner)
        async with self._repo.transaction() as conn:
            await conn.execute(
                text(
                    "UPDATE log_user_rules SET expr = :expr, severity = :sev, "
                    "  summary = :sum, description = :desc, for_duration = :for_d, "
                    "  enabled = :en, updated_at = :now WHERE id = :id"
                ),
                {
                    "expr": merged.expr,
                    "sev": merged.severity,
                    "sum": merged.summary,
                    "desc": merged.description,
                    "for_d": merged.for_duration,
                    "en": 1 if merged.enabled else 0,
                    "now": merged.updated_at,
                    "id": rule_id,
                },
            )
        return await self.get(rule_id)

    async def set_enabled(self, rule_id: int, *, enabled: bool) -> LogUserRule | None:
        """Flip the enabled flag. Returns None if absent (no validation needed)."""
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text("UPDATE log_user_rules SET enabled = :en, updated_at = :now WHERE id = :id"),
                {"en": 1 if enabled else 0, "now": utc_now_iso(), "id": rule_id},
            )
            if (result.rowcount or 0) == 0:
                return None
        return await self.get(rule_id)

    async def delete(self, rule_id: int) -> bool:
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text("DELETE FROM log_user_rules WHERE id = :id"),
                {"id": rule_id},
            )
            return (result.rowcount or 0) > 0


_COLUMNS = (
    "id, rule_name, expr, expr_kind, severity, summary, description, "
    "for_duration, source_kind, source_ref, enabled, created_at, updated_at"
)


def _row_to_rule(r: Any) -> LogUserRule:  # noqa: ANN401 -- SQLite Row
    raw_sr = r.source_ref  # pyright: ignore[reportAttributeAccessIssue]
    return LogUserRule(
        id=int(r.id),  # pyright: ignore[reportAttributeAccessIssue]
        rule_name=str(r.rule_name),  # pyright: ignore[reportAttributeAccessIssue]
        expr=str(r.expr),  # pyright: ignore[reportAttributeAccessIssue]
        expr_kind=str(r.expr_kind),  # pyright: ignore[reportAttributeAccessIssue]
        severity=str(r.severity),  # pyright: ignore[reportAttributeAccessIssue]
        summary=str(r.summary),  # pyright: ignore[reportAttributeAccessIssue]
        description=str(r.description),  # pyright: ignore[reportAttributeAccessIssue]
        for_duration=str(r.for_duration),  # pyright: ignore[reportAttributeAccessIssue]
        source_kind=str(r.source_kind),  # pyright: ignore[reportAttributeAccessIssue]
        source_ref=(None if raw_sr is None else str(raw_sr)),
        enabled=bool(r.enabled),  # pyright: ignore[reportAttributeAccessIssue]
        created_at=str(r.created_at),  # pyright: ignore[reportAttributeAccessIssue]
        updated_at=str(r.updated_at),  # pyright: ignore[reportAttributeAccessIssue]
    )


__all__ = [
    "VALID_EXPR_KINDS",
    "VALID_SEVERITIES",
    "DuplicateRuleNameError",
    "LogUserRule",
    "LogUserRulesRepository",
    "UserRuleValidationError",
]
