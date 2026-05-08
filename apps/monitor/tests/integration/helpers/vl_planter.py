"""Test helper: plant deterministic log lines into VictoriaLogs.

Used by integration tests to inject log records with known content + timestamps,
bypassing the vector pipeline (which is tested separately in STAGE-021's
canonical e2e test).

VictoriaLogs ingest endpoint contract (verified against v0.30.0 docs):
    POST {vl_url}/insert/jsonline
    Content-Type: application/x-ndjson  (one JSON object per line)
    Special fields: _time (RFC3339Nano OR epoch nanoseconds OR epoch millis),
                    _msg  (log message text)
    All other fields become indexed log labels (host, service, severity, ...).

VL accepts batched ingest (multiple NDJSON lines per request) — single POST.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx


def _default_vl_url() -> str:
    """Resolve VL ingest URL from $VL_URL or default to compose-network DNS."""
    import os  # noqa: PLC0415  -- inline to keep import surface narrow at module load

    return os.environ.get("VL_URL", "http://victorialogs:9428").rstrip("/")


def plant_log_lines(  # noqa: PLR0913
    *,
    host: str,
    service: str,
    severity: str,
    message: str,
    count: int,
    base_time: datetime | None = None,
    interval_ms: int = 100,
    extra_fields: dict[str, str] | None = None,
    vl_url: str | None = None,
    timeout_s: float = 10.0,
) -> int:
    """POST `count` NDJSON log records to VictoriaLogs `/insert/jsonline`.

    Each record is timestamped sequentially starting from ``base_time`` (default:
    UTC now) with ``interval_ms`` between records. Records share host/service/severity
    label set; ``message`` is identical across all records (the rule's phrase filter
    matches each).

    Args:
        host: value for the `host` log field (matches vector's emitter labelling).
        service: value for the `service` log field (matches vector's emitter labelling).
        severity: value for the `severity` log field (info|warning|error|critical).
        message: text for `_msg`. The rule's phrase filter must match this literal.
        count: number of records to plant. Must be >= 1.
        base_time: timestamp anchor (UTC). If None, uses ``datetime.now(UTC)``.
        interval_ms: gap between successive records' timestamps. Default 100ms.
        extra_fields: additional indexed log fields merged into each record.
        vl_url: explicit VL base URL; if None, reads $VL_URL or defaults to
            ``http://victorialogs:9428``.
        timeout_s: HTTP request timeout.

    Returns:
        HTTP status code from the VL ingest POST (typically 200 on success).

    Raises:
        httpx.HTTPStatusError: if VL responds non-2xx.
        AssertionError: if ``count < 1``.
    """
    assert count >= 1, "plant_log_lines requires count >= 1"

    base = base_time or datetime.now(UTC)
    base_ms = int(base.timestamp() * 1000)
    extras = extra_fields or {}

    lines: list[str] = []
    for i in range(count):
        ts_ms = base_ms + (i * interval_ms)
        record: dict[str, Any] = {
            "_time": ts_ms,
            "_msg": message,
            "host": host,
            "service": service,
            "severity": severity,
            **extras,
        }
        lines.append(json.dumps(record, separators=(",", ":")))

    body = "\n".join(lines)
    url = f"{vl_url or _default_vl_url()}/insert/jsonline"
    response = httpx.post(
        url,
        content=body,
        headers={"Content-Type": "application/x-ndjson"},
        timeout=timeout_s,
    )
    response.raise_for_status()
    return response.status_code
