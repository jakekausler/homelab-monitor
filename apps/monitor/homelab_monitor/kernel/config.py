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


__all__ = [
    "DiskBudgetConfig",
    "LogStreamBudgetConfig",
    "load_disk_budget_config",
    "load_log_stream_budget_config",
]
