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
class TailConfig:
    """Runtime tunables for the live-tail SSE endpoint (STAGE-004-023).

    ``poll_ms`` — VL poll cadence in milliseconds.
    ``max_connections`` — global cap on concurrent tail connections.
    ``max_lines_per_sec`` — per-connection backpressure cap; surplus is dropped.
    ``max_duration_s`` — per-connection hard cap; the stream closes after this.
    """

    poll_ms: int = 1000
    max_connections: int = 5
    max_lines_per_sec: int = 200
    max_duration_s: int = 3600


def load_tail_config() -> TailConfig:
    """Load live-tail tunables from env (HOMELAB_MONITOR_TAIL_*)."""
    defaults = TailConfig()
    poll_ms = defaults.poll_ms
    max_connections = defaults.max_connections
    max_lines_per_sec = defaults.max_lines_per_sec
    max_duration_s = defaults.max_duration_s
    raw_poll = os.environ.get("HOMELAB_MONITOR_TAIL_POLL_MS")
    if raw_poll is not None:
        poll_ms = int(raw_poll)
    raw_conns = os.environ.get("HOMELAB_MONITOR_TAIL_MAX_CONNECTIONS")
    if raw_conns is not None:
        max_connections = int(raw_conns)
    raw_lps = os.environ.get("HOMELAB_MONITOR_TAIL_MAX_LINES_PER_SEC")
    if raw_lps is not None:
        max_lines_per_sec = int(raw_lps)
    raw_dur = os.environ.get("HOMELAB_MONITOR_TAIL_MAX_DURATION_S")
    if raw_dur is not None:
        max_duration_s = int(raw_dur)
    # Clamp operator foot-gun values: poll_ms must be >= 1 (0 busy-loops the
    # poll); the caps must be >= 1 (0 connections/lines/seconds makes the
    # feature unusable). Mirrors load_cron_anomaly_config's max(..) clamps.
    poll_ms = max(poll_ms, 1)
    max_connections = max(max_connections, 1)
    max_lines_per_sec = max(max_lines_per_sec, 1)
    max_duration_s = max(max_duration_s, 1)
    return TailConfig(
        poll_ms=poll_ms,
        max_connections=max_connections,
        max_lines_per_sec=max_lines_per_sec,
        max_duration_s=max_duration_s,
    )


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
    # STAGE-004-034 — cron run-failure log correlation.
    # cron_failure_enrich_max_lines: capped last-N lines fetched per failed run.
    # cron_failure_enrich_retention_days: independent 30d retention for the
    #   cron_run_failure_enrichments table (D-CRON-RETAIN-30D; outlives the
    #   cron_runs row's own prune). Env: HOMELAB_MONITOR_CRON_ENRICHMENT_RETENTION_DAYS.
    # cron_failure_enrich_max_rows_per_cron: per-fingerprint cap on stored rows.
    cron_failure_enrich_max_lines: int = 50
    cron_failure_enrich_retention_days: int = 30
    cron_failure_enrich_max_rows_per_cron: int = 100


