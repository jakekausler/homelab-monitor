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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

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


def _coerce_int(d: dict[str, Any], key: str, default: int) -> int:
    """Read d[key] coerced to int, or return default if missing/None.

    Raises ValueError when the value is present but non-integer (mirrors the
    explicit-coercion guard in load_log_stream_budget_config).
    """
    v = d.get(key)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        msg = f"{key!r} must be an integer, got {v!r}"
        raise ValueError(msg)
    try:
        return int(v)
    except (TypeError, ValueError) as exc:
        msg = f"{key!r} must be an integer, got {v!r}"
        raise ValueError(msg) from exc


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
            try:
                lines_per_sec = float(lps)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{_LOG_STREAM_BUDGET_KEY}.lines_per_sec_per_stream must be a "
                    f"number, got {lps!r}"
                ) from exc
        bpd = section.get("bytes_per_day_per_stream")
        if bpd is not None:
            try:
                bytes_per_day = int(bpd)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{_LOG_STREAM_BUDGET_KEY}.bytes_per_day_per_stream must be an "
                    f"integer, got {bpd!r}"
                ) from exc

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
class VlHealthConfig:
    """Probe timeout for VlHealthCollector. Env-only.

    ``timeout_s`` — HTTP GET timeout in seconds for the /health probe.
    """

    timeout_s: float = 5.0


def load_vl_health_config() -> VlHealthConfig:
    """Load VL health probe timeout from env (HOMELAB_MONITOR_VL_HEALTH_TIMEOUT_S)."""
    raw = os.environ.get("HOMELAB_MONITOR_VL_HEALTH_TIMEOUT_S")
    if raw is None:
        return VlHealthConfig()
    return VlHealthConfig(timeout_s=float(raw))


@dataclass(frozen=True, slots=True)
class HaConfig:
    """Home Assistant connection config (STAGE-005-001). Env-only.

    ``base_url`` is the HA instance base URL (no trailing slash; the loader
    strips it). The long-lived bearer token is NOT here — it lives in the
    secret store under ``ha_token`` and is resolved at request time.

    ``notify_service`` (STAGE-005-017) is the HA ``notify`` target name (e.g.
    ``mobile_app_pixel``) used by the alert push channel. Empty string (the
    open-source-safe default) makes the push channel a no-op — no HA call is
    made — so a public release never tries to notify a service that does not
    exist on the operator's instance.

    ``event_type`` (STAGE-005-020) is the HA event-bus event type fired by the
    ha_event push-back channel (recommended value ``homelab_monitor_alert``).
    Empty string (the open-source-safe default) makes that channel globally
    OFF — no event is fired — so a public release never spams an event bus the
    operator has no automation for. Per-alert opt-in is via the
    ``push_to_ha=="true"`` label (STOPGAP — see EPIC-012 STAGE-012-005).
    """

    base_url: str = "http://192.168.2.148:8123"
    notify_service: str = ""
    event_type: str = ""


def load_ha_config() -> HaConfig:
    """Load HA connection config from env.

    ``HOMELAB_MONITOR_HA_URL`` -> ``base_url`` (trailing slash stripped so client
    path concatenation is unambiguous; default base URL when unset).
    ``HOMELAB_MONITOR_HA_NOTIFY_SERVICE`` -> ``notify_service`` (default ``""``;
    empty makes the alert push channel a no-op).
    ``HOMELAB_MONITOR_HA_EVENT_TYPE`` -> ``event_type`` (default ``""``; empty
    makes the ha_event push-back channel globally OFF. Recommended value:
    ``homelab_monitor_alert``).
    """
    raw = os.environ.get("HOMELAB_MONITOR_HA_URL")
    base_url = HaConfig().base_url if raw is None else raw.rstrip("/")
    notify_service = os.environ.get("HOMELAB_MONITOR_HA_NOTIFY_SERVICE", "")
    event_type = os.environ.get("HOMELAB_MONITOR_HA_EVENT_TYPE", "")
    return HaConfig(base_url=base_url, notify_service=notify_service, event_type=event_type)


@dataclass(frozen=True, slots=True)
class UnifiConfig:
    """Unifi controller connection config (STAGE-007-001). Env-only.

    ``base_url`` is the UDM controller base URL (no trailing slash; the loader strips
    it). The read-only API key is NOT here — it lives in the secret store under
    ``unifi_api_key`` and is resolved at request time. ``site_id`` is the controller
    site id (default ``"default"``); the client re-caches it from ``v1/sites`` at
    startup, so this is just the pre-resolution default.

    ``host_lan_ip`` (STAGE-007-002) is the LAN IP of the monitor host on the Unifi
    network, used for host attribution by Wave-B/C collectors.

    ``ssh_lease_enabled`` (STAGE-007-002) is the opt-in gate (default ``False``) the
    SSH DHCP-lease collector (STAGE-007-012) reads. Defined here now; its consumer
    lands in 012.

    ``ssh_lease_target_id`` (STAGE-007-012) is the SSH target id the lease collector
    opens (default ``"udm"``); env ``HOMELAB_MONITOR_UNIFI_SSH_LEASE_TARGET_ID``.

    ``observation_retention_days`` (STAGE-007-003) is the time-window for the
    unifi_client_observations span prune (default 90 days). The caller computes a
    cutoff = now - observation_retention_days and passes it to
    UnifiClientRepo.append_observation_conn, which deletes spans older than the cutoff.

    ``expected_dns_steering_ip`` (STAGE-007-026) is the expected per-network DNS-steering
    IP the dns-posture endpoint compares each handout against to flag drift (default
    ``"192.168.2.148"``; env ``HOMELAB_MONITOR_UNIFI_EXPECTED_DNS_STEERING_IP``). An empty
    value is treated as "not configured" by the endpoint (no drift flagged).
    """

    base_url: str = "https://192.168.2.1"
    site_id: str = "default"
    host_lan_ip: str = "192.168.2.148"
    expected_dns_steering_ip: str = "192.168.2.148"
    ssh_lease_enabled: bool = False
    ssh_lease_target_id: str = "udm"
    observation_retention_days: int = 90


