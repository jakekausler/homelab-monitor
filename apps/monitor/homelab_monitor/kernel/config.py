"""Minimal YAML config loader for runtime tuning.

Currently exposes only :func:`load_disk_budget_config`. Future stages will add
their own typed config dataclasses + loaders here.

Sources, in priority order (later overrides earlier):
  1. Hard-coded defaults in :class:`DiskBudgetConfig`
  2. ``HOMELAB_MONITOR_CONFIG`` (default ``/config/homelab-monitor.yaml``);
     extracts the ``disk_budget`` mapping if present.
  3. ``HOMELAB_MONITOR_DISK_BUDGET_GB`` env (overrides ``total_gb`` only).
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml


@dataclass(frozen=True, slots=True)
class DiskBudgetConfig:
    """Total disk budget + per-slot ratio split.

    Default ratios sum to exactly 1.0 (60/30/10 per spec §6.4). Validation
    requires ratios within 0.01 of 1.0 to allow operator-supplied configs that
    don't sum perfectly (e.g. 0.6 + 0.3 + 0.1 = 1.0 exact, but 0.5 + 0.3 + 0.2
    is also fine).
    """

    total_gb: float = 50.0
    vm_ratio: float = 0.60
    vl_ratio: float = 0.30
    sqlite_ratio: float = 0.10


_RATIO_TOLERANCE = 0.01
_DEFAULT_CONFIG_PATH = "/config/homelab-monitor.yaml"


def _coerce_float(d: dict[str, Any], key: str, default: float) -> float:
    """Read d[key] coerced to float, or return default if missing/None."""
    v = d.get(key)
    if v is None:
        return default
    return float(v)


def load_disk_budget_config() -> DiskBudgetConfig:
    """Load the disk budget configuration from YAML + env.

    Returns:
        DiskBudgetConfig: validated configuration.

    Raises:
        ValueError: if YAML ratios do not sum to ~1.0.
        yaml.YAMLError: if the YAML file exists but is malformed.
    """
    config_path = Path(os.environ.get("HOMELAB_MONITOR_CONFIG", _DEFAULT_CONFIG_PATH))

    _defaults = DiskBudgetConfig()
    total_gb = _defaults.total_gb
    vm_ratio = _defaults.vm_ratio
    vl_ratio = _defaults.vl_ratio
    sqlite_ratio = _defaults.sqlite_ratio

    if config_path.is_file():
        with config_path.open(encoding="utf-8") as f:
            raw_obj: object = yaml.safe_load(f) or {}
        if not isinstance(raw_obj, dict):
            msg = f"config root must be a mapping, got {type(raw_obj).__name__}"
            raise ValueError(msg)
        raw = cast(dict[str, Any], raw_obj)
        section_obj: object = raw.get("disk_budget") or {}
        if not isinstance(section_obj, dict):
            msg = f"disk_budget must be a mapping, got {type(section_obj).__name__}"
            raise ValueError(msg)
        section = cast(dict[str, Any], section_obj)
        total_gb = _coerce_float(section, "total_gb", total_gb)
        vm_ratio = _coerce_float(section, "vm_ratio", vm_ratio)
        vl_ratio = _coerce_float(section, "vl_ratio", vl_ratio)
        sqlite_ratio = _coerce_float(section, "sqlite_ratio", sqlite_ratio)

        ratio_sum = vm_ratio + vl_ratio + sqlite_ratio
        if not math.isclose(ratio_sum, 1.0, abs_tol=_RATIO_TOLERANCE):
            msg = (
                f"disk_budget ratios must sum to ~1.0 (within {_RATIO_TOLERANCE}); got {ratio_sum}"
            )
            raise ValueError(msg)

    env_total_gb = os.environ.get("HOMELAB_MONITOR_DISK_BUDGET_GB")
    if env_total_gb is not None:
        total_gb = float(env_total_gb)

    return DiskBudgetConfig(
        total_gb=total_gb,
        vm_ratio=vm_ratio,
        vl_ratio=vl_ratio,
        sqlite_ratio=sqlite_ratio,
    )


@dataclass(frozen=True, slots=True)
class LogStreamBudgetConfig:
    """Per-stream log budget defaults.

    ``lines_per_sec_per_stream`` matches the throttle threshold in vector.toml
    so vmalert rules and dashboards can reference a single source of truth.
    ``bytes_per_day_per_stream`` is the soft-cap used by Grafana panels and
    future per-stream alert rules.
    """

    lines_per_sec_per_stream: float = 50.0
    bytes_per_day_per_stream: int = 500 * 1024 * 1024  # 500 MiB


_LOG_STREAM_BUDGET_KEY = "log_stream_budget"


def load_log_stream_budget_config() -> LogStreamBudgetConfig:
    """Load the log-stream budget configuration from YAML.

    Returns:
        LogStreamBudgetConfig: validated configuration.

    Raises:
        ValueError: if the YAML root or section is not a mapping.
        yaml.YAMLError: if the YAML file exists but is malformed.
    """
    config_path = Path(os.environ.get("HOMELAB_MONITOR_CONFIG", _DEFAULT_CONFIG_PATH))

    defaults = LogStreamBudgetConfig()
    lines_per_sec = defaults.lines_per_sec_per_stream
    bytes_per_day = defaults.bytes_per_day_per_stream

    if config_path.is_file():
        with config_path.open(encoding="utf-8") as f:
            raw_obj: object = yaml.safe_load(f) or {}
        if not isinstance(raw_obj, dict):
            msg = f"config root must be a mapping, got {type(raw_obj).__name__}"
            raise ValueError(msg)
        raw = cast(dict[str, Any], raw_obj)
        section_obj: object = raw.get(_LOG_STREAM_BUDGET_KEY) or {}
        if not isinstance(section_obj, dict):
            msg = f"{_LOG_STREAM_BUDGET_KEY} must be a mapping, got {type(section_obj).__name__}"
            raise ValueError(msg)
        section = cast(dict[str, Any], section_obj)
        lps = section.get("lines_per_sec_per_stream")
        if lps is not None:
            lines_per_sec = float(lps)
        bpd = section.get("bytes_per_day_per_stream")
        if bpd is not None:
            bytes_per_day = int(bpd)

    return LogStreamBudgetConfig(
        lines_per_sec_per_stream=lines_per_sec,
        bytes_per_day_per_stream=bytes_per_day,
    )


def get_public_url() -> str | None:
    """Return the monitor's externally-reachable base URL from HOMELAB_MONITOR_PUBLIC_URL.

    Used by the wrapper installer to render the heartbeat callback URL.
    Returns None if unset or empty.
    """
    return os.environ.get("HOMELAB_MONITOR_PUBLIC_URL") or None


@dataclass(frozen=True, slots=True)
class VlQueryLimits:
    """Hard bounds applied to every VictoriaLogsClient query.

    ``timeout_seconds`` is the OVERALL httpx timeout (connect + read +
    write), not a per-stage budget. The reconciler's collector ``timeout``
    is ``timedelta(seconds=20)``, so two back-to-back VL timeouts at the
    default ``10s`` still fit a single tick.
    """

    max_lines: int = 10_000
    max_bytes: int = 5_000_000
    timeout_seconds: float = 10.0


def load_vl_query_limits() -> VlQueryLimits:
    """Load VL query hard-limits from env (HOMELAB_MONITOR_VL_QUERY_*)."""
    defaults = VlQueryLimits()
    max_lines = defaults.max_lines
    max_bytes = defaults.max_bytes
    timeout_seconds = defaults.timeout_seconds
    raw_lines = os.environ.get("HOMELAB_MONITOR_VL_QUERY_MAX_LINES")
    if raw_lines is not None:
        max_lines = int(raw_lines)
    raw_bytes = os.environ.get("HOMELAB_MONITOR_VL_QUERY_MAX_BYTES")
    if raw_bytes is not None:
        max_bytes = int(raw_bytes)
    raw_timeout = os.environ.get("HOMELAB_MONITOR_VL_QUERY_TIMEOUT_SECONDS")
    if raw_timeout is not None:
        timeout_seconds = float(raw_timeout)
    return VlQueryLimits(max_lines=max_lines, max_bytes=max_bytes, timeout_seconds=timeout_seconds)


@dataclass(frozen=True, slots=True)
class CronRunReconcilerConfig:
    """Runtime tunables for CronRunReconciler (env-only).

    ``enrich_max_per_tick`` caps the number of runs enriched per tick,
    preventing large backlogs from exceeding the collector's 20s timeout.
    A large backlog drains over successive ticks; enrichment is idempotent
    per §6.3 so retries are cheap.
    ``enrich_window_slack_seconds`` extra seconds to add to the VL query upper
    bound during enrichment. Bridges the gap between when the wrapper posts /ok
    (ended_at) and when the captured hmrun lines actually appear in VictoriaLogs
    (journald → Vector → VL ingest latency, typically 1-5s; allow up to 30s
    headroom for slow pipelines).
    """

    retention_days: int = 30
    max_rows_per_cron: int = 50_000
    bmode_timeout_hours: int = 6
    enrich_grace_seconds: int = 15
    enrich_max_per_tick: int = 200
    enrich_window_slack_seconds: int = 30


def load_cron_run_reconciler_config() -> CronRunReconcilerConfig:
    """Load CronRunReconciler tunables from env (HOMELAB_MONITOR_CRON_RUN_*)."""
    defaults = CronRunReconcilerConfig()
    retention_days = defaults.retention_days
    max_rows_per_cron = defaults.max_rows_per_cron
    bmode_timeout_hours = defaults.bmode_timeout_hours
    enrich_grace_seconds = defaults.enrich_grace_seconds
    enrich_max_per_tick = defaults.enrich_max_per_tick
    enrich_window_slack_seconds = defaults.enrich_window_slack_seconds
    raw_days = os.environ.get("HOMELAB_MONITOR_CRON_RUN_RETENTION_DAYS")
    if raw_days is not None:
        retention_days = int(raw_days)
    raw_max = os.environ.get("HOMELAB_MONITOR_CRON_RUN_MAX_ROWS_PER_CRON")
    if raw_max is not None:
        max_rows_per_cron = int(raw_max)
    raw_timeout_h = os.environ.get("HOMELAB_MONITOR_CRON_RUN_BMODE_TIMEOUT_HOURS")
    if raw_timeout_h is not None:
        bmode_timeout_hours = int(raw_timeout_h)
    raw_grace = os.environ.get("HOMELAB_MONITOR_CRON_RUN_ENRICH_GRACE_SECONDS")
    if raw_grace is not None:
        enrich_grace_seconds = int(raw_grace)
    raw_enrich_max = os.environ.get("HOMELAB_MONITOR_CRON_RUN_ENRICH_MAX_PER_TICK")
    if raw_enrich_max is not None:
        enrich_max_per_tick = int(raw_enrich_max)
    raw_slack = os.environ.get("HOMELAB_MONITOR_CRON_RUN_ENRICH_WINDOW_SLACK_SECONDS")
    if raw_slack is not None:
        enrich_window_slack_seconds = int(raw_slack)
    return CronRunReconcilerConfig(
        retention_days=retention_days,
        max_rows_per_cron=max_rows_per_cron,
        bmode_timeout_hours=bmode_timeout_hours,
        enrich_grace_seconds=enrich_grace_seconds,
        enrich_max_per_tick=enrich_max_per_tick,
        enrich_window_slack_seconds=enrich_window_slack_seconds,
    )


@dataclass(frozen=True, slots=True)
class CronAnomalyConfig:
    """Tunables for the rule-based anomaly evaluator (STAGE-002-014).

    Every rule is gated on at least ``min_history`` completed runs of the
    same cron. ``rolling_window`` caps how many recent completed runs the
    evaluator considers as the baseline. ``duration_k`` is the
    duration_outlier multiplier above p95. ``output_band`` is the
    fractional ±-band around the rolling median used by
    output_size_spike / output_size_drop (0.5 → ±50%).
    """

    min_history: int = 10
    rolling_window: int = 20
    duration_k: float = 4.0
    output_band: float = 0.5


def load_cron_anomaly_config() -> CronAnomalyConfig:
    """Load CronAnomalyConfig from env (HOMELAB_MONITOR_CRON_ANOMALY_*).

    Clamped to min_history ≥ 2 because rolling-stats helpers (p95, median)
    need at least 2 values to be meaningful; min_history=1 produces degenerate
    thresholds.
    """
    defaults = CronAnomalyConfig()
    min_history = defaults.min_history
    rolling_window = defaults.rolling_window
    duration_k = defaults.duration_k
    output_band = defaults.output_band
    raw_min = os.environ.get("HOMELAB_MONITOR_CRON_ANOMALY_MIN_HISTORY")
    if raw_min is not None:
        min_history = int(raw_min)
    raw_window = os.environ.get("HOMELAB_MONITOR_CRON_ANOMALY_ROLLING_WINDOW")
    if raw_window is not None:
        rolling_window = int(raw_window)
    raw_k = os.environ.get("HOMELAB_MONITOR_CRON_ANOMALY_DURATION_K")
    if raw_k is not None:
        duration_k = float(raw_k)
    raw_band = os.environ.get("HOMELAB_MONITOR_CRON_ANOMALY_OUTPUT_BAND")
    if raw_band is not None:
        output_band = float(raw_band)
    min_history = max(min_history, 2)
    return CronAnomalyConfig(
        min_history=min_history,
        rolling_window=rolling_window,
        duration_k=duration_k,
        output_band=output_band,
    )


def load_vl_retention_days() -> int:
    """Return the configured VictoriaLogs retention (days).

    Carries from VL's own ``-retentionPeriod`` default of 30 days. Read by
    the narrow run-log endpoint to decide whether a closed run's vl_window
    has aged out (log_status='expired'). A single global env override.
    """
    raw = os.environ.get("HOMELAB_MONITOR_VL_RETENTION_DAYS")
    if raw is None:
        return 30
    return int(raw)


@dataclass(frozen=True, slots=True)
class VlDiskWarningConfig:
    """Warn / crit thresholds (percent of the VL disk budget) for the logs
    settings page. Env-only (no YAML home yet). ``warn_pct`` < ``crit_pct``
    is expected but NOT enforced here — the UI colors by ``>=`` comparison."""

    warn_pct: int = 70
    crit_pct: int = 85


def load_vl_disk_warning_config() -> VlDiskWarningConfig:
    """Load VL disk warn/crit thresholds from env (HOMELAB_MONITOR_VL_DISK_*).

    Normalizes so warn_pct <= crit_pct regardless of env ordering."""
    defaults = VlDiskWarningConfig()
    warn_pct = defaults.warn_pct
    crit_pct = defaults.crit_pct
    raw_warn = os.environ.get("HOMELAB_MONITOR_VL_DISK_WARN_PCT")
    if raw_warn is not None:
        warn_pct = int(raw_warn)
    raw_crit = os.environ.get("HOMELAB_MONITOR_VL_DISK_CRIT_PCT")
    if raw_crit is not None:
        crit_pct = int(raw_crit)
    lo, hi = sorted([warn_pct, crit_pct])
    return VlDiskWarningConfig(warn_pct=lo, crit_pct=hi)


# ---------------------------------------------------------------------------
# STAGE-004-006: log redaction patterns (logs.redact:)
# ---------------------------------------------------------------------------

_REDACT_LOOKAROUND_TOKENS: tuple[str, ...] = ("(?=", "(?!", "(?<=", "(?<!")


@dataclass(frozen=True, slots=True)
class RedactPattern:
    """One redaction rule rendered into the Vector redact VRL block.

    ``name`` becomes the ``pattern_type`` metric label AND the ``.rdt_<name>``
    marker field, so it MUST be a valid metric-label + VRL-field token
    (lowercase snake_case; validated below). ``pattern`` is a Rust-regex-crate
    pattern (NO lookarounds). ``replacement`` is the literal replacement string
    (may contain ``${1}`` capture backrefs).
    """

    name: str
    pattern: str
    replacement: str


#: The v1 default redaction policy (ships with the public release; used when
#: homelab-monitor.yaml omits ``logs.redact``). EPIC-007/008 ADD host-specific
#: patterns to the yaml later — no code change needed.
DEFAULT_REDACT_PATTERNS: tuple[RedactPattern, ...] = (
    RedactPattern(
        name="bearer_token",
        pattern=r"(?i)bearer\s+[A-Za-z0-9._-]{20,}",
        replacement="Bearer [REDACTED]",
    ),
    RedactPattern(
        name="jwt",
        pattern=r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        replacement="[REDACTED_JWT]",
    ),
    RedactPattern(
        name="password_in_url",
        pattern=r"://[^:@/\s]+:[^@/\s]+@",
        replacement="://[REDACTED]:[REDACTED]@",
    ),
    RedactPattern(
        name="aws_access_key",
        pattern=r"AKIA[0-9A-Z]{16}",
        replacement="[REDACTED_AWS_KEY]",
    ),
    RedactPattern(
        name="api_key_generic",
        pattern=(
            r"(?i)(api[-_]?key|api[-_]?token|access[-_]?token|secret[-_]?key)"
            r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._-]{16,}"
        ),
        replacement="${1}=[REDACTED]",
    ),
)

_REDACT_KEY = "logs"
_REDACT_SUBKEY = "redact"
_REDACT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _assert_redact_no_lookarounds(pattern: str, name: str) -> None:
    """Raise ValueError if pattern contains a regex lookaround (Rust regex crate)."""
    for token in _REDACT_LOOKAROUND_TOKENS:
        if token in pattern:
            msg = (
                f"logs.redact entry {name!r} pattern uses lookaround {token!r}; "
                "Vector's Rust regex crate rejects lookarounds"
            )
            raise ValueError(msg)


def load_redact_patterns() -> list[RedactPattern]:
    """Load the redaction policy from YAML ``logs.redact``.

    Sources:
      - ``logs.redact`` present  → parse + validate that list.
      - ``logs`` absent OR ``logs.redact`` absent → ``DEFAULT_REDACT_PATTERNS``.
      - ``logs.redact: []`` (explicit empty list) → empty list (redaction OFF).

    Validation (raises ValueError):
      - root not a mapping; ``logs`` not a mapping; ``redact`` not a list.
      - any entry not a mapping.
      - any entry missing ``name`` / ``pattern`` / ``replacement`` or with a
        non-string / empty value for any of them.
      - ``name`` not lowercase snake_case (``^[a-z][a-z0-9_]*$``).
      - duplicate ``name``.
      - ``pattern`` containing a regex lookaround.
    """
    config_path = Path(os.environ.get("HOMELAB_MONITOR_CONFIG", _DEFAULT_CONFIG_PATH))
    if not config_path.is_file():
        return list(DEFAULT_REDACT_PATTERNS)

    with config_path.open(encoding="utf-8") as f:
        raw_obj: object = yaml.safe_load(f) or {}
    if not isinstance(raw_obj, dict):
        msg = f"config root must be a mapping, got {type(raw_obj).__name__}"
        raise ValueError(msg)
    raw = cast(dict[str, Any], raw_obj)

    if _REDACT_KEY not in raw:
        return list(DEFAULT_REDACT_PATTERNS)
    logs_obj: object = raw.get(_REDACT_KEY) or {}
    if not isinstance(logs_obj, dict):
        msg = f"{_REDACT_KEY} must be a mapping, got {type(logs_obj).__name__}"
        raise ValueError(msg)
    logs = cast(dict[str, Any], logs_obj)

    if _REDACT_SUBKEY not in logs:
        return list(DEFAULT_REDACT_PATTERNS)
    redact_obj: object = logs.get(_REDACT_SUBKEY)
    if redact_obj is None:
        # `redact:` with empty value → defaults (mirror empty-section precedent).
        return list(DEFAULT_REDACT_PATTERNS)
    if not isinstance(redact_obj, list):
        msg = f"{_REDACT_KEY}.{_REDACT_SUBKEY} must be a list, got {type(redact_obj).__name__}"
        raise ValueError(msg)
    redact_list = cast(list[object], redact_obj)

    patterns: list[RedactPattern] = []
    seen: set[str] = set()
    for idx, entry_obj in enumerate(redact_list):
        patterns.append(_validate_redact_entry(idx, entry_obj, seen))
    return patterns


def _validate_redact_entry(idx: int, entry_obj: object, seen: set[str]) -> RedactPattern:
    """Validate and convert one raw logs.redact entry into a RedactPattern."""
    if not isinstance(entry_obj, dict):
        msg = f"logs.redact[{idx}] must be a mapping, got {type(entry_obj).__name__}"
        raise ValueError(msg)
    entry = cast(dict[str, Any], entry_obj)
    name = entry.get("name")
    pattern = entry.get("pattern")
    replacement = entry.get("replacement")
    for field_name, value in (
        ("name", name),
        ("pattern", pattern),
        ("replacement", replacement),
    ):
        if not isinstance(value, str) or not value:
            msg = (
                f"logs.redact[{idx}] field {field_name!r} must be a non-empty string, got {value!r}"
            )
            raise ValueError(msg)
    name_s = cast(str, name)
    pattern_s = cast(str, pattern)
    replacement_s = cast(str, replacement)
    if not _REDACT_NAME_RE.match(name_s):
        msg = f"logs.redact[{idx}] name {name_s!r} must be lowercase snake_case (^[a-z][a-z0-9_]*$)"
        raise ValueError(msg)
    if name_s in seen:
        msg = f"logs.redact has duplicate name {name_s!r}"
        raise ValueError(msg)
    seen.add(name_s)
    _assert_redact_no_lookarounds(pattern_s, name_s)
    return RedactPattern(name=name_s, pattern=pattern_s, replacement=replacement_s)


__all__ = [
    "DEFAULT_REDACT_PATTERNS",
    "CronAnomalyConfig",
    "CronRunReconcilerConfig",
    "DiskBudgetConfig",
    "LogStreamBudgetConfig",
    "RedactPattern",
    "VlDiskWarningConfig",
    "VlQueryLimits",
    "get_public_url",
    "load_cron_anomaly_config",
    "load_cron_run_reconciler_config",
    "load_disk_budget_config",
    "load_log_stream_budget_config",
    "load_redact_patterns",
    "load_vl_disk_warning_config",
    "load_vl_query_limits",
    "load_vl_retention_days",
]
