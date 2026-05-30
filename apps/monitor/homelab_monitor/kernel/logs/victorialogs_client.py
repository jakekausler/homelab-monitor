"""VictoriaLogsClient — bounded LogsQL query primitive against VictoriaLogs.

STAGE-002-013. The single reusable VL-query primitive. Every query is bounded
by an explicit time range, a max-lines cap, a max-bytes cap, and an HTTP
timeout. A response exceeding a cap is returned truncated with truncated=True
— never an unbounded fetch.

EPIC-004's generic /api/logs proxy will later be built on top of this client.
The kernel API router logs.py already delegates /logs/query to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import structlog
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.config import VlQueryLimits
from homelab_monitor.kernel.cron.log_match import canonical_log_key

_HTTP_OK = 200


class VictoriaLogsClientError(RuntimeError):
    """Raised when VictoriaLogs is unreachable, times out, or returns non-200.

    Callers (CronRunReconciler enrich phase, /logs/query endpoint) catch this
    and degrade gracefully — they never let it propagate as an unhandled crash.
    """


@dataclass(slots=True, frozen=True)
class VlLogLine:
    """One parsed VictoriaLogs log line.

    `fields` holds every journald / regular field EXCEPT the three VL builtins
    (_stream_id, _msg, _time), so `run_id`, `service`, SYSLOG_IDENTIFIER, etc.
    are all accessible there.
    """

    timestamp: str
    message: str
    stream: str
    fields: dict[str, str] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


@dataclass(slots=True, frozen=True)
class VlQueryResult:
    """Result of a bounded VL query."""

    lines: list[VlLogLine]
    truncated: bool


def build_amode_query(run_id: str) -> str:
    """LogsQL for an A-mode (wrapper) run: UUID-exact.

    `run_id` is a REGULAR VictoriaLogs field (set by the Vector hmrun transform,
    NOT a stream field). The per-run [vl_window_start, vl_window_end] bound that
    the caller passes keeps this regular-field filter performant. SYSLOG_IDENTIFIER
    is also a regular field; the conjunction narrows to exactly this run's lines.

    `run_id` is logsql_quote_phrase-escaped for defense-in-depth even though
    current callers only generate UUID-shaped values.
    """
    return f"SYSLOG_IDENTIFIER:hmrun AND run_id:{logsql_quote_phrase(run_id)}"


def logsql_quote_phrase(text: str) -> str:
    """Return `text` as a LogsQL double-quoted phrase string, fully escaped.

    Canonical LogsQL value-quoting primitive. Anything embedding a value in
    LogsQL MUST go through this function.

    LogsQL quoted phrases are Go-style quoted strings: the backslash is the
    escape introducer, so a literal backslash MUST be escaped to ``\\\\`` and a
    literal double-quote to ``\\"``. Backslash MUST be escaped FIRST — escaping
    ``"`` first would leave the inserted backslash unescaped. Real cron commands
    contain ``\\`` (e.g. ``\\!``), single quotes, parentheses, ``&&`` and ``|``;
    only ``"`` and ``\\`` are structurally significant inside a quoted phrase,
    so those two are the complete escape set.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_bmode_query(command: str) -> str:
    """LogsQL for a B-mode (log-scrape) run: cron-fingerprint heuristic.

    Reuses canonical_log_key (STAGE-002-008) so the query key matches the same
    canonical command form the cron registry stores in crons.log_match_key.
    The caller bounds the query by the run's [started_at, ended_at] window.
    A B-mode run has no per-line UUID, so this is a heuristic phrase match on
    the canonical command text. The canonical key may contain backslashes,
    single/double quotes and parentheses (real cron commands are messy), so it
    is escaped via logsql_quote_phrase before being embedded.
    """
    log_key = canonical_log_key(command)
    return logsql_quote_phrase(log_key)