def load_unifi_config() -> UnifiConfig:
    """Load Unifi connection config from env.

    ``HOMELAB_MONITOR_UNIFI_URL`` -> ``base_url`` (trailing slash stripped so client
    path concatenation is unambiguous; default base URL when unset).
    ``HOMELAB_MONITOR_UNIFI_SITE_ID`` -> ``site_id`` (default ``"default"``).
    ``HOMELAB_MONITOR_UNIFI_HOST_LAN_IP`` -> ``host_lan_ip`` (default
    ``"192.168.2.148"``; raw value, no strip — an IP carries no trailing slash).
    ``HOMELAB_MONITOR_UNIFI_EXPECTED_DNS_STEERING_IP`` -> ``expected_dns_steering_ip``
    (default ``"192.168.2.148"``; raw value, no strip; empty string is meaningful —
    the dns-posture endpoint treats it as "not configured").
    ``HOMELAB_MONITOR_UNIFI_SSH_LEASE_ENABLED`` -> ``ssh_lease_enabled`` (default
    ``False``; truthy when the env value lowercases to one of 1/true/yes).
    ``HOMELAB_MONITOR_UNIFI_SSH_LEASE_TARGET_ID`` -> ``ssh_lease_target_id`` (default
    ``"udm"`` when unset or empty/whitespace; stripped of surrounding whitespace).
    ``HOMELAB_MONITOR_UNIFI_OBSERVATION_RETENTION_DAYS`` -> ``observation_retention_days``
    (default ``90``; parsed as int — an unparseable value raises ValueError, mirroring
    load_vl_retention_days / load_cron_run_reconciler_config).
    """
    raw = os.environ.get("HOMELAB_MONITOR_UNIFI_URL")
    base_url = UnifiConfig().base_url if raw is None else raw.rstrip("/")
    site_id = os.environ.get("HOMELAB_MONITOR_UNIFI_SITE_ID", "default")
    host_lan_ip = os.environ.get("HOMELAB_MONITOR_UNIFI_HOST_LAN_IP", UnifiConfig().host_lan_ip)
    expected_dns_steering_ip = os.environ.get(
        "HOMELAB_MONITOR_UNIFI_EXPECTED_DNS_STEERING_IP",
        UnifiConfig().expected_dns_steering_ip,
    )
    raw_ssh_lease = os.environ.get("HOMELAB_MONITOR_UNIFI_SSH_LEASE_ENABLED")
    ssh_lease_enabled = (
        UnifiConfig().ssh_lease_enabled
        if raw_ssh_lease is None
        else raw_ssh_lease.strip().lower() in ("1", "true", "yes")
    )
    raw_target = os.environ.get("HOMELAB_MONITOR_UNIFI_SSH_LEASE_TARGET_ID")
    ssh_lease_target_id = (
        UnifiConfig().ssh_lease_target_id
        if raw_target is None or not raw_target.strip()
        else raw_target.strip()
    )
    raw_retention = os.environ.get("HOMELAB_MONITOR_UNIFI_OBSERVATION_RETENTION_DAYS")
    observation_retention_days = (
        UnifiConfig().observation_retention_days if raw_retention is None else int(raw_retention)
    )
    return UnifiConfig(
        base_url=base_url,
        site_id=site_id,
        host_lan_ip=host_lan_ip,
        expected_dns_steering_ip=expected_dns_steering_ip,
        ssh_lease_enabled=ssh_lease_enabled,
        ssh_lease_target_id=ssh_lease_target_id,
        observation_retention_days=observation_retention_days,
    )


@dataclass(frozen=True, slots=True)
class PiholeConfig:
    """Pi-hole connection config (STAGE-006-001). Env-only.

    ``base_url`` is the Pi-hole instance base URL (no trailing slash; the loader
    strips it). The app password is NOT here — it lives in the secret store under
    ``pihole_api_password_ro`` (read-only collectors) and ``pihole_api_password_rw``
    (Wave-E write actions, first used STAGE-006-018) and is resolved at request time
    via ``POST /api/auth``. Pi-hole is plain HTTP on the LAN, so the default points
    at the host's LAN IP (mirrors HaConfig; the bridge-network container cannot reach
    ``localhost`` which is its own loopback).

    ``host_lan_ip`` (STAGE-006-004) is the LAN IP of the monitor host on the Pi-hole
    network, used by the client classification helper to attribute loopback clients.
    Defaults to empty string — callers must set ``HOMELAB_MONITOR_PIHOLE_HOST_LAN_IP``
    to enable full attribution; without it, loopback clients receive kind
    ``"unattributed"``. The empty default is intentional: unlike Unifi (where the host
    IP is always required for registry reconciliation), Pi-hole classification degrades
    gracefully.

    ``dns_host`` (STAGE-006-014) is the resolver IP for the INDEPENDENT direct
    UDP :53 DNS health probe. Empty (the default) means "derive from base_url's
    hostname" in the loader. ``dns_port`` defaults to 53.
    (STAGE-006-015) adds ``direct_dns_host`` / ``direct_dns_port`` — a WAN-bypass
    resolver (default ``1.1.1.1:53``) probed alongside Pi-hole each cycle for the
    DNS split-check.
    """

    base_url: str = "http://192.168.2.148:8080"  # host LAN IP (container cannot reach localhost)
    host_lan_ip: str = ""
    dns_host: str = ""  # resolver IP for the direct :53 DNS probe; empty -> derive from base_url
    dns_port: int = 53
    direct_dns_host: str = "1.1.1.1"  # WAN-bypass resolver for split-check (STAGE-006-015)
    direct_dns_port: int = 53
    # STAGE-006-025: default-OFF query-feed log shipper. Ships PII (DNS history)
    # to VictoriaLogs stream "pihole-queries"; gated behind this flag.
    stream_query_feed_enabled: bool = False
    # STAGE-006-025: per-UTC-day byte cap for the query feed. Defaults to the
    # standard per-stream log budget (500 MiB). On cap-hit the shipper stops
    # ingesting for the day but still advances the cursor (drop, don't backlog).
    query_feed_max_bytes_per_day: int = 500 * 1024 * 1024


