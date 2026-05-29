"""Unit tests for the converged LogLine model + VictoriaLogs mapper.

STAGE-004-002. Covers every branch of _normalize_severity and
from_victorialogs_line for the 100% kernel coverage gate.
"""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.logs.models import (
    _normalize_severity,  # pyright: ignore[reportPrivateUsage]  -- testing the helper's branch matrix directly
    from_victorialogs_line,
)
from homelab_monitor.kernel.logs.victorialogs_client import VlLogLine


def _vl(**fields: str) -> VlLogLine:
    return VlLogLine(
        timestamp="2026-05-07T00:00:00+00:00",
        message="hello",
        stream="svc.host",
        fields=dict(fields),
    )


# ---------- _normalize_severity: numerics ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", "emergency"),
        ("1", "alert"),
        ("2", "critical"),
        ("3", "error"),
        ("4", "warn"),
        ("5", "notice"),
        ("6", "info"),
        ("7", "debug"),
    ],
)
def test_normalize_syslog_numerics(raw: str, expected: str) -> None:
    assert _normalize_severity(raw) == expected


# ---------- _normalize_severity: aliases ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("warning", "warn"),
        ("err", "error"),
        ("crit", "critical"),
        ("panic", "emergency"),
        ("emerg", "emergency"),
    ],
)
def test_normalize_aliases(raw: str, expected: str) -> None:
    assert _normalize_severity(raw) == expected


# ---------- _normalize_severity: canonical passthrough ----------


@pytest.mark.parametrize(
    "canonical",
    ["debug", "info", "notice", "warn", "error", "critical", "alert", "emergency"],
)
def test_normalize_canonical_passthrough(canonical: str) -> None:
    assert _normalize_severity(canonical) == canonical


# ---------- _normalize_severity: case-insensitivity ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ERROR", "error"),
        ("Warn", "warn"),
        ("WARNING", "warn"),
        ("Err", "error"),
    ],
)
def test_normalize_mixed_case(raw: str, expected: str) -> None:
    assert _normalize_severity(raw) == expected


# ---------- _normalize_severity: edge cases ----------


def test_normalize_unknown_defaults_to_info() -> None:
    assert _normalize_severity("weird") == "info"


def test_normalize_none_returns_none() -> None:
    assert _normalize_severity(None) is None


def test_normalize_empty_returns_none() -> None:
    assert _normalize_severity("") is None


def test_normalize_whitespace_returns_none() -> None:
    assert _normalize_severity("   ") is None


# ---------- from_victorialogs_line: severity promotion ----------


def test_mapper_promotes_severity() -> None:
    out = from_victorialogs_line(_vl(severity="warning"))
    assert out.severity == "warn"
    assert out.fields["severity_raw"] == "warning"


def test_mapper_no_severity_field_leaves_none_and_no_raw() -> None:
    out = from_victorialogs_line(_vl())
    assert out.severity is None
    assert "severity_raw" not in out.fields


def test_mapper_empty_severity_preserves_raw_but_normalizes_none() -> None:
    out = from_victorialogs_line(_vl(severity=""))
    assert out.severity is None
    assert out.fields["severity_raw"] == ""


# ---------- from_victorialogs_line: host extraction ----------


def test_mapper_host_primary() -> None:
    out = from_victorialogs_line(_vl(host="nas01"))
    assert out.host == "nas01"


def test_mapper_host_fallback_hostname() -> None:
    out = from_victorialogs_line(_vl(_HOSTNAME="nas01"))
    assert out.host == "nas01"


def test_mapper_host_missing_is_none() -> None:
    out = from_victorialogs_line(_vl())
    assert out.host is None


# ---------- from_victorialogs_line: service extraction ----------


def test_mapper_service_primary() -> None:
    out = from_victorialogs_line(_vl(service="plex"))
    assert out.service == "plex"


def test_mapper_service_fallback_syslog_identifier() -> None:
    out = from_victorialogs_line(_vl(SYSLOG_IDENTIFIER="plex"))
    assert out.service == "plex"


def test_mapper_service_missing_is_none() -> None:
    out = from_victorialogs_line(_vl())
    assert out.service is None


# ---------- from_victorialogs_line: non-mutation + serialization ----------


def test_mapper_does_not_mutate_input_fields() -> None:
    src = _vl(severity="error", host="h", service="s")
    original = dict(src.fields)
    _ = from_victorialogs_line(src)
    assert src.fields == original
    assert "severity_raw" not in src.fields


def test_mapper_preserves_full_fields_bag() -> None:
    out = from_victorialogs_line(_vl(severity="info", run_id="abc", custom="x"))
    assert out.fields["run_id"] == "abc"
    assert out.fields["custom"] == "x"


def test_logline_serializes_all_fields() -> None:
    out = from_victorialogs_line(_vl(severity="info", host="h", service="s"))
    dumped = out.model_dump()
    assert set(dumped) == {
        "timestamp",
        "message",
        "stream",
        "severity",
        "host",
        "service",
        "fields",
    }
    assert dumped["message"] == "hello"
    assert dumped["timestamp"] == "2026-05-07T00:00:00+00:00"
    assert dumped["stream"] == "svc.host"
