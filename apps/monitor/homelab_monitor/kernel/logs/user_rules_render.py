"""Render user-authored vmalert rules to aggregate YAML files (STAGE-004-042).

Two render targets, partitioned by expr_kind:
  - logsql   rules -> logs.yaml    (group name: user-rules-logs)
  - metricsql rules -> metrics.yaml (group name: user-rules-metrics)

`render_yaml(rules)` is PURE: string-in/string-out, no DB, no filesystem. It
assumes all `rules` share one expr_kind and emits a single `groups:` document
with one group. Rules are sorted by rule_name for deterministic output (round-trip
stability). An empty list yields a valid `groups: []` doc so a stale rule file is
reliably cleared.

`render_all(repo, logs_path, metrics_path)` reconciles BOTH files from
repo.list_enabled(): groups by expr_kind, renders each, and ATOMICALLY rewrites
both files (full rewrite => deletes/disables/renames are handled by rewrite, no
orphans). Mirrors kernel/cron/render.py's atomic write (tempfile.mkstemp in the
target dir -> write -> os.replace, chmod 0o640, group-own by CONFIG_GROUP_NAME).

vmalert globs *.yaml in its rule dir and picks up changes within
-configCheckInterval (30s). NO explicit reload (D3).
"""

from __future__ import annotations

import grp
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.cron.render import CONFIG_GROUP_NAME

if TYPE_CHECKING:
    from homelab_monitor.kernel.logs.user_rules_repo import (
        LogUserRule,
        LogUserRulesRepository,
    )

#: Inline (double-quoted / label) value guard: a quote/backslash/control char.
_FORBIDDEN_INLINE: Final[re.Pattern[str]] = re.compile(r'["\\]|[\x00-\x1f\x7f]')
#: Block-scalar guard: control chars except newline (\n=0x0a).
_FORBIDDEN_BLOCK: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Fixed group names per file (collision-safety / distinguishability).
_GROUP_LOGS: Final[str] = "user-rules-logs"
_GROUP_METRICS: Final[str] = "user-rules-metrics"
_GROUP_INTERVAL: Final[str] = "60s"

_EXPR_KIND_LOGS: Final[str] = "logsql"
_EXPR_KIND_METRICS: Final[str] = "metricsql"


def _indent_block(value: str, indent: str) -> str:
    """Indent every line of a YAML block-scalar body by `indent`.

    Trailing newline is stripped first; each line (incl. empty) gets the prefix.
    """
    lines = value.rstrip("\n").split("\n")
    return "\n".join(f"{indent}{ln}" if ln else indent.rstrip() for ln in lines)


def _render_one_rule(rule: LogUserRule) -> str:
    """Render a single rule's YAML block (8-space base indent for list item).

    expr/description are YAML literal block scalars (`|`); summary is also a block
    scalar (D: prefer block scalars to dodge quoting bugs). severity is an inline
    value (sanitized). rule_name is the alert name (identifier-validated upstream).
    """
    if _FORBIDDEN_INLINE.search(rule.rule_name):
        msg = f"rule_name contains a forbidden inline character: {rule.rule_name!r}"
        raise ValueError(msg)
    if _FORBIDDEN_INLINE.search(rule.severity):
        msg = f"severity contains a forbidden inline character: {rule.severity!r}"
        raise ValueError(msg)
    if _FORBIDDEN_BLOCK.search(rule.expr):
        msg = "expr contains a forbidden control character"
        raise ValueError(msg)
    if _FORBIDDEN_BLOCK.search(rule.summary):
        msg = "summary contains a forbidden control character"
        raise ValueError(msg)
    if _FORBIDDEN_BLOCK.search(rule.description):
        msg = "description contains a forbidden control character"
        raise ValueError(msg)

    expr_body = _indent_block(rule.expr, " " * 10)
    summary_body = _indent_block(rule.summary, " " * 12)
    description_body = _indent_block(rule.description, " " * 12)
    return (
        f"      - alert: {rule.rule_name}\n"
        f"        expr: |\n"
        f"{expr_body}\n"
        f"        for: {rule.for_duration}\n"
        f"        labels:\n"
        f"          severity: {rule.severity}\n"
        f"          source_tool: user\n"
        f"          category: user-rule\n"
        f"        annotations:\n"
        f"          summary: |\n"
        f"{summary_body}\n"
        f"          description: |\n"
        f"{description_body}\n"
    )