def load_pihole_config() -> PiholeConfig:
    """Load Pi-hole connection config from env.

    ``HOMELAB_MONITOR_PIHOLE_URL`` -> ``base_url`` (trailing slash stripped so client
    path concatenation is unambiguous; default ``http://192.168.2.148:8080`` — the host's
    LAN IP — when unset, because the prod monitor runs on a bridge network and cannot reach
    localhost which is its own container loopback).
    ``HOMELAB_MONITOR_PIHOLE_HOST_LAN_IP`` -> ``host_lan_ip`` (raw value, no strip — an
    IP carries no trailing slash; default ``""`` when unset, preserving graceful
    degradation to ``"unattributed"`` in the client classifier).
    ``HOMELAB_MONITOR_PIHOLE_DNS_HOST`` -> ``dns_host`` (raw IP; empty default means
    derive from base_url's hostname). ``HOMELAB_MONITOR_PIHOLE_DNS_PORT`` -> ``dns_port``
    (int; default 53).
    ``HOMELAB_MONITOR_PIHOLE_DIRECT_DNS_HOST`` -> ``direct_dns_host`` (raw IP; empty
    default falls back to literal ``1.1.1.1`` — NOT derived from base_url).
    ``HOMELAB_MONITOR_PIHOLE_DIRECT_DNS_PORT`` -> ``direct_dns_port`` (int; default 53).
    """
    raw = os.environ.get("HOMELAB_MONITOR_PIHOLE_URL")
    base_url = PiholeConfig().base_url if raw is None else raw.rstrip("/")
    host_lan_ip = os.environ.get("HOMELAB_MONITOR_PIHOLE_HOST_LAN_IP", PiholeConfig().host_lan_ip)

    dns_host = os.environ.get("HOMELAB_MONITOR_PIHOLE_DNS_HOST", "")
    if not dns_host:
        dns_host = urlparse(base_url).hostname or ""

    raw_port = os.environ.get("HOMELAB_MONITOR_PIHOLE_DNS_PORT")
    dns_port = PiholeConfig().dns_port if raw_port is None else int(raw_port)

    direct_dns_host = os.environ.get("HOMELAB_MONITOR_PIHOLE_DIRECT_DNS_HOST", "")
    if not direct_dns_host:
        direct_dns_host = PiholeConfig().direct_dns_host

    raw_direct_port = os.environ.get("HOMELAB_MONITOR_PIHOLE_DIRECT_DNS_PORT")
    direct_dns_port = (
        PiholeConfig().direct_dns_port if raw_direct_port is None else int(raw_direct_port)
    )

    raw_qf = os.environ.get("HOMELAB_MONITOR_PIHOLE_STREAM_QUERY_FEED", "")
    stream_query_feed_enabled = raw_qf.strip().lower() in ("1", "true", "yes")

    raw_qf_bytes = os.environ.get("HOMELAB_MONITOR_PIHOLE_QUERY_FEED_MAX_BYTES_PER_DAY")
    query_feed_max_bytes_per_day = (
        PiholeConfig().query_feed_max_bytes_per_day if raw_qf_bytes is None else int(raw_qf_bytes)
    )

    return PiholeConfig(
        base_url=base_url,
        host_lan_ip=host_lan_ip,
        dns_host=dns_host,
        dns_port=dns_port,
        direct_dns_host=direct_dns_host,
        direct_dns_port=direct_dns_port,
        stream_query_feed_enabled=stream_query_feed_enabled,
        query_feed_max_bytes_per_day=query_feed_max_bytes_per_day,
    )


@dataclass(frozen=True, slots=True)
class PiholeUnboundConfig:
    """unbound-control access config (STAGE-006-003). Env-only.

    ``container`` is the Docker container that runs unbound (and into which the
    access layer execs ``unbound-control stats_noreset``). On this homelab Pi-hole
    and unbound share one container, ``pihole-unbound``.
    """

    container: str = "pihole-unbound"


def load_pihole_unbound_config() -> PiholeUnboundConfig:
    """Load the unbound-control container name from env.

    ``HOMELAB_MONITOR_PIHOLE_UNBOUND_CONTAINER`` -> ``container`` (default
    ``pihole-unbound`` when unset/empty).
    """
    container = os.environ.get(
        "HOMELAB_MONITOR_PIHOLE_UNBOUND_CONTAINER", PiholeUnboundConfig().container
    )
    return PiholeUnboundConfig(container=container)


@dataclass(frozen=True, slots=True)
class SynologyConfig:
    """Synology DSM connection config (STAGE-008-001). Env-only.

    ``base_url`` is the DSM HTTPS base URL (no trailing slash; the loader strips it).
    DSM serves a self-signed cert (``CN=synology``), so the lifespan builds a
    DEDICATED ``verify=False`` httpx client for it (mirrors Unifi). ``account`` is
    the DSM service-account NAME (``homelab-monitor`` — an admin account, recon-
    required; observe-only mitigates the exposure). The account name is NOT a secret
    so it lives here; the password lives in the secret store under
    ``synology_dsm_password`` and is resolved at login time via ``SYNO.API.Auth``.
    """

    base_url: str = "https://192.168.2.4:5001"
    account: str = "homelab-monitor"


def load_synology_config() -> SynologyConfig:
    """Load Synology DSM connection config from env.

    ``HOMELAB_MONITOR_SYNOLOGY_URL`` -> ``base_url`` (trailing slash stripped so
    client path concatenation is unambiguous; default ``https://192.168.2.4:5001``
    — the verified DSM host IP — when unset). ``HOMELAB_MONITOR_SYNOLOGY_ACCOUNT``
    -> ``account`` (default ``homelab-monitor``; falls back to the default when unset
    or blank).
    """
    raw_url = os.environ.get("HOMELAB_MONITOR_SYNOLOGY_URL")
    base_url = SynologyConfig().base_url if raw_url is None else raw_url.rstrip("/")
    raw_account = os.environ.get("HOMELAB_MONITOR_SYNOLOGY_ACCOUNT")
    account = (
        SynologyConfig().account
        if raw_account is None or not raw_account.strip()
        else raw_account.strip()
    )
    return SynologyConfig(base_url=base_url, account=account)


