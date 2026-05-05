"""Subprocess plugin manifest schema.

A plugin manifest is a YAML file at the root of a subprocess plugin's
directory (`plugin.yaml`). It declares everything the runner needs to
spawn and supervise the plugin: command, intervals, trust level, env
vars, secrets allowlist, and concurrency group.

Schema version 1 only; future versions add fields, never break v1.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from homelab_monitor.kernel.plugins.types import PLUGIN_NAME_PATTERN, TrustLevel

# Single-unit only by design; mixed units like '1h30m' rejected
# (see docs/plugins/subprocess.md).
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smh])\s*$")
_MIN_INTERVAL = timedelta(seconds=5)


def _parse_duration(value: str | int | float | timedelta) -> timedelta:
    """Coerce a manifest duration field into a timedelta.

    Accepts:
      * `timedelta` (passthrough)
      * `int` / `float` — interpreted as seconds
      * `str` matching `^\\d+[smh]$` — e.g., "60s", "5m", "1h"
    """
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    if not isinstance(value, str):  # type: ignore[unreachable]  # pragma: no cover
        # Defensive: pydantic validator delivers Any; runtime check rejects
        # non-str/int/float/timedelta inputs.
        msg = f"invalid duration type for {value!r}: {type(value).__name__}"
        raise TypeError(msg)
    # value is str by elimination
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"invalid duration {value!r}: expected '<int>s|m|h' or int seconds")
    n = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return timedelta(seconds=n)
    if unit == "m":
        return timedelta(minutes=n)
    # unit == "h" — regex restricts to s|m|h
    return timedelta(hours=n)


class SubprocessManifest(BaseModel):
    """Validated subprocess plugin manifest.

    Loaded from `plugin.yaml`. The runner uses this to construct env,
    spawn arguments, and trust-tier dispatch.
    """

    model_config = ConfigDict(extra="forbid")

    manifest: Literal[1] = Field(default=1, description="Schema version")
    name: str = Field(pattern=PLUGIN_NAME_PATTERN)
    language: str = Field(default="bash", description="Informational only")
    command: list[str] = Field(min_length=1, description="argv list relative to manifest dir")
    interval: timedelta
    timeout: timedelta
    concurrency_group: str = "default"
    trust_level: Literal[TrustLevel.TRUSTED, TrustLevel.UNTRUSTED] = TrustLevel.TRUSTED
    env: dict[str, str] = Field(default_factory=lambda: {})
    secrets: list[str] = Field(default_factory=lambda: [])
    workdir: str | None = None  # None = manifest's parent directory

    @field_validator("interval", "timeout", mode="before")
    @classmethod
    def _coerce_duration(cls, v: object) -> timedelta:
        return _parse_duration(v)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def _validate_intervals(self) -> SubprocessManifest:
        if self.interval < _MIN_INTERVAL:
            raise ValueError(
                f"interval must be >= {_MIN_INTERVAL.total_seconds()}s, "
                f"got {self.interval.total_seconds()}s"
            )
        if self.timeout >= self.interval:
            raise ValueError(
                f"timeout ({self.timeout.total_seconds()}s) must be less than "
                f"interval ({self.interval.total_seconds()}s)"
            )
        return self

    @classmethod
    def load_from_path(cls, manifest_path: Path) -> SubprocessManifest:
        """Read plugin.yaml from disk and return a validated instance."""
        with manifest_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise ValueError(
                f"plugin manifest at {manifest_path} must be a YAML mapping, "
                f"got {type(data).__name__}"
            )
        return cls.model_validate(data)


__all__ = ["SubprocessManifest"]
