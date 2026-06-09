"""Render user-authored vmalert rules to per-rule YAML files (STAGE-004-043).

Two render dirs, partitioned by expr_kind:
  - logsql    rules -> <logs_dir>/<rule_name>.yaml    (group: user-rules-logs)
  - metricsql rules -> <metrics_dir>/<rule_name>.yaml (group: user-rules-metrics)

ONE FILE PER RULE (BUG 2 fix): vmalert isolates a bad live-reload per file, so a
single invalid rule rejects only its own file's reload — the other already-loaded
rule files keep running. (The previous scheme wrote ALL rules of a kind into one
aggregate file/group, so one bad expr killed every user rule on reload.)

`render_yaml(rules)` is PURE: string-in/string-out, no DB, no filesystem. It
assumes all `rules` share one expr_kind and emits a single `groups:` document
with one group. Reused as `render_yaml([rule])` to render each per-rule file.
Rules are sorted by rule_name for deterministic output. An empty list yields a
valid `groups: []` doc (used only by the pure function's tests now; render_all
no longer writes empty placeholders — it leaves the dir without that rule's file).

`render_all(repo, logs_dir, metrics_dir)` reconciles per-rule files in BOTH dirs
from repo.list_enabled(): writes `<dir>/<rule_name>.yaml` per enabled rule and
unlinks any orphan `*.yaml` (delete/disable/rename, plus the OLD aggregate
logs.yaml/metrics.yaml). Mirrors kernel/cron/render.py's atomic write per file
(tempfile.mkstemp -> os.replace, chmod 0o640, group-own by CONFIG_GROUP_NAME).

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

#: Filename-safety guard for rule_name before using it in a path. Matches the
#: upstream RULE_NAME_REGEX in user_rules_repo (identifier chars only -> no
#: path traversal, no separators, no spaces). Defensive: rule_name is validated
#: at create/patch time, but the renderer must never write an unsafe path.
_SAFE_RULE_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

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


def _render_kind_dir(
    out_dir: Path,
    rules: list[LogUserRule],
    log: BoundLogger,
) -> bool:
    """Render one expr_kind's enabled rules to per-rule files in `out_dir`.

    Writes `<out_dir>/<rule_name>.yaml` (one valid one-rule group per file) for
    each enabled rule, then reconciles orphans: any pre-existing `*.yaml` whose
    stem is NOT a desired rule_name is unlinked (handles delete/disable/rename,
    and migrates away the OLD aggregate `logs.yaml`/`metrics.yaml` whose stem
    `logs`/`metrics` is not a rule name). Empty `rules` -> the dir ends up with
    no `*.yaml` files, so vmalert's glob matches nothing (no placeholder).

    Never raises on disk failure: logs + swallows per op, returns False if any
    write or unlink was swallowed (True if all succeeded). render_yaml
    ValueErrors propagate (a rule that should never have persisted).
    """
    ok = True
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error(
            "user_rules.render.mkdir_failed",
            out_dir=str(out_dir),
            error=str(exc),
        )
        return False
    desired: set[str] = set()
    for rule in rules:
        if _SAFE_RULE_NAME.match(rule.rule_name) is None:
            # Defensive: rule_name is identifier-validated upstream, so this
            # should be unreachable, but never build a path from an unsafe name.
            log.error(
                "user_rules.render.unsafe_rule_name",
                rule_name=rule.rule_name,
                out_dir=str(out_dir),
            )
            ok = False
            continue
        desired.add(rule.rule_name)
        target = out_dir / f"{rule.rule_name}.yaml"
        try:
            _atomic_write(target, render_yaml([rule]), log)
        except OSError:
            # _atomic_write already logged user_rules.render.write_failed.
            ok = False
    # Reconcile orphans: remove any *.yaml whose stem is not a desired rule_name.
    for existing in out_dir.glob("*.yaml"):
        if existing.stem in desired:
            continue
        try:
            existing.unlink()
        except FileNotFoundError:  # pragma: no cover -- raced removal; benign
            pass
        except OSError as exc:
            log.error(
                "user_rules.render.unlink_failed",
                output_path=str(existing),
                error=str(exc),
            )
            ok = False
        else:
            log.info("user_rules.render.orphan_removed", output_path=str(existing))
    return ok


async def render_all(
    repo: LogUserRulesRepository,
    logs_dir: Path,
    metrics_dir: Path,
) -> bool:
    """Reconcile per-rule YAML files in BOTH user-rule dirs from list_enabled().

    Splits enabled rules by expr_kind and writes one file per rule
    (`<dir>/<rule_name>.yaml`) into logs_dir (logsql) and metrics_dir
    (metricsql), reconciling orphans (delete/disable/rename, and the old
    aggregate file) per dir. A bad live-reload of one file rejects only that
    file in vmalert; other already-loaded files keep running (BUG 2 fix).

    Idempotent. Never raises on disk failure — logs + swallows per op, returns
    False if any write/unlink was swallowed (True if all succeeded). render_yaml
    ValueErrors ARE re-raised (callers validated already).
    """
    log: BoundLogger = structlog.get_logger().bind(component="user_rules_render")
    enabled = await repo.list_enabled()
    logs_rules = [r for r in enabled if r.expr_kind == _EXPR_KIND_LOGS]
    metrics_rules = [r for r in enabled if r.expr_kind == _EXPR_KIND_METRICS]
    ok_logs = _render_kind_dir(logs_dir, logs_rules, log)
    ok_metrics = _render_kind_dir(metrics_dir, metrics_rules, log)
    return ok_logs and ok_metrics


def render_dirs_from_env() -> tuple[Path, Path]:
    """Resolve (logs_dir, metrics_dir) from env, with in-container defaults.

    Per-rule rendering (STAGE-004-043 BUG 2): rules are written one file per rule
    INSIDE these dirs (`<dir>/<rule_name>.yaml`), not into a single aggregate
    file. vmalert globs `*.yaml` in each dir.

    HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR    (default /var/vmalert-user-logs)
    HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR (default /var/vmalert-user-metrics)
    """
    logs_dir = Path(
        os.environ.get("HOMELAB_MONITOR_VMALERT_USER_LOGS_DIR", "/var/vmalert-user-logs")
    )
    metrics_dir = Path(
        os.environ.get("HOMELAB_MONITOR_VMALERT_USER_METRICS_DIR", "/var/vmalert-user-metrics")
    )
    return logs_dir, metrics_dir


__all__ = ["render_all", "render_dirs_from_env", "render_yaml"]