# Built-in per-family cardinality caps (STAGE-005-006).
# A typical large HA deployment emits ~1906 entity-family series; 2500 gives
# comfortable headroom while still bounding runaway growth.
# Users can override individual entries via cardinality_caps.families in YAML.
_DEFAULT_CARDINALITY_FAMILIES: dict[str, int] = {
    "homelab_ha_entity_available": 2500,
    "homelab_ha_entity_last_changed_seconds": 2500,
    # ~106 real update entities observed on this homelab + headroom.
    "homelab_ha_update_available": 150,
    # STAGE-007-004 — Unifi per-client/DPI metric-family caps. CONFIG only here;
    # the cap is APPLIED in STAGE-007-008 (per-client stats: cap_for("unifi_client_stats"))
    # and STAGE-007-009 (DPI: cap_for("unifi_dpi")). The identity-upsert helper applies
    # NO cap — the unifi_clients registry is the complete canonical inventory
    # (a home /24 is ~85 clients, far below any cap). These bound only the per-client
    # METRIC-series cardinality in Prometheus.
    # ~85 home clients x a few stat series each → 200 headroom.
    "unifi_client_stats": 200,
    # DPI top-N clients x top-N apps → 100.
    "unifi_dpi": 100,
    # STAGE-006-012 — Pi-hole per-client & top-domain metric-family caps.
    # ~85 home clients + headroom; top-domain lists are small but capped defensively.
    "pihole_client_queries": 50,
    "pihole_top_domains": 50,
}


@dataclass(frozen=True, slots=True)
class CardinalityCapsConfig:
    """Per-metric-family cardinality caps (STAGE-005-004).

    A "family" is a metric name (e.g. ``homelab_ha_entity_available``). A
    collector feeds that family's candidate series through a
    :class:`~homelab_monitor.kernel.metrics.cardinality.CardinalityCapper`
    built with ``cap_for(family)`` to bound how many distinct label-sets it
    emits per tick. ``default`` applies to any family without an explicit
    ``families`` entry.

    Config is OPT-IN at the collector level — nothing enforces a cap unless a
    collector explicitly wires the capper into its ``run()``. The cap is a per-
    tick survivor budget, not a hard registry limit.
    """

    default: int = 500
    families: Mapping[str, int] = field(default_factory=lambda: dict(_DEFAULT_CARDINALITY_FAMILIES))

    def cap_for(self, family: str) -> int:
        """Return the configured cap for ``family``, or ``default`` if unset."""
        return self.families.get(family, self.default)


_CARDINALITY_CAPS_KEY = "cardinality_caps"


def load_cardinality_caps_config() -> CardinalityCapsConfig:
    """Load per-family cardinality caps from YAML + env.

    Sources, in priority order (later overrides earlier):
      1. Hard-coded ``default`` (500) in :class:`CardinalityCapsConfig`.
      2. Built-in per-family caps from ``_DEFAULT_CARDINALITY_FAMILIES``
         (e.g. HA entity families default to 2500).
      3. ``HOMELAB_MONITOR_CONFIG`` ``cardinality_caps`` section:
         ``cardinality_caps.default`` (int) and ``cardinality_caps.families``
         (mapping of family-name -> int cap).
      4. ``HOMELAB_MONITOR_CARDINALITY_CAP_DEFAULT`` env (overrides ``default`` only;
         per-family entries from YAML are kept).

    Returns:
        CardinalityCapsConfig: validated configuration.

    Raises:
        ValueError: if the YAML root, the ``cardinality_caps`` section, or its
            ``families`` sub-mapping is not a mapping, or a cap value is non-integer.
        yaml.YAMLError: if the YAML file exists but is malformed.
    """
    config_path = Path(os.environ.get("HOMELAB_MONITOR_CONFIG", _DEFAULT_CONFIG_PATH))

    defaults = CardinalityCapsConfig()
    default_cap = defaults.default
    families: dict[str, int] = dict(_DEFAULT_CARDINALITY_FAMILIES)

    if config_path.is_file():
        with config_path.open(encoding="utf-8") as f:
            raw_obj: object = yaml.safe_load(f) or {}
        if not isinstance(raw_obj, dict):
            msg = f"config root must be a mapping, got {type(raw_obj).__name__}"
            raise ValueError(msg)
        raw = cast(dict[str, Any], raw_obj)
        section_obj: object = raw.get(_CARDINALITY_CAPS_KEY) or {}
        if not isinstance(section_obj, dict):
            msg = f"{_CARDINALITY_CAPS_KEY} must be a mapping, got {type(section_obj).__name__}"
            raise ValueError(msg)
        section = cast(dict[str, Any], section_obj)
        default_cap = _coerce_int(section, "default", default_cap)
        yaml_families = _load_cardinality_families(section)
        families = {**_DEFAULT_CARDINALITY_FAMILIES, **yaml_families}

    env_default = os.environ.get("HOMELAB_MONITOR_CARDINALITY_CAP_DEFAULT")
    if env_default is not None:
        default_cap = int(env_default)

    return CardinalityCapsConfig(default=default_cap, families=families)


def _load_cardinality_families(section: dict[str, Any]) -> dict[str, int]:
    """Parse cardinality_caps.families; empty dict when absent or null."""
    families_obj: object = section.get("families") or {}
    if not isinstance(families_obj, dict):
        msg = (
            f"{_CARDINALITY_CAPS_KEY}.families must be a mapping, got {type(families_obj).__name__}"
        )
        raise ValueError(msg)
    families_raw = cast(dict[str, Any], families_obj)
    result: dict[str, int] = {}
    for key, value_obj in families_raw.items():
        if not key:
            msg = f"{_CARDINALITY_CAPS_KEY}.families key must be a non-empty string, got {key!r}"
            raise ValueError(msg)
        if isinstance(value_obj, bool) or not isinstance(value_obj, (int, float, str)):
            msg = f"{_CARDINALITY_CAPS_KEY}.families[{key!r}] must be an integer, got {value_obj!r}"
            raise ValueError(msg)
        try:
            result[key] = int(value_obj)
        except (TypeError, ValueError) as exc:
            msg = f"{_CARDINALITY_CAPS_KEY}.families[{key!r}] must be an integer, got {value_obj!r}"
            raise ValueError(msg) from exc
    return result


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
    """Per-service error-rate override (future stage; v1 unused)."""

    service: str
    static_floor: float | None = None
    multiplier: float | None = None