class VictoriaLogsClient:
    """Bounded LogsQL query client for VictoriaLogs."""

    def __init__(
        self,
        *,
        vl_url: str,
        http_client: httpx.AsyncClient,
        limits: VlQueryLimits,
    ) -> None:
        self._vl_url = vl_url.rstrip("/")
        self._http_client = http_client
        self._limits = limits
        self._log: BoundLogger = cast(
            BoundLogger,
            structlog.get_logger().bind(component="victorialogs_client"),
        )

    async def query(
        self,
        *,
        expr: str,
        start: str,
        end: str,
    ) -> VlQueryResult:
        """Run a bounded LogsQL query over [start, end] (ISO-8601 UTC strings).

        Caps the result at limits.max_lines AND limits.max_bytes — whichever is
        hit first sets truncated=True. The HTTP `limit` param is set to
        max_lines+1 so a result exactly at the cap is still detectable.

        Raises VictoriaLogsClientError on transport error, timeout, or non-200.
        """
        params = {
            "query": expr,
            "start": start,
            "end": end,
            "limit": str(self._limits.max_lines + 1),
        }
        try:
            resp = await self._http_client.get(
                f"{self._vl_url}/select/logsql/query",
                params=params,
                timeout=self._limits.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            self._log.warning("victorialogs_client.upstream_error", error=str(exc))
            msg = f"victorialogs query failed: {exc}"
            raise VictoriaLogsClientError(msg) from exc

        if resp.status_code != _HTTP_OK:
            self._log.warning("victorialogs_client.upstream_status", status=resp.status_code)
            # SECURITY: do not relay resp.text (may carry cross-query log excerpts).
            msg = f"victorialogs returned status {resp.status_code}"
            raise VictoriaLogsClientError(msg)

        return self._parse_ndjson(resp.text)

    def with_limits(self, limits: VlQueryLimits) -> VictoriaLogsClient:
        """Return a new client sharing this client's URL + http client but with
        different bounded limits. Used by the A1 paginator to set the per-page
        limit (page_size + boundary_n) without mutating the original client.
        """
        return VictoriaLogsClient(
            vl_url=self._vl_url,
            http_client=self._http_client,
            limits=limits,
        )

    def _parse_ndjson(self, body: str) -> VlQueryResult:
        """Parse VL NDJSON, applying the max-lines + max-bytes caps."""
        lines: list[VlLogLine] = []
        running_bytes = 0
        truncated = False
        for raw_line in body.splitlines():
            if not raw_line.strip():
                continue
            running_bytes += len(raw_line.encode("utf-8"))
            if len(lines) >= self._limits.max_lines or running_bytes > self._limits.max_bytes:
                truncated = True
                break
            parsed = self._parse_one(raw_line)
            if parsed is not None:
                lines.append(parsed)
        return VlQueryResult(lines=lines, truncated=truncated)

    @staticmethod
    def _parse_one(raw_line: str) -> VlLogLine | None:
        """Parse one NDJSON line into a VlLogLine, or None if malformed."""
        try:
            obj_raw: object = json.loads(raw_line)
        except ValueError:
            return None
        if not isinstance(obj_raw, dict):
            return None
        obj = cast(dict[str, Any], obj_raw)
        stream = str(obj.get("_stream_id", ""))
        message = str(obj.get("_msg", ""))
        timestamp = str(obj.get("_time", ""))
        # VictoriaLogs serializes every non-meta field as a JSON string in
        # its response, so str(v) here is a no-op for the standard journald
        # field set. Non-string field types only occur if a Vector transform
        # emits nested structures (arrays/objects), in which case str(v)
        # produces a Python repr — useful for log forensics, not for typed
        # downstream consumption.
        fields = {k: str(v) for k, v in obj.items() if k not in {"_stream_id", "_msg", "_time"}}
        return VlLogLine(timestamp=timestamp, message=message, stream=stream, fields=fields)


__all__ = [
    "VictoriaLogsClient",
    "VictoriaLogsClientError",
    "VlLogLine",
    "VlQueryResult",
    "build_amode_query",
    "build_bmode_query",
    "logsql_quote_phrase",
]
