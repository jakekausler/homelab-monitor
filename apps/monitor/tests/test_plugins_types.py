"""Tests for enums, config validation, events discriminated union, and results."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from homelab_monitor.kernel.plugins.types import (
    AlertForwardEvent,
    CollectorConfig,
    CollectorEvent,
    CollectorResult,
    HeartbeatEvent,
    LogSignatureEvent,
    RunKind,
    SuggestionEvent,
    TrustLevel,
)

EVENT_ADAPTER: TypeAdapter[CollectorEvent] = TypeAdapter(CollectorEvent)

# Test constants
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 30
LOG_SIG_COUNT = 7
RESULT_METRICS = 3
RESULT_EVENT_COUNT = 2
RESULT_DURATION = 1.25


# --- enums ----------------------------------------------------------------------------------


def test_runkind_has_three_string_values() -> None:
    """RunKind exposes ASYNC/THREAD/PROCESS as string members."""
    assert RunKind.ASYNC == "async"
    assert RunKind.THREAD == "thread"
    assert RunKind.PROCESS == "process"
    assert {m.value for m in RunKind} == {"async", "thread", "process"}


def test_trustlevel_has_three_string_values() -> None:
    """TrustLevel exposes BUILTIN/TRUSTED/UNTRUSTED as string members."""
    assert TrustLevel.BUILTIN == "builtin"
    assert TrustLevel.TRUSTED == "trusted"
    assert TrustLevel.UNTRUSTED == "untrusted"
    assert {m.value for m in TrustLevel} == {"builtin", "trusted", "untrusted"}


# --- CollectorConfig ------------------------------------------------------------------------


@pytest.mark.parametrize("good", ["abc", "my-collector", "foo_bar", "host-psutil-001", "a" * 64])
def test_collector_config_accepts_valid_names(good: str) -> None:
    """Valid plugin names match `[a-z][a-z0-9_-]{2,63}`."""
    cfg = CollectorConfig(name=good)
    assert cfg.name == good


@pytest.mark.parametrize(
    "bad",
    [
        "My-Collector",  # uppercase
        "1abc",  # starts with digit
        "ab",  # too short (only 2 chars)
        "a" * 65,  # too long (65 chars)
        "foo bar",  # space
        "-foo",  # starts with hyphen
        "foo!",  # special char
        "",  # empty
    ],
)
def test_collector_config_rejects_invalid_names(bad: str) -> None:
    """Invalid plugin names raise ValidationError."""
    with pytest.raises(ValidationError):
        CollectorConfig(name=bad)


def test_collector_config_defaults() -> None:
    """interval_seconds=60, timeout_seconds=30, enabled=True."""
    cfg = CollectorConfig(name="foo")
    assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS
    assert cfg.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert cfg.enabled is True


def test_collector_config_rejects_extra_fields() -> None:
    """`extra="forbid"` rejects unknown keys."""
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate({"name": "foo", "unexpected": 1})


def test_collector_config_rejects_zero_interval() -> None:
    """interval_seconds must be >= 1."""
    with pytest.raises(ValidationError):
        CollectorConfig(name="foo", interval_seconds=0)


def test_collector_config_rejects_zero_timeout() -> None:
    """timeout_seconds must be >= 1."""
    with pytest.raises(ValidationError):
        CollectorConfig(name="foo", timeout_seconds=0)


# --- CollectorEvent (discriminated union) ---------------------------------------------------


def test_event_round_trip_suggestion() -> None:
    """SuggestionEvent round-trips through the discriminated union."""
    raw = {"kind": "suggestion", "title": "T", "body": "B", "severity": "warning"}
    parsed = EVENT_ADAPTER.validate_python(raw)
    assert isinstance(parsed, SuggestionEvent)
    assert parsed.title == "T"
    assert EVENT_ADAPTER.dump_python(parsed) == raw


def test_event_round_trip_alert_forward() -> None:
    """AlertForwardEvent round-trips through the discriminated union."""
    raw = {
        "kind": "alert_forward",
        "fingerprint": "abc",
        "summary": "S",
        "severity": "critical",
    }
    parsed = EVENT_ADAPTER.validate_python(raw)
    assert isinstance(parsed, AlertForwardEvent)
    assert parsed.severity == "critical"
    assert EVENT_ADAPTER.dump_python(parsed) == raw


def test_event_round_trip_log_signature() -> None:
    """LogSignatureEvent round-trips through the discriminated union."""
    raw = {
        "kind": "log_signature",
        "signature": "sig-1",
        "count": LOG_SIG_COUNT,
        "sample_line": "...",
    }
    parsed = EVENT_ADAPTER.validate_python(raw)
    assert isinstance(parsed, LogSignatureEvent)
    assert parsed.count == LOG_SIG_COUNT
    assert EVENT_ADAPTER.dump_python(parsed) == raw


def test_event_round_trip_heartbeat() -> None:
    """HeartbeatEvent round-trips through the discriminated union."""
    raw = {"kind": "heartbeat", "name": "rtlamr", "state": "ok"}
    parsed = EVENT_ADAPTER.validate_python(raw)
    assert isinstance(parsed, HeartbeatEvent)
    assert parsed.state == "ok"
    assert EVENT_ADAPTER.dump_python(parsed) == raw


def test_event_unknown_kind_rejected() -> None:
    """A discriminator value not in the union raises ValidationError."""
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python({"kind": "unknown", "blob": "x"})


def test_event_log_signature_count_must_be_positive() -> None:
    """LogSignatureEvent.count is constrained ge=1."""
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(
            {"kind": "log_signature", "signature": "s", "count": 0, "sample_line": "x"}
        )


def test_event_heartbeat_state_must_be_known() -> None:
    """HeartbeatEvent.state is a Literal — bad values rejected."""
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python({"kind": "heartbeat", "name": "n", "state": "weird"})


# --- CollectorResult ------------------------------------------------------------------------


def test_collector_result_defaults() -> None:
    """Defaults: metrics_emitted=0, errors=[], events=[], duration_seconds=0.0."""
    r = CollectorResult(ok=True)
    assert r.metrics_emitted == 0
    assert r.errors == []
    assert r.events == []
    assert r.duration_seconds == 0.0


def test_collector_result_round_trip_with_events() -> None:
    """A result containing two events of different kinds round-trips through model_dump."""
    r = CollectorResult(
        ok=True,
        metrics_emitted=RESULT_METRICS,
        errors=["x"],
        events=[
            SuggestionEvent(title="T", body="B"),
            HeartbeatEvent(name="n", state="ok"),
        ],
        duration_seconds=RESULT_DURATION,
    )
    payload = r.model_dump()
    rebuilt = CollectorResult.model_validate(payload)
    assert rebuilt.metrics_emitted == RESULT_METRICS
    assert rebuilt.errors == ["x"]
    assert len(rebuilt.events) == RESULT_EVENT_COUNT
    assert isinstance(rebuilt.events[0], SuggestionEvent)
    assert isinstance(rebuilt.events[1], HeartbeatEvent)
    assert rebuilt.duration_seconds == RESULT_DURATION


def test_collector_result_rejects_negative_metrics() -> None:
    """metrics_emitted is ge=0."""
    with pytest.raises(ValidationError):
        CollectorResult(ok=False, metrics_emitted=-1)


def test_collector_result_rejects_negative_duration() -> None:
    """duration_seconds is ge=0."""
    with pytest.raises(ValidationError):
        CollectorResult(ok=False, duration_seconds=-0.001)