@dataclass(frozen=True, slots=True)
class SeverityFloor:
    """Per-service severity-escalation floor override.

    Reserved for a future per-service-override stage; parsed but UNUSED in v1.
    """

    service: str
    floor: str


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
    """Operator-tunable error-rate config (logs.error_patterns /
    logs.error_rate_overrides / logs.severity_escalation).

    ``error_patterns`` — folded into the collector's single LogsQL query
    (OR'd with the severity union). Defaults to DEFAULT_ERROR_PATTERNS.
    ``error_rate_overrides`` — per-service tuning, reserved for future
    per-service-override stage (parsed here but UNUSED in v1).
    ``severity_escalation_excluded_services`` — services excluded from the
    CriticalLogLine alert, reserved for future per-service-override stage
    (parsed but UNUSED in v1).
    ``severity_escalation_floors`` — per-service minimum severity floors,
    reserved for future per-service-override stage (parsed but UNUSED in v1).
    """

    error_patterns: tuple[ErrorPattern, ...] = DEFAULT_ERROR_PATTERNS
    error_rate_overrides: tuple[ErrorRateOverride, ...] = ()
    severity_escalation_excluded_services: tuple[str, ...] = ()
    severity_escalation_floors: tuple[SeverityFloor, ...] = ()


_LOGS_KEY = "logs"
_ERROR_PATTERNS_SUBKEY = "error_patterns"
_ERROR_RATE_OVERRIDES_SUBKEY = "error_rate_overrides"
_SEVERITY_ESCALATION_SUBKEY = "severity_escalation"
_SEVERITY_ESCALATION_EXCLUDED_SERVICES_SUBKEY = "excluded_services"
_SEVERITY_ESCALATION_FLOORS_SUBKEY = "severity_floors"