def render_yaml(rules: list[LogUserRule]) -> str:
    """Render one expr_kind's rules to a vmalert `groups:` YAML doc (PURE).

    All `rules` MUST share one expr_kind; the group name is derived from the FIRST
    rule's expr_kind (logsql -> user-rules-logs, metricsql -> user-rules-metrics).
    Empty list -> `groups: []\n` (valid, clears the file). Rules sorted by
    rule_name (deterministic). Raises ValueError if a value fails sanitization or
    if rules mix expr_kinds.

    A rendered file ALWAYS parses as valid YAML and round-trips:
    groups[0].rules[i] preserves alert/expr/for/labels/annotations.
    """
    if not rules:
        return "groups: []\n"
    kinds = {r.expr_kind for r in rules}
    if len(kinds) != 1:
        msg = f"render_yaml requires a single expr_kind, got {sorted(kinds)}"
        raise ValueError(msg)
    kind = next(iter(kinds))
    if kind == _EXPR_KIND_LOGS:
        group_name = _GROUP_LOGS
    elif kind == _EXPR_KIND_METRICS:
        group_name = _GROUP_METRICS
    else:
        msg = f"unknown expr_kind: {kind!r}"
        raise ValueError(msg)
    ordered = sorted(rules, key=lambda r: r.rule_name)
    body = "".join(_render_one_rule(r) for r in ordered)
    # Logs rules require a group-level `type: vlogs` so vmalert-logs queries
    # VictoriaLogs (LogsQL) instead of defaulting to prometheus and hitting VL's
    # unsupported /api/v1/query endpoint. Metrics rules omit type (default prometheus).
    type_line = "    type: vlogs\n" if kind == _EXPR_KIND_LOGS else ""
    return (
        f"groups:\n  - name: {group_name}\n{type_line}"
        f"    interval: {_GROUP_INTERVAL}\n    rules:\n{body}"
    )


def _atomic_write(output_path: Path, content: str, log: BoundLogger) -> None:
    """Atomically write `content` to output_path (mirrors kernel/cron/render.py).

    tempfile.mkstemp in the SAME dir -> fdopen write -> os.replace -> group-own
    by CONFIG_GROUP_NAME (KeyError/OSError-guarded) -> chmod 0o640.
    Raises ValueError if output_path does not end in .yaml. Raises OSError on
    write/replace failure (caller logs + swallows).
    """
    if output_path.suffix != ".yaml":
        msg = f"user-rules render target must end in .yaml, got {output_path}"
        raise ValueError(msg)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=output_path.name + ".", suffix=".tmp", dir=str(output_path.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, output_path)
        try:
            gid = grp.getgrnam(CONFIG_GROUP_NAME).gr_gid
        except KeyError:
            pass
        else:  # pragma: no cover -- requires amconfig group (prod/dev rig only)
            try:
                os.chown(output_path, -1, gid)  # pragma: no cover -- requires amconfig group
            except OSError as exc:  # pragma: no cover -- requires amconfig group
                log.warning(
                    "user_rules.render.chown_failed",
                    output_path=str(output_path),
                    target_gid=gid,
                    reason=str(exc),
                )
        os.chmod(output_path, 0o640)
    except OSError as exc:  # pragma: no cover -- filesystem errors are environment-specific
        log.error(
            "user_rules.render.write_failed",
            output_path=str(output_path),
            error=str(exc),
        )
        if tmp_name is not None:
            with suppress(OSError):  # pragma: no cover -- defensive
                os.unlink(tmp_name)
        raise
    log.info("user_rules.render.success", output_path=str(output_path), bytes=len(content))


async def render_all(
    repo: LogUserRulesRepository,
    logs_path: Path,
    metrics_path: Path,
) -> bool:
    """Reconcile BOTH aggregate YAML files from repo.list_enabled().

    Groups enabled rules by expr_kind, renders each via render_yaml, atomically
    rewrites logs_path (logsql) and metrics_path (metricsql). Both files are
    ALWAYS written (a kind with no enabled rules -> `groups: []`), so disables/
    deletes/renames are reconciled by full rewrite (no orphans).

    Idempotent. Never raises on disk failure — logs + swallows per file so an API
    mutation or boot does not crash, and returns False if any write was swallowed
    (True if both succeeded). render_yaml ValueErrors ARE re-raised (they indicate
    a rule that should never have persisted; callers validated already).
    """
    log: BoundLogger = structlog.get_logger().bind(component="user_rules_render")
    enabled = await repo.list_enabled()
    logs_rules = [r for r in enabled if r.expr_kind == _EXPR_KIND_LOGS]
    metrics_rules = [r for r in enabled if r.expr_kind == _EXPR_KIND_METRICS]
    logs_yaml = render_yaml(logs_rules)
    metrics_yaml = render_yaml(metrics_rules)
    ok = True
    for path, content in ((logs_path, logs_yaml), (metrics_path, metrics_yaml)):
        try:
            _atomic_write(path, content, log)
        except OSError:
            # _atomic_write already logged user_rules.render.write_failed at ERROR;
            # swallow so an API mutation or boot does not crash, but flag overall
            # failure so the caller can warn the operator that the rule is persisted
            # but not yet active (boot reconcile + 30s poll will retry).
            ok = False
    return ok


def render_paths_from_env() -> tuple[Path, Path]:
    """Resolve (logs_path, metrics_path) from env, with in-container defaults.

    HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR  (default /var/vmalert-user-logs)    -> /logs.yaml
    HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR (default /var/vmalert-user-metrics) -> /metrics.yaml
    """
    logs_dir = Path(
        os.environ.get("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", "/var/vmalert-user-logs")
    )
    metrics_dir = Path(
        os.environ.get("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", "/var/vmalert-user-metrics")
    )
    return logs_dir / "logs.yaml", metrics_dir / "metrics.yaml"


__all__ = ["render_all", "render_paths_from_env", "render_yaml"]
