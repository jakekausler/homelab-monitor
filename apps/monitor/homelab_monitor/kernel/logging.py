"""Centralized structlog configuration with stdlib interop.

Wires structlog processors (contextvars, timestamps, JSON rendering) and
pipes stdlib loggers (uvicorn, httpx, alembic) through structlog's
ProcessorFormatter so all output is consistent JSON.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

import structlog
from structlog.processors import JSONRenderer, TimeStamper
from structlog.stdlib import ProcessorFormatter

_configured: bool = False


def configure_logging(
    *,
    log_format: Literal["json", "pretty"] | None = None,
    level: str | None = None,
) -> None:
    """Configure structlog + stdlib logging. Idempotent.

    Reads HOMELAB_MONITOR_LOG_FORMAT (default "json") and
    HOMELAB_MONITOR_LOG_LEVEL (default "INFO") when args are None.

    Args:
        log_format: "json" or "pretty"; None reads env or defaults to "json".
        level: logging level string; None reads env or defaults to "INFO".
    """
    global _configured  # noqa: PLW0603  -- module-level idempotency guard
    if _configured:
        return
    _configured = True

    # Read environment overrides
    resolved_format: Literal["json", "pretty"]
    if log_format is None:
        env_format = os.environ.get("HOMELAB_MONITOR_LOG_FORMAT", "json")
        resolved_format = "pretty" if env_format == "pretty" else "json"
    else:
        resolved_format = log_format
    if level is None:
        level = os.environ.get("HOMELAB_MONITOR_LOG_LEVEL", "INFO")

    # Choose renderer
    if resolved_format == "pretty":
        renderer = structlog.dev.ConsoleRenderer(
            colors=True,
            exception_formatter=structlog.dev.RichTracebackFormatter(),
        )
    else:
        renderer = JSONRenderer(sort_keys=True)

    # Processor chain for structlog
    structlog.configure(  # pyright: ignore[reportUnknownArgumentType]
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.stdlib.add_logger_name,
            TimeStamper(fmt="iso", utc=True, key="ts"),
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Wire stdlib loggers through structlog's ProcessorFormatter
    formatter = ProcessorFormatter(
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            TimeStamper(fmt="iso", utc=True, key="ts"),
        ],
        processors=[
            ProcessorFormatter.remove_processors_meta,
            JSONRenderer(sort_keys=True) if resolved_format == "json" else renderer,
        ],
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Configure stdlib loggers to propagate through structlog
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "alembic"):
        lg = logging.getLogger(logger_name)
        lg.handlers = []
        lg.propagate = True


def reset_logging_for_tests() -> None:
    """Reset configure_logging idempotency flag and structlog defaults for tests."""
    global _configured  # noqa: PLW0603
    _configured = False
    structlog.reset_defaults()
