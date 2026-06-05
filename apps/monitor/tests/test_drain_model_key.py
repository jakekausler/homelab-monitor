"""Tests for default_model_key + custom model_key_fn (STAGE-004-025)."""

from __future__ import annotations

import hashlib
from typing import Any

from homelab_monitor.kernel.cron.log_match import canonical_log_key
from homelab_monitor.kernel.logs.drain_engine import default_model_key
from homelab_monitor.kernel.logs.models import LogLine


def _line(*, service: str | None, fields: dict[str, Any] | None = None) -> LogLine:
    return LogLine(
        timestamp="2026-06-05T12:00:00Z",
        message="msg",
        stream="stdout",
        severity="info",
        host="h",
        service=service,
        fields=fields or {},
    )


def test_default_model_key_service_bucket() -> None:
    assert default_model_key(_line(service="pihole")) == "pihole"


def test_default_model_key_none_service() -> None:
    assert default_model_key(_line(service=None)) == "_unknown"


def test_default_model_key_hmrun_with_command() -> None:
    cmd = "/usr/bin/backup.sh --full"
    key = default_model_key(_line(service="hmrun", fields={"command": cmd}))
    expected = hashlib.sha256(canonical_log_key(cmd).encode("utf-8")).hexdigest()[:16]
    assert key == f"cron:{expected}"


def test_default_model_key_hmrun_missing_command() -> None:
    assert default_model_key(_line(service="hmrun", fields={})) == "cron:unknown"


def test_default_model_key_hmrun_empty_command() -> None:
    assert default_model_key(_line(service="hmrun", fields={"command": ""})) == "cron:unknown"


def test_default_model_key_hmrun_non_string_command() -> None:
    assert default_model_key(_line(service="hmrun", fields={"command": 123})) == "cron:unknown"