def load_cron_run_reconciler_config() -> CronRunReconcilerConfig:
    """Load CronRunReconciler tunables from env (HOMELAB_MONITOR_CRON_RUN_*)."""
    defaults = CronRunReconcilerConfig()
    retention_days = defaults.retention_days
    max_rows_per_cron = defaults.max_rows_per_cron
    bmode_timeout_hours = defaults.bmode_timeout_hours
    enrich_grace_seconds = defaults.enrich_grace_seconds
    enrich_max_per_tick = defaults.enrich_max_per_tick
    enrich_window_slack_seconds = defaults.enrich_window_slack_seconds
    cron_failure_enrich_max_lines = defaults.cron_failure_enrich_max_lines
    cron_failure_enrich_retention_days = defaults.cron_failure_enrich_retention_days
    cron_failure_enrich_max_rows_per_cron = defaults.cron_failure_enrich_max_rows_per_cron
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
    raw_fail_retention = os.environ.get("HOMELAB_MONITOR_CRON_ENRICHMENT_RETENTION_DAYS")
    if raw_fail_retention is not None:
        cron_failure_enrich_retention_days = int(raw_fail_retention)
    cron_failure_enrich_retention_days = max(cron_failure_enrich_retention_days, 1)
    raw_fail_lines = os.environ.get("HOMELAB_MONITOR_CRON_FAILURE_ENRICH_MAX_LINES")
    if raw_fail_lines is not None:
        cron_failure_enrich_max_lines = int(raw_fail_lines)
    cron_failure_enrich_max_lines = max(cron_failure_enrich_max_lines, 1)
    raw_fail_rows = os.environ.get("HOMELAB_MONITOR_CRON_FAILURE_ENRICH_MAX_ROWS_PER_CRON")
    if raw_fail_rows is not None:
        cron_failure_enrich_max_rows_per_cron = int(raw_fail_rows)
    cron_failure_enrich_max_rows_per_cron = max(cron_failure_enrich_max_rows_per_cron, 1)
    return CronRunReconcilerConfig(
        retention_days=retention_days,
        max_rows_per_cron=max_rows_per_cron,
        bmode_timeout_hours=bmode_timeout_hours,
        enrich_grace_seconds=enrich_grace_seconds,
        enrich_max_per_tick=enrich_max_per_tick,
        enrich_window_slack_seconds=enrich_window_slack_seconds,
        cron_failure_enrich_max_lines=cron_failure_enrich_max_lines,
        cron_failure_enrich_retention_days=cron_failure_enrich_retention_days,
        cron_failure_enrich_max_rows_per_cron=cron_failure_enrich_max_rows_per_cron,
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


@dataclass(frozen=True, slots=True)
class DrainConfig:
    """Runtime tunables for the periodic DrainConsumer (STAGE-004-026).

    ``interval_seconds`` — cycle cadence; the consumer queries VL, feeds the
    DrainEngine, snapshots, and advances the watermark every ``interval_seconds``.
    ``batch_max_lines`` — per-cycle VL line cap; a cycle that returns exactly this
    many lines is treated as PARTIAL (more lines pending) and the watermark only
    advances to the newest line seen, so the next cycle resumes mid-window.
    ``ingest_lag_grace_seconds`` — subtracted from ``now`` to form the query upper
    bound, so lines still propagating through journald→Vector→VL ingest are not
    skipped past by the watermark.
    ``enabled`` — env gate; when false the consumer is never constructed/started.
    ``signature_cardinality_warn_threshold`` — when the number of DISTINCT
    (service_key, template_hash) signatures touched in a single cycle exceeds this,
    the consumer sets the ``homelab_log_signature_cardinality_warn`` gauge to 1.0
    and logs a rising-edge warning. Clamped to >= 1.
    """

    interval_seconds: int = 300
    batch_max_lines: int = 50_000
    ingest_lag_grace_seconds: int = 30
    enabled: bool = True
    signature_cardinality_warn_threshold: int = 100_000


def load_drain_config() -> DrainConfig:
    """Load DrainConsumer tunables from env (HOMELAB_MONITOR_DRAIN_*).

    Clamps interval_seconds >= 1 and batch_max_lines >= 1 (0 makes the cycle
    degenerate) and ingest_lag_grace_seconds >= 0 (negative lag would push the
    query upper bound into the future). A non-numeric *_S / *_LINES env value
    propagates ValueError (mirrors load_tail_config / load_cron_run_reconciler_config).
    """
    defaults = DrainConfig()
    interval_seconds = defaults.interval_seconds
    batch_max_lines = defaults.batch_max_lines
    ingest_lag_grace_seconds = defaults.ingest_lag_grace_seconds
    enabled = defaults.enabled
    signature_cardinality_warn_threshold = defaults.signature_cardinality_warn_threshold
    raw_interval = os.environ.get("HOMELAB_MONITOR_DRAIN_INTERVAL_S")
    if raw_interval is not None:
        interval_seconds = int(raw_interval)
    raw_batch = os.environ.get("HOMELAB_MONITOR_DRAIN_BATCH_MAX_LINES")
    if raw_batch is not None:
        batch_max_lines = int(raw_batch)
    raw_lag = os.environ.get("HOMELAB_MONITOR_DRAIN_INGEST_LAG_GRACE_S")
    if raw_lag is not None:
        ingest_lag_grace_seconds = int(raw_lag)
    raw_enabled = os.environ.get("HOMELAB_MONITOR_DRAIN_ENABLED")
    if raw_enabled is not None:
        enabled = raw_enabled.strip().lower() in ("1", "true", "yes")
    raw_card = os.environ.get("HOMELAB_MONITOR_DRAIN_CARDINALITY_WARN")
    if raw_card is not None:
        signature_cardinality_warn_threshold = int(raw_card)
    interval_seconds = max(interval_seconds, 1)
    batch_max_lines = max(batch_max_lines, 1)
    ingest_lag_grace_seconds = max(ingest_lag_grace_seconds, 0)
    signature_cardinality_warn_threshold = max(signature_cardinality_warn_threshold, 1)
    return DrainConfig(
        interval_seconds=interval_seconds,
        batch_max_lines=batch_max_lines,
        ingest_lag_grace_seconds=ingest_lag_grace_seconds,
        enabled=enabled,
        signature_cardinality_warn_threshold=signature_cardinality_warn_threshold,
    )


@dataclass(frozen=True, slots=True)
class CrashLogConfig:
    """Runtime tunables for ContainerCrashReconciler (STAGE-004-032).

    ``window_before_s`` / ``window_after_s`` bound the VictoriaLogs window
    centered on a container's FinishedAt (the crash anchor). ``line_limit``
    caps lines persisted per crash. ``retention_days`` ages out crash rows;
    ``max_rows_per_container`` caps rows per logical container. Only
    ``window_before_s`` (via HOMELAB_MONITOR_CRASH_LOG_WINDOW_S) and
    ``retention_days`` (via HOMELAB_MONITOR_CRASH_ENRICHMENT_RETENTION_DAYS)
    are env-tunable; the rest are fixed constants.
    """

    window_before_s: int = 60
    window_after_s: int = 5
    line_limit: int = 200
    retention_days: int = 7
    max_rows_per_container: int = 50


def load_crash_log_config() -> CrashLogConfig:
    """Load ContainerCrashReconciler tunables from env.

    HOMELAB_MONITOR_CRASH_LOG_WINDOW_S -> window_before_s (clamped >= 1).
    HOMELAB_MONITOR_CRASH_ENRICHMENT_RETENTION_DAYS -> retention_days (clamped >= 1).
    """
    defaults = CrashLogConfig()
    window_before_s = defaults.window_before_s
    retention_days = defaults.retention_days
    raw_window = os.environ.get("HOMELAB_MONITOR_CRASH_LOG_WINDOW_S")
    if raw_window is not None:
        window_before_s = int(raw_window)
    raw_retention = os.environ.get("HOMELAB_MONITOR_CRASH_ENRICHMENT_RETENTION_DAYS")
    if raw_retention is not None:
        retention_days = int(raw_retention)
    window_before_s = max(window_before_s, 1)
    retention_days = max(retention_days, 1)
    # Defensive clamp: a 0 cap would make the prune per-key DELETE wipe every row
    # (LIMIT 0 -> NOT IN empty set). Harmless today (fixed constant), but guards
    # against a foot-gun if max_rows_per_container ever becomes env-tunable.
    max_rows_per_container = max(defaults.max_rows_per_container, 1)
    return CrashLogConfig(
        window_before_s=window_before_s,
        window_after_s=defaults.window_after_s,
        line_limit=defaults.line_limit,
        retention_days=retention_days,
        max_rows_per_container=max_rows_per_container,
    )


@dataclass(frozen=True, slots=True)
class HealthcheckLogConfig:
    """Runtime tunables for ContainerHealthcheckReconciler (STAGE-004-033).

    ``window_before_s`` / ``window_after_s`` bound the VictoriaLogs window centered
    on a container's healthcheck_changed_at (the edge-into-unhealthy anchor).
    ``line_limit`` caps lines persisted per episode. ``retention_days`` ages out
    rows; ``max_rows_per_container`` caps rows per logical container. Only
    ``window_before_s`` (via HOMELAB_MONITOR_HEALTHCHECK_LOG_WINDOW_S) and
    ``retention_days`` (via HOMELAB_MONITOR_HEALTHCHECK_ENRICHMENT_RETENTION_DAYS)
    are env-tunable; the rest are fixed constants. The 60s total window splits
    30/30 (before is the env-configurable half; after is a fixed 30s constant).
    """

    window_before_s: int = 30
    window_after_s: int = 30
    line_limit: int = 100
    retention_days: int = 7
    max_rows_per_container: int = 50


def load_healthcheck_log_config() -> HealthcheckLogConfig:
    """Load ContainerHealthcheckReconciler tunables from env.

    HOMELAB_MONITOR_HEALTHCHECK_LOG_WINDOW_S -> window_before_s (clamped >= 1).
    HOMELAB_MONITOR_HEALTHCHECK_ENRICHMENT_RETENTION_DAYS -> retention_days (clamped >= 1).
    """
    defaults = HealthcheckLogConfig()
    window_before_s = defaults.window_before_s
    retention_days = defaults.retention_days
    raw_window = os.environ.get("HOMELAB_MONITOR_HEALTHCHECK_LOG_WINDOW_S")
    if raw_window is not None:
        window_before_s = int(raw_window)
    raw_retention = os.environ.get("HOMELAB_MONITOR_HEALTHCHECK_ENRICHMENT_RETENTION_DAYS")
    if raw_retention is not None:
        retention_days = int(raw_retention)
    window_before_s = max(window_before_s, 1)
    retention_days = max(retention_days, 1)
    max_rows_per_container = max(defaults.max_rows_per_container, 1)
    return HealthcheckLogConfig(
        window_before_s=window_before_s,
        window_after_s=defaults.window_after_s,
        line_limit=defaults.line_limit,
        retention_days=retention_days,
        max_rows_per_container=max_rows_per_container,
    )


@dataclass(frozen=True, slots=True)
class NewSignatureConfig:
    """Tunables for NewSignatureCollector (STAGE-004-035).

    ``window_seconds`` — a signature counts as "new" when
    ``now_ms - first_seen_at <= window_seconds * 1000``. Default 300 (5 min);
    catches up if a drain cycle was delayed. Clamped >= 1.
    ``severities`` — the in-scope severity set; a signature alerts only when its
    ``first_seen_severity`` is in this set. Default {error, critical, warning};
    info/debug signatures still get a catalog row but never trip the new-signature
    alert. Empty / all-unknown env input falls back to the default set (an empty
    set would mute the feature entirely — a foot-gun).
    """

    window_seconds: int = 300
    severities: frozenset[str] = frozenset({"error", "critical", "warning"})


def load_new_signature_config() -> NewSignatureConfig:
    """Load NewSignatureCollector tunables from env (HOMELAB_MONITOR_NEW_SIGNATURE_*).

    HOMELAB_MONITOR_NEW_SIGNATURE_WINDOW_S -> window_seconds (clamped >= 1).
    HOMELAB_MONITOR_NEW_SIGNATURE_SEVERITIES -> comma-separated severities,
    lowercased + stripped; empty result falls back to the default set.
    """
    defaults = NewSignatureConfig()
    window_seconds = defaults.window_seconds
    severities = defaults.severities
    raw_window = os.environ.get("HOMELAB_MONITOR_NEW_SIGNATURE_WINDOW_S")
    if raw_window is not None:
        window_seconds = int(raw_window)
    raw_sev = os.environ.get("HOMELAB_MONITOR_NEW_SIGNATURE_SEVERITIES")
    if raw_sev is not None:
        parsed = frozenset(s.strip().lower() for s in raw_sev.split(",") if s.strip())
        if parsed:
            severities = parsed
    window_seconds = max(window_seconds, 1)
    return NewSignatureConfig(window_seconds=window_seconds, severities=severities)


@dataclass(frozen=True, slots=True)
class SilenceDetectionConfig:
    """Tunables for SilenceDetectionCollector (STAGE-004-038).

    A signature is alertable-silent when
    ``silent_min_seconds * 1000 <= now_ms - last_seen_at <= silent_max_seconds * 1000``
    (default 15min..1h) AND not suppressed AND not covered by an active allowlist entry.
    ``cron_grace_seconds`` is passed to is_silence_allowed for cron entries (the
    post-fire active window in which silence is NOT expected). All clamped >= 1.
    """

    silent_min_seconds: int = 900
    silent_max_seconds: int = 3600
    cron_grace_seconds: int = 900


def load_silence_detection_config() -> SilenceDetectionConfig:
    """Load SilenceDetectionCollector tunables from env (HOMELAB_MONITOR_SILENCE_*).

    HOMELAB_MONITOR_SILENCE_MIN_S -> silent_min_seconds (clamped >= 1).
    HOMELAB_MONITOR_SILENCE_MAX_S -> silent_max_seconds (clamped >= 1).
    HOMELAB_MONITOR_SILENCE_CRON_GRACE_S -> cron_grace_seconds (clamped >= 1).
    """
    defaults = SilenceDetectionConfig()
    silent_min_seconds = defaults.silent_min_seconds
    silent_max_seconds = defaults.silent_max_seconds
    cron_grace_seconds = defaults.cron_grace_seconds
    raw_min = os.environ.get("HOMELAB_MONITOR_SILENCE_MIN_S")
    if raw_min is not None:
        silent_min_seconds = int(raw_min)
    raw_max = os.environ.get("HOMELAB_MONITOR_SILENCE_MAX_S")
    if raw_max is not None:
        silent_max_seconds = int(raw_max)
    raw_grace = os.environ.get("HOMELAB_MONITOR_SILENCE_CRON_GRACE_S")
    if raw_grace is not None:
        cron_grace_seconds = int(raw_grace)
    silent_min_seconds = max(silent_min_seconds, 1)
    silent_max_seconds = max(silent_max_seconds, 1)
    cron_grace_seconds = max(cron_grace_seconds, 1)
    return SilenceDetectionConfig(
        silent_min_seconds=silent_min_seconds,
        silent_max_seconds=silent_max_seconds,
        cron_grace_seconds=cron_grace_seconds,
    )


# ---------------------------------------------------------------------------
# STAGE-004-037: error-rate patterns + overrides (logs.error_patterns /
# logs.error_rate_overrides). Mirrors the RedactPattern / DEFAULT_REDACT_PATTERNS
# / load_redact_patterns() precedent above.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ErrorPattern:
    """One error-rate pattern folded into the collector's LogsQL query.

    ``kind`` is a stable identifier carried for the (deferred) per-pattern
    ``homelab_container_error_rate_pattern{name, pattern_kind}`` breakdown.
    ``regex`` is a LogsQL ``_msg:~`` regex fragment (OR-joined with the others).
    """

    kind: str
    regex: str


@dataclass(frozen=True, slots=True)
class ErrorRateOverride:
    """Per-service error-rate override (reserved for STAGE-042; parsed but UNUSED in v1)."""

    service: str
    static_floor: float | None = None
    multiplier: float | None = None


#: The v1 default error-rate patterns (ships with the public release; used when
#: homelab-monitor.yaml omits ``logs.error_patterns``). These catch error-like
#: lines for languages whose ``severity`` field isn't normalized to error
#: (e.g. Python tracebacks). Folded into the collector's single LogsQL query.
DEFAULT_ERROR_PATTERNS: tuple[ErrorPattern, ...] = (
    ErrorPattern(kind="panic", regex="panic"),
    ErrorPattern(kind="traceback", regex="[Tt]raceback"),
    ErrorPattern(kind="exception", regex="[Ee]xception"),
)


@dataclass(frozen=True, slots=True)
class LogsConfig:
    """Operator-tunable error-rate config (logs.error_patterns / logs.error_rate_overrides).

    ``error_patterns`` — folded into the collector's single LogsQL query (OR'd
    with the severity union). Defaults to DEFAULT_ERROR_PATTERNS.
    ``error_rate_overrides`` — per-service tuning, reserved for STAGE-042 (parsed
    here but UNUSED in v1).
    """

    error_patterns: tuple[ErrorPattern, ...] = DEFAULT_ERROR_PATTERNS
    error_rate_overrides: tuple[ErrorRateOverride, ...] = ()


_LOGS_KEY = "logs"
_ERROR_PATTERNS_SUBKEY = "error_patterns"
_ERROR_RATE_OVERRIDES_SUBKEY = "error_rate_overrides"


def load_logs_config() -> LogsConfig:
    """Load error-rate config from YAML ``logs.error_patterns`` / ``logs.error_rate_overrides``.

    Sources:
      - ``logs.error_patterns`` present → parse + validate that list.
      - ``logs`` absent OR ``logs.error_patterns`` absent → DEFAULT_ERROR_PATTERNS.
      - ``logs.error_patterns: []`` (explicit empty list) → empty tuple
        (NOT defaults; mirrors redact precedent but for patterns this means
        "severity union only").
      - ``logs.error_patterns:`` (null value) → DEFAULT_ERROR_PATTERNS.
      - ``logs.error_rate_overrides`` parsed (UNUSED in v1; reserved for 042).

    Validation (raises ValueError):
      - root not a mapping; ``logs`` not a mapping.
      - ``error_patterns`` present but not a list; any entry not a mapping;
        any entry missing ``kind`` / ``regex`` or with a non-string/empty value.
      - ``error_rate_overrides`` present but not a list; any entry not a mapping;
        any entry missing ``service`` or with a non-numeric ``static_floor`` /
        ``multiplier``.
    """
    config_path = Path(os.environ.get("HOMELAB_MONITOR_CONFIG", _DEFAULT_CONFIG_PATH))
    if not config_path.is_file():
        return LogsConfig()

    with config_path.open(encoding="utf-8") as f:
        raw_obj: object = yaml.safe_load(f) or {}
    if not isinstance(raw_obj, dict):
        msg = f"config root must be a mapping, got {type(raw_obj).__name__}"
        raise ValueError(msg)
    raw = cast(dict[str, Any], raw_obj)

    if _LOGS_KEY not in raw:
        return LogsConfig()
    logs_obj: object = raw.get(_LOGS_KEY) or {}
    if not isinstance(logs_obj, dict):
        msg = f"{_LOGS_KEY} must be a mapping, got {type(logs_obj).__name__}"
        raise ValueError(msg)
    logs = cast(dict[str, Any], logs_obj)

    error_patterns = _load_error_patterns(logs)
    error_rate_overrides = _load_error_rate_overrides(logs)
    return LogsConfig(error_patterns=error_patterns, error_rate_overrides=error_rate_overrides)


def _load_error_patterns(logs: dict[str, Any]) -> tuple[ErrorPattern, ...]:
    """Parse logs.error_patterns; default when absent, empty tuple when [] given."""
    if _ERROR_PATTERNS_SUBKEY not in logs:
        return DEFAULT_ERROR_PATTERNS
    raw_obj: object = logs.get(_ERROR_PATTERNS_SUBKEY)
    if raw_obj is None:
        # `error_patterns:` with empty value → defaults (mirror redact precedent).
        return DEFAULT_ERROR_PATTERNS
    if not isinstance(raw_obj, list):
        msg = f"{_LOGS_KEY}.{_ERROR_PATTERNS_SUBKEY} must be a list, got {type(raw_obj).__name__}"
        raise ValueError(msg)
    raw_list = cast(list[object], raw_obj)
    patterns: list[ErrorPattern] = []
    for idx, entry_obj in enumerate(raw_list):
        if not isinstance(entry_obj, dict):
            msg = (
                f"{_LOGS_KEY}.{_ERROR_PATTERNS_SUBKEY}[{idx}] must be a mapping, "
                f"got {type(entry_obj).__name__}"
            )
            raise ValueError(msg)
        entry = cast(dict[str, Any], entry_obj)
        kind = entry.get("kind")
        regex = entry.get("regex")
        for field_name, value in (("kind", kind), ("regex", regex)):
            if not isinstance(value, str) or not value:
                msg = (
                    f"{_LOGS_KEY}.{_ERROR_PATTERNS_SUBKEY}[{idx}] field "
                    f"{field_name!r} must be a non-empty string, got {value!r}"
                )
                raise ValueError(msg)
        patterns.append(ErrorPattern(kind=cast(str, kind), regex=cast(str, regex)))
    return tuple(patterns)


def _load_error_rate_overrides(logs: dict[str, Any]) -> tuple[ErrorRateOverride, ...]:
    """Parse logs.error_rate_overrides (reserved for STAGE-042; v1 carries it unused)."""
    if _ERROR_RATE_OVERRIDES_SUBKEY not in logs:
        return ()
    raw_obj: object = logs.get(_ERROR_RATE_OVERRIDES_SUBKEY)
    if raw_obj is None:
        return ()
    if not isinstance(raw_obj, list):
        msg = (
            f"{_LOGS_KEY}.{_ERROR_RATE_OVERRIDES_SUBKEY} must be a list, "
            f"got {type(raw_obj).__name__}"
        )
        raise ValueError(msg)
    raw_list = cast(list[object], raw_obj)
    overrides: list[ErrorRateOverride] = []
    for idx, entry_obj in enumerate(raw_list):
        if not isinstance(entry_obj, dict):
            msg = (
                f"{_LOGS_KEY}.{_ERROR_RATE_OVERRIDES_SUBKEY}[{idx}] must be a mapping, "
                f"got {type(entry_obj).__name__}"
            )
            raise ValueError(msg)
        entry = cast(dict[str, Any], entry_obj)
        service = entry.get("service")
        if not isinstance(service, str) or not service:
            msg = (
                f"{_LOGS_KEY}.{_ERROR_RATE_OVERRIDES_SUBKEY}[{idx}] field 'service' "
                f"must be a non-empty string, got {service!r}"
            )
            raise ValueError(msg)
        static_floor = _coerce_opt_float(entry, "static_floor", idx, _ERROR_RATE_OVERRIDES_SUBKEY)
        multiplier = _coerce_opt_float(entry, "multiplier", idx, _ERROR_RATE_OVERRIDES_SUBKEY)
        overrides.append(
            ErrorRateOverride(service=service, static_floor=static_floor, multiplier=multiplier)
        )
    return tuple(overrides)


def _coerce_opt_float(entry: dict[str, Any], key: str, idx: int, subkey: str) -> float | None:
    """Read an optional numeric field; None when absent, ValueError when present-but-non-numeric."""
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{_LOGS_KEY}.{subkey}[{idx}] field {key!r} must be numeric, got {value!r}"
        raise ValueError(msg)
    return float(value)


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
    "DEFAULT_ERROR_PATTERNS",
    "DEFAULT_REDACT_PATTERNS",
    "CrashLogConfig",
    "CronAnomalyConfig",
    "CronRunReconcilerConfig",
    "DiskBudgetConfig",
    "DrainConfig",
    "ErrorPattern",
    "ErrorRateOverride",
    "HealthcheckLogConfig",
    "LogStreamBudgetConfig",
    "LogsConfig",
    "NewSignatureConfig",
    "RedactPattern",
    "SilenceDetectionConfig",
    "TailConfig",
    "VlDiskWarningConfig",
    "VlQueryLimits",
    "get_public_url",
    "load_crash_log_config",
    "load_cron_anomaly_config",
    "load_cron_run_reconciler_config",
    "load_disk_budget_config",
    "load_drain_config",
    "load_healthcheck_log_config",
    "load_log_stream_budget_config",
    "load_logs_config",
    "load_new_signature_config",
    "load_redact_patterns",
    "load_silence_detection_config",
    "load_tail_config",
    "load_vl_disk_warning_config",
    "load_vl_query_limits",
    "load_vl_retention_days",
]
