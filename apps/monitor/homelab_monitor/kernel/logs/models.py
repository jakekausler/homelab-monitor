"""Converged log-line response model + VictoriaLogs mapper (STAGE-004-002).

`LogLine` is the single shape returned by every log-viewing endpoint
(docker container logs, cron run logs, generic LogsQL query). It replaces the
three divergent per-endpoint models (ContainerLogLine, RunLogLine,
LogsQueryEntry) that previously diverged on field names (`line`/`message`,
`ts`/`timestamp`) and on whether severity/host/service were surfaced.

`from_victorialogs_line` is the ONLY mapper shipped in this stage (YAGNI: no
from_docker_socket_log / from_hmrun_event). It promotes severity/host/service
out of the raw VL `fields` bag into typed top-level columns while preserving
the full `fields` dict for forensics.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.logs.victorialogs_client import VlLogLine

# Canonical lowercase severity set (syslog-derived, see D5).
_SYSLOG_NUMERIC: dict[str, str] = {
    "0": "emergency",
    "1": "alert",
    "2": "critical",
    "3": "error",
    "4": "warn",
    "5": "notice",
    "6": "info",
    "7": "debug",
}

_SEVERITY_ALIASES: dict[str, str] = {
    "warning": "warn",
    "err": "error",
    "crit": "critical",
    "panic": "emergency",
    "emerg": "emergency",
}

_CANONICAL_SEVERITIES: frozenset[str] = frozenset(
    {
        "debug",
        "info",
        "notice",
        "warn",
        "error",
        "critical",
        "alert",
        "emergency",
    }
)


def normalize_severity(raw: str | None) -> str | None:
    """Normalize a raw severity token to the canonical lowercase set.

    Rules (D5):
      - None or empty/whitespace-only -> None (caller/viewer may default).
      - syslog numeric "0".."7" -> mapped canonical name.
      - known alias (warning/err/crit/panic/emerg, case-insensitive) -> canonical.
      - already-canonical (case-insensitive) -> lowercased canonical.
      - any other non-empty token -> "info" (defensive default).

    Vector already defaults severity to "info" at ingest, so a missing raw
    severity (None) is rare; we keep None rather than inventing one here.
    """
    if raw is None:
        return None
    token = raw.strip().lower()
    if not token:
        return None
    if token in _SYSLOG_NUMERIC:
        return _SYSLOG_NUMERIC[token]
    if token in _SEVERITY_ALIASES:
        return _SEVERITY_ALIASES[token]
    if token in _CANONICAL_SEVERITIES:
        return token
    return "info"


class LogLine(BaseModel):
    """One converged log line returned by every log-viewing endpoint.

    `severity`/`host`/`service` are promoted out of the raw VictoriaLogs
    `fields` bag into typed top-level columns by `from_victorialogs_line`.
    `fields` retains the full original bag (plus `severity_raw` when a raw
    severity was present), so nothing is lost.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: str
    message: str
    stream: str
    severity: str | None
    host: str | None
    service: str | None
    fields: dict[str, Any]


def from_victorialogs_line(line: VlLogLine) -> LogLine:
    """Map a raw VlLogLine to the converged LogLine shape.

    Promotes severity (normalized), host, and service out of `line.fields`.
    Does NOT mutate `line.fields` — builds a fresh dict for the result.
    Preserves the raw severity token under `fields["severity_raw"]` when a raw
    severity was present in the input.
    """
    # Copy so we never mutate the (frozen-dataclass) input's fields dict.
    fields: dict[str, Any] = dict(line.fields)

    raw_severity = line.fields.get("severity")
    severity = normalize_severity(raw_severity)
    if raw_severity is not None:
        fields["severity_raw"] = raw_severity

    host = line.fields.get("host") or line.fields.get("_HOSTNAME") or None
    service = line.fields.get("service") or line.fields.get("SYSLOG_IDENTIFIER") or None

    return LogLine(
        timestamp=line.timestamp,
        message=line.message,
        stream=line.stream,
        severity=severity,
        host=host,
        service=service,
        fields=fields,
    )


__all__ = ["LogLine", "from_victorialogs_line", "normalize_severity"]
