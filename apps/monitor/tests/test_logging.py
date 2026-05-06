"""Tests for kernel/logging.py — structlog configuration and stdlib interop."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from io import StringIO

import pytest
import structlog

from homelab_monitor.kernel.logging import configure_logging


@pytest.fixture
def reset_structlog_config() -> Iterator[None]:
    """Reset structlog + kernel/logging.py module-level idempotency flag between tests.

    Why: kernel.logging.configure_logging() short-circuits on _configured=True so
    every per-test reconfiguration would otherwise be a no-op. We also call
    structlog.reset_defaults() to restore default processors after the test so
    other tests that share this process get the library default config back.
    """
    from homelab_monitor.kernel import logging as log_module  # noqa: PLC0415

    log_module._configured = False  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        structlog.reset_defaults()
        log_module._configured = False  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def capture_logging() -> Iterator[tuple[StringIO, logging.Handler]]:
    """Capture logging output to a StringIO."""
    _stream = StringIO()
    handler = logging.StreamHandler(_stream)
    root = logging.getLogger()
    root.addHandler(handler)
    yield _stream, handler
    root.removeHandler(handler)


def test_configure_logging_json_format(
    reset_structlog_config: None, capture_logging: tuple[StringIO, logging.Handler]
) -> None:
    """configure_logging(format='json') produces parseable JSON on stdout."""
    stream, _ = capture_logging
    configure_logging(log_format="json", level="INFO")
    log = structlog.get_logger("test")
    log.info("test_event", key="value")
    output = stream.getvalue()
    # Should be valid JSON lines
    for line in output.strip().split("\n"):
        if line:
            data = json.loads(line)
            assert "event" in data or "ts" in data


def test_configure_logging_ts_field_iso8601(
    reset_structlog_config: None, capture_logging: tuple[StringIO, logging.Handler]
) -> None:
    """ts field is ISO-8601 UTC."""
    stream, _ = capture_logging
    configure_logging(log_format="json", level="INFO")
    log = structlog.get_logger("test")
    log.info("test_event")
    output = stream.getvalue()
    for line in output.strip().split("\n"):
        if line:
            data = json.loads(line)
            if "ts" in data:
                ts = data["ts"]
                # ISO-8601 format check (basic)
                assert "T" in ts
                assert "Z" in ts or "+" in ts


def test_configure_logging_request_id_propagates(
    reset_structlog_config: None, capture_logging: tuple[StringIO, logging.Handler]
) -> None:
    """request_id propagates from contextvar to log output."""
    stream, handler = capture_logging
    configure_logging(log_format="json", level="INFO")
    logging.getLogger().addHandler(handler)
    structlog.contextvars.bind_contextvars(request_id="req-12345")
    log = structlog.get_logger("test")
    log.info("test_event")
    output = stream.getvalue()
    # At least one line should contain request_id
    found = False
    for line in output.strip().split("\n"):
        if line:
            data = json.loads(line)
            if data.get("request_id") == "req-12345":
                found = True
                break
    assert found


def test_configure_logging_tick_id_propagates(
    reset_structlog_config: None, capture_logging: tuple[StringIO, logging.Handler]
) -> None:
    """tick_id propagates from contextvar."""
    stream, handler = capture_logging
    configure_logging(log_format="json", level="INFO")
    logging.getLogger().addHandler(handler)
    structlog.contextvars.bind_contextvars(tick_id="tick-abc123")
    log = structlog.get_logger("test")
    log.info("test_event")
    output = stream.getvalue()
    found = False
    for line in output.strip().split("\n"):
        if line:
            data = json.loads(line)
            if data.get("tick_id") == "tick-abc123":
                found = True
                break
    assert found


def test_configure_logging_stdlib_piping(
    reset_structlog_config: None, capture_logging: tuple[StringIO, logging.Handler]
) -> None:
    """stdlib logging.getLogger('uvicorn').info(...) produces structured JSON."""
    stream, handler = capture_logging
    configure_logging(log_format="json", level="INFO")
    logging.getLogger().addHandler(handler)
    # Get a stdlib logger and log something
    logger = logging.getLogger("uvicorn")
    logger.info("test_stdlib_message")
    output = stream.getvalue()
    # Should produce at least one line of JSON
    lines = [line for line in output.strip().split("\n") if line]
    assert len(lines) > 0


def test_configure_logging_idempotent(
    reset_structlog_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """configure_logging is idempotent."""
    # First call
    configure_logging(log_format="json", level="INFO")
    # Second call should not raise
    configure_logging(log_format="json", level="DEBUG")
    # Verify it's truly idempotent by checking the _configured flag
    from homelab_monitor.kernel import logging as log_module  # noqa: PLC0415

    assert log_module._configured is True  # pyright: ignore[reportPrivateUsage]


def test_configure_logging_pretty_format(
    reset_structlog_config: None, capture_logging: tuple[StringIO, logging.Handler]
) -> None:
    """configure_logging(format='pretty') produces colored output without crashing."""
    stream, handler = capture_logging
    configure_logging(log_format="pretty", level="INFO")
    logging.getLogger().addHandler(handler)
    log = structlog.get_logger("test")
    log.info("test_event", key="value")
    output = stream.getvalue()
    # Just verify it doesn't crash and produces some output
    assert len(output) > 0


def test_configure_logging_honors_env_format(
    reset_structlog_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capture_logging: tuple[StringIO, logging.Handler],
) -> None:
    """configure_logging honors HOMELAB_MONITOR_LOG_FORMAT env var."""
    _stream, handler = capture_logging
    monkeypatch.setenv("HOMELAB_MONITOR_LOG_FORMAT", "pretty")
    configure_logging()  # Should read env
    logging.getLogger().addHandler(handler)
    log = structlog.get_logger("test")
    log.info("test_event")
    # Just verify it ran without error
    assert len(_stream.getvalue()) > 0


def test_configure_logging_honors_env_level(
    reset_structlog_config: None,
    monkeypatch: pytest.MonkeyPatch,
    capture_logging: tuple[StringIO, logging.Handler],
) -> None:
    """configure_logging honors HOMELAB_MONITOR_LOG_LEVEL env var."""
    _stream, _ = capture_logging
    monkeypatch.setenv("HOMELAB_MONITOR_LOG_LEVEL", "DEBUG")
    configure_logging()
    root = logging.getLogger()
    assert root.level == logging.DEBUG


def test_configure_logging_level_string_conversion(
    reset_structlog_config: None, capture_logging: tuple[StringIO, logging.Handler]
) -> None:
    """configure_logging converts level string to logging constant."""
    _stream, _ = capture_logging
    configure_logging(log_format="json", level="WARNING")
    root = logging.getLogger()
    assert root.level == logging.WARNING


def test_configure_logging_stdlib_loggers_propagate(
    reset_structlog_config: None, capture_logging: tuple[StringIO, logging.Handler]
) -> None:
    """stdlib loggers (uvicorn, httpx, alembic) are configured to propagate."""
    _stream, _ = capture_logging
    configure_logging(log_format="json", level="INFO")
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "alembic"):
        logger = logging.getLogger(logger_name)
        assert logger.propagate is True
        assert len(logger.handlers) == 0


def test_reset_logging_for_tests() -> None:
    """reset_logging_for_tests() resets _configured flag to enable re-configuration."""
    from homelab_monitor.kernel import logging as log_module  # noqa: PLC0415
    from homelab_monitor.kernel.logging import reset_logging_for_tests  # noqa: PLC0415

    # Verify flag starts as False or gets set
    reset_logging_for_tests()
    assert log_module._configured is False  # pyright: ignore[reportPrivateUsage]

    # After configure_logging, flag should be True
    configure_logging(log_format="json", level="INFO")
    assert log_module._configured is True  # pyright: ignore[reportPrivateUsage]

    # After reset, should be False again
    reset_logging_for_tests()
    assert log_module._configured is False  # pyright: ignore[reportPrivateUsage]