def load_logs_config() -> LogsConfig:
    """Load error-rate config from YAML ``logs.error_patterns`` / ``logs.error_rate_overrides``.

    Sources:
      - ``logs.error_patterns`` present → parse + validate that list.
      - ``logs`` absent OR ``logs.error_patterns`` absent → DEFAULT_ERROR_PATTERNS.
      - ``logs.error_patterns: []`` (explicit empty list) → empty tuple
        (NOT defaults; mirrors redact precedent but for patterns this means
        "severity union only").
      - ``logs.error_patterns:`` (null value) → DEFAULT_ERROR_PATTERNS.
      - ``logs.error_rate_overrides`` parsed (UNUSED in v1; reserved for
        future per-service-override stage).

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
    sev_exc, sev_floors = _load_severity_escalation(logs)
    return LogsConfig(
        error_patterns=error_patterns,
        error_rate_overrides=error_rate_overrides,
        severity_escalation_excluded_services=sev_exc,
        severity_escalation_floors=sev_floors,
    )


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
    """Parse logs.error_rate_overrides (future stage; v1 carries it unused)."""
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


def _load_severity_escalation(
    logs: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[SeverityFloor, ...]]:
    """Parse logs.severity_escalation.excluded_services and .severity_floors.

    Reserved for future per-service-override stage; parsed but UNUSED in v1.
    Mirrors _load_error_rate_overrides.

    Returns:
        (excluded_services, severity_floors) — both empty tuples when the
        sub-section is absent or null.

    Raises:
        ValueError: severity_escalation not a mapping; excluded_services not a list
            or contains a non-string entry; severity_floors not a list, contains a
            non-mapping entry, or an entry is missing/has a non-string 'service' or
            'floor'.
    """
    if _SEVERITY_ESCALATION_SUBKEY not in logs:
        return (), ()
    sev_obj: object = logs.get(_SEVERITY_ESCALATION_SUBKEY)
    if sev_obj is None:
        return (), ()
    if not isinstance(sev_obj, dict):
        msg = (
            f"{_LOGS_KEY}.{_SEVERITY_ESCALATION_SUBKEY} must be a mapping, "
            f"got {type(sev_obj).__name__}"
        )
        raise ValueError(msg)
    sev = cast(dict[str, Any], sev_obj)

    excluded = _load_severity_escalation_excluded(sev)
    floors = _load_severity_escalation_floors(sev)
    return excluded, floors


def _load_severity_escalation_excluded(sev: dict[str, Any]) -> tuple[str, ...]:
    """Parse logs.severity_escalation.excluded_services."""
    subkey = _SEVERITY_ESCALATION_EXCLUDED_SERVICES_SUBKEY
    if subkey not in sev:
        return ()
    raw_obj: object = sev.get(subkey)
    if raw_obj is None:
        return ()
    if not isinstance(raw_obj, list):
        msg = (
            f"{_LOGS_KEY}.{_SEVERITY_ESCALATION_SUBKEY}.{subkey} must be a list, "
            f"got {type(raw_obj).__name__}"
        )
        raise ValueError(msg)
    raw_list = cast(list[object], raw_obj)
    result: list[str] = []
    for idx, entry_obj in enumerate(raw_list):
        if not isinstance(entry_obj, str) or not entry_obj:
            msg = (
                f"{_LOGS_KEY}.{_SEVERITY_ESCALATION_SUBKEY}.{subkey}[{idx}] "
                f"must be a non-empty string, got {entry_obj!r}"
            )
            raise ValueError(msg)
        result.append(entry_obj)
    return tuple(result)


def _load_severity_escalation_floors(sev: dict[str, Any]) -> tuple[SeverityFloor, ...]:
    """Parse logs.severity_escalation.severity_floors."""
    subkey = _SEVERITY_ESCALATION_FLOORS_SUBKEY
    if subkey not in sev:
        return ()
    raw_obj: object = sev.get(subkey)
    if raw_obj is None:
        return ()
    if not isinstance(raw_obj, list):
        msg = (
            f"{_LOGS_KEY}.{_SEVERITY_ESCALATION_SUBKEY}.{subkey} must be a list, "
            f"got {type(raw_obj).__name__}"
        )
        raise ValueError(msg)
    raw_list = cast(list[object], raw_obj)
    floors: list[SeverityFloor] = []
    for idx, entry_obj in enumerate(raw_list):
        if not isinstance(entry_obj, dict):
            msg = (
                f"{_LOGS_KEY}.{_SEVERITY_ESCALATION_SUBKEY}.{subkey}[{idx}] "
                f"must be a mapping, got {type(entry_obj).__name__}"
            )
            raise ValueError(msg)
        entry = cast(dict[str, Any], entry_obj)
        service = entry.get("service")
        floor = entry.get("floor")
        for field_name, value in (("service", service), ("floor", floor)):
            if not isinstance(value, str) or not value:
                msg = (
                    f"{_LOGS_KEY}.{_SEVERITY_ESCALATION_SUBKEY}.{subkey}[{idx}] "
                    f"field {field_name!r} must be a non-empty string, got {value!r}"
                )
                raise ValueError(msg)
        floors.append(SeverityFloor(service=cast(str, service), floor=cast(str, floor)))
    return tuple(floors)


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
    # STAGE-007-016: UDM/Unifi token redaction. No lookarounds (Rust regex crate).
    RedactPattern(
        name="udm_bearer",
        pattern=r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._-]{20,}",
        replacement="Authorization: Bearer [REDACTED]",
    ),
    RedactPattern(
        name="udm_session",
        pattern=(
            r"(?i)(unifises|token|x-csrf-token)"
            r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._-]{16,}"
        ),
        replacement="${1}=[REDACTED]",
    ),
    # STAGE-007-016: UDM mcad authkey (cleartext hex on the wire).
    # Fixed replacement (no `${1}` group): cleaner than echoing the capture, and
    # avoids relying on render.py's "$" -> "$$" env-escaping interacting with a
    # literal `${1}` in the generated VRL replace().
    RedactPattern(
        name="udm_authkey",
        pattern=r"(?i)authkey[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9]{16,}",
        replacement="authkey=[REDACTED]",
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


# ---------------------------------------------------------------------------
# STAGE-005-013: HA history/anomaly z-score collector tunables.
# Mirrors the CronAnomalyConfig / NewSignatureConfig env-loader precedent. The
# z-score collector reads this INSIDE run() each tick (like ha_battery reads
# load_cardinality_caps_config), so an operator edit is picked up without a
# restart.
# ---------------------------------------------------------------------------

#: Default eligible device classes for z-score scoring. A sensor qualifies only
#: when state_class == "measurement" AND device_class is in this set (plus the
#: extra_entity_ids force-include / excluded_entity_ids force-exclude overrides).
#: Chosen to cover the common numeric-trend sensors on a homelab HA install while
#: excluding enum/diagnostic classes that have no meaningful rolling z-score.
DEFAULT_ZSCORE_DEVICE_CLASSES: frozenset[str] = frozenset(
    {
        "temperature",
        "humidity",
        "pressure",
        "power",
        "energy",
        "current",
        "voltage",
        "carbon_dioxide",
        "pm25",
        "illuminance",
        "signal_strength",
    }
)


@dataclass(frozen=True, slots=True)
class AnomalyZscoreConfig:
    """Tunables for HaAnomalyZscoreCollector (STAGE-005-013).

    ``window_samples`` — per-entity rolling-window length (deque maxlen). Default
    48 ≈ 4h at the locked 5-minute cadence. ``min_samples`` — minimum values in a
    window before any z-score is emitted (cold-start warmup gate). Default 12 ≈ 1h.
    ``zero_variance_epsilon`` — population-std floor; a window whose pstdev is below
    this is treated as flat and emits NO series (a z-score over zero variance is a
    division-by-(near)-zero blow-up, not a signal). ``device_classes`` — the
    eligible device-class set (state_class must additionally be "measurement").
    ``excluded_entity_ids`` — force-exclude (wins over everything). ``extra_entity_ids``
    — force-include even when the device-class heuristic would reject (the value must
    still parse to a float to contribute).

    All numeric tunables clamped to safe floors so an operator 0/negative can't
    make the collector degenerate.
    """

    window_samples: int = 48
    min_samples: int = 12
    zero_variance_epsilon: float = 1e-9
    device_classes: frozenset[str] = DEFAULT_ZSCORE_DEVICE_CLASSES
    excluded_entity_ids: frozenset[str] = frozenset()
    extra_entity_ids: frozenset[str] = frozenset()


def load_anomaly_zscore_config() -> AnomalyZscoreConfig:
    """Load HaAnomalyZscoreCollector tunables from env (HOMELAB_MONITOR_HA_ZSCORE_*).

    HOMELAB_MONITOR_HA_ZSCORE_WINDOW_SAMPLES   -> window_samples (clamped >= 1).
    HOMELAB_MONITOR_HA_ZSCORE_MIN_SAMPLES      -> min_samples (clamped >= 2;
        pstdev needs >= 2 values to be meaningful, mirroring load_cron_anomaly_config's
        min_history >= 2 clamp). Also clamped <= window_samples so the gate is reachable.
    HOMELAB_MONITOR_HA_ZSCORE_EPSILON          -> zero_variance_epsilon (clamped > 0;
        a 0 epsilon would let a flat window divide by zero).
    HOMELAB_MONITOR_HA_ZSCORE_DEVICE_CLASSES   -> comma-separated device classes,
        lowercased + stripped; empty result falls back to DEFAULT_ZSCORE_DEVICE_CLASSES.
    HOMELAB_MONITOR_HA_ZSCORE_EXCLUDED_ENTITY_IDS -> comma-separated entity_ids.
    HOMELAB_MONITOR_HA_ZSCORE_EXTRA_ENTITY_IDS    -> comma-separated entity_ids.
    """
    defaults = AnomalyZscoreConfig()
    window_samples = defaults.window_samples
    min_samples = defaults.min_samples
    epsilon = defaults.zero_variance_epsilon
    device_classes = defaults.device_classes
    excluded = defaults.excluded_entity_ids
    extra = defaults.extra_entity_ids

    raw_window = os.environ.get("HOMELAB_MONITOR_HA_ZSCORE_WINDOW_SAMPLES")
    if raw_window is not None:
        window_samples = int(raw_window)
    raw_min = os.environ.get("HOMELAB_MONITOR_HA_ZSCORE_MIN_SAMPLES")
    if raw_min is not None:
        min_samples = int(raw_min)
    raw_eps = os.environ.get("HOMELAB_MONITOR_HA_ZSCORE_EPSILON")
    if raw_eps is not None:
        epsilon = float(raw_eps)
    raw_dc = os.environ.get("HOMELAB_MONITOR_HA_ZSCORE_DEVICE_CLASSES")
    if raw_dc is not None:
        parsed_dc = frozenset(s.strip().lower() for s in raw_dc.split(",") if s.strip())
        if parsed_dc:
            device_classes = parsed_dc
    raw_excl = os.environ.get("HOMELAB_MONITOR_HA_ZSCORE_EXCLUDED_ENTITY_IDS")
    if raw_excl is not None:
        excluded = frozenset(s.strip() for s in raw_excl.split(",") if s.strip())
    raw_extra = os.environ.get("HOMELAB_MONITOR_HA_ZSCORE_EXTRA_ENTITY_IDS")
    if raw_extra is not None:
        extra = frozenset(s.strip() for s in raw_extra.split(",") if s.strip())

    window_samples = max(window_samples, 1)
    min_samples = max(min_samples, 2)
    min_samples = min(min_samples, window_samples)
    epsilon = max(epsilon, 1e-12)

    return AnomalyZscoreConfig(
        window_samples=window_samples,
        min_samples=min_samples,
        zero_variance_epsilon=epsilon,
        device_classes=device_classes,
        excluded_entity_ids=excluded,
        extra_entity_ids=extra,
    )


@dataclass(frozen=True, slots=True)
class HaRegistryConfig:
    """Tunables for the HA entity-registry cache (STAGE-005-037).

    Controls whether registry-driven exclusion is active and which classes of
    entity are excluded from ``homelab_ha_entity_available`` /
    ``homelab_ha_entity_last_changed_seconds`` and from z-score eligibility.

    - ``enabled`` — master switch; when False the cache loop is not started and
      no exclusion occurs (fail-open).
    - ``exclude_disabled`` — drop entities whose registry ``disabled_by`` is set.
    - ``exclude_hidden`` — drop entities whose registry ``hidden_by`` is set.
    - ``exclude_categories`` — drop entities whose registry ``entity_category``
      is in this set (lowercased; e.g. ``{"diagnostic", "config"}``).
    - ``refresh_seconds`` — registry re-fetch interval (clamped >= 60).
    """

    enabled: bool = True
    exclude_disabled: bool = True
    exclude_hidden: bool = True
    exclude_categories: frozenset[str] = frozenset()
    refresh_seconds: int = 600


def load_ha_registry_config() -> HaRegistryConfig:
    """Load HaRegistryConfig from env (HOMELAB_MONITOR_HA_REGISTRY_*).

    - ``HOMELAB_MONITOR_HA_REGISTRY_ENABLED`` (bool) -> ``enabled``.
    - ``HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_DISABLED`` (bool) -> ``exclude_disabled``.
    - ``HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_HIDDEN`` (bool) -> ``exclude_hidden``.
    - ``HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_CATEGORIES`` (comma-separated,
      lowercased/stripped) -> ``exclude_categories`` (empty -> empty frozenset).
    - ``HOMELAB_MONITOR_HA_REGISTRY_REFRESH_SECONDS`` (int, clamped >= 60).
    """
    defaults = HaRegistryConfig()
    enabled = defaults.enabled
    exclude_disabled = defaults.exclude_disabled
    exclude_hidden = defaults.exclude_hidden
    exclude_categories = defaults.exclude_categories
    refresh_seconds = defaults.refresh_seconds

    raw_enabled = os.environ.get("HOMELAB_MONITOR_HA_REGISTRY_ENABLED")
    if raw_enabled is not None:
        enabled = raw_enabled.strip().lower() in ("1", "true", "yes")
    raw_disabled = os.environ.get("HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_DISABLED")
    if raw_disabled is not None:
        exclude_disabled = raw_disabled.strip().lower() in ("1", "true", "yes")
    raw_hidden = os.environ.get("HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_HIDDEN")
    if raw_hidden is not None:
        exclude_hidden = raw_hidden.strip().lower() in ("1", "true", "yes")
    raw_cats = os.environ.get("HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_CATEGORIES")
    if raw_cats is not None:
        exclude_categories = frozenset(s.strip().lower() for s in raw_cats.split(",") if s.strip())
    raw_refresh = os.environ.get("HOMELAB_MONITOR_HA_REGISTRY_REFRESH_SECONDS")
    if raw_refresh is not None:
        refresh_seconds = int(raw_refresh)

    refresh_seconds = max(refresh_seconds, 60)

    return HaRegistryConfig(
        enabled=enabled,
        exclude_disabled=exclude_disabled,
        exclude_hidden=exclude_hidden,
        exclude_categories=exclude_categories,
        refresh_seconds=refresh_seconds,
    )


@dataclass(frozen=True, slots=True)
class DockerConfig:
    """Master switch for the Docker plugin (DockerDiscoverer + DockerSocketCollector).

    When ``enabled`` is False, the lifespan skips registering the Docker
    collectors and never constructs a ``DockerSocketClient``, so the instance
    does no container monitoring and never touches the docker socket. The docker
    router endpoints already return 503 defensively when the socket client is
    None, and the auto-degrading consumers (ProbeSupervisor, ImageUpdateCollector,
    LocalBuildUpdateCollector) receive ``None`` and degrade gracefully.

    - ``enabled`` — master switch; defaults True so unset env reproduces today's
      behavior exactly (fail-on for the primary instance).
    """

    enabled: bool = True


def load_docker_config() -> DockerConfig:
    """Load DockerConfig from env (HOMELAB_MONITOR_DOCKER_*).

    - ``HOMELAB_MONITOR_DOCKER_ENABLED`` (bool) -> ``enabled``. Truthy values are
      ``1``/``true``/``yes`` (lowercased), matching the DrainConfig convention.
      Unset leaves the default (True).
    """
    defaults = DockerConfig()
    enabled = defaults.enabled

    raw_enabled = os.environ.get("HOMELAB_MONITOR_DOCKER_ENABLED")
    if raw_enabled is not None:
        enabled = raw_enabled.strip().lower() in ("1", "true", "yes")

    return DockerConfig(enabled=enabled)


# ---------------------------------------------------------------------------
# STAGE-005-016: HA safety binary-sensor + HA temp/humidity value collectors.
# Both use the established frozen-dataclass + load_* env-loader pattern (read
# inside run()). Each holds only its device-class allow-set, env-overridable via
# a comma-separated list (lowercased + stripped; empty result keeps the default).
# ---------------------------------------------------------------------------

#: Default safety-relevant binary_sensor device classes for HaSafetySensorsCollector.
#: Life-safety (smoke/gas/carbon_monoxide/moisture) + contact (door/window/opening).
DEFAULT_SAFETY_DEVICE_CLASSES: frozenset[str] = frozenset(
    {"smoke", "gas", "carbon_monoxide", "moisture", "door", "window", "opening"}
)


@dataclass(frozen=True, slots=True)
class SafetySensorsConfig:
    """Tunables for HaSafetySensorsCollector (STAGE-005-016).

    ``device_classes`` — the binary_sensor device-class allow-set the collector
    emits ``homelab_ha_binary_sensor_on`` for. Env-overridable; an empty override
    falls back to ``DEFAULT_SAFETY_DEVICE_CLASSES``.
    """

    device_classes: frozenset[str] = DEFAULT_SAFETY_DEVICE_CLASSES


def load_safety_sensors_config() -> SafetySensorsConfig:
    """Load HaSafetySensorsCollector tunables from env.

    HOMELAB_MONITOR_HA_SAFETY_DEVICE_CLASSES -> comma-separated device classes,
        lowercased + stripped; empty result falls back to the default set.
    """
    defaults = SafetySensorsConfig()
    device_classes = defaults.device_classes
    raw_dc = os.environ.get("HOMELAB_MONITOR_HA_SAFETY_DEVICE_CLASSES")
    if raw_dc is not None:
        parsed_dc = frozenset(s.strip().lower() for s in raw_dc.split(",") if s.strip())
        if parsed_dc:
            device_classes = parsed_dc
    return SafetySensorsConfig(device_classes=device_classes)


#: Default device classes for HaSensorValueCollector (temp/humidity raw values).
DEFAULT_SENSOR_VALUE_DEVICE_CLASSES: frozenset[str] = frozenset({"temperature", "humidity"})


@dataclass(frozen=True, slots=True)
class SensorValueConfig:
    """Tunables for HaSensorValueCollector (STAGE-005-016).

    ``device_classes`` — the device-class allow-set the collector emits
    ``homelab_ha_sensor_value`` for (scoped by device_class, NOT domain).
    Env-overridable; an empty override falls back to the default set.
    """

    device_classes: frozenset[str] = DEFAULT_SENSOR_VALUE_DEVICE_CLASSES


def load_sensor_value_config() -> SensorValueConfig:
    """Load HaSensorValueCollector tunables from env.

    HOMELAB_MONITOR_HA_SENSOR_VALUE_DEVICE_CLASSES -> comma-separated device
        classes, lowercased + stripped; empty result falls back to the default.
    """
    defaults = SensorValueConfig()
    device_classes = defaults.device_classes
    raw_dc = os.environ.get("HOMELAB_MONITOR_HA_SENSOR_VALUE_DEVICE_CLASSES")
    if raw_dc is not None:
        parsed_dc = frozenset(s.strip().lower() for s in raw_dc.split(",") if s.strip())
        if parsed_dc:
            device_classes = parsed_dc
    return SensorValueConfig(device_classes=device_classes)


__all__ = [
    "DEFAULT_ERROR_PATTERNS",
    "DEFAULT_REDACT_PATTERNS",
    "DEFAULT_SAFETY_DEVICE_CLASSES",
    "DEFAULT_SENSOR_VALUE_DEVICE_CLASSES",
    "DEFAULT_ZSCORE_DEVICE_CLASSES",
    "AnomalyZscoreConfig",
    "CardinalityCapsConfig",
    "CrashLogConfig",
    "CronAnomalyConfig",
    "CronRunReconcilerConfig",
    "DiskBudgetConfig",
    "DockerConfig",
    "DrainConfig",
    "ErrorPattern",
    "ErrorRateOverride",
    "HaConfig",
    "HaRegistryConfig",
    "HealthcheckLogConfig",
    "LogStreamBudgetConfig",
    "LogsConfig",
    "NewSignatureConfig",
    "RedactPattern",
    "SafetySensorsConfig",
    "SensorValueConfig",
    "SeverityFloor",
    "SilenceDetectionConfig",
    "TailConfig",
    "VlDiskWarningConfig",
    "VlHealthConfig",
    "VlQueryLimits",
    "get_public_url",
    "load_anomaly_zscore_config",
    "load_cardinality_caps_config",
    "load_crash_log_config",
    "load_cron_anomaly_config",
    "load_cron_run_reconciler_config",
    "load_disk_budget_config",
    "load_docker_config",
    "load_drain_config",
    "load_ha_config",
    "load_ha_registry_config",
    "load_healthcheck_log_config",
    "load_log_stream_budget_config",
    "load_logs_config",
    "load_new_signature_config",
    "load_redact_patterns",
    "load_safety_sensors_config",
    "load_sensor_value_config",
    "load_silence_detection_config",
    "load_tail_config",
    "load_vl_disk_warning_config",
    "load_vl_health_config",
    "load_vl_query_limits",
    "load_vl_retention_days",
]
