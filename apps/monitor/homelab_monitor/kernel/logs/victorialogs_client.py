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
from collections.abc import AsyncIterator
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

    Callers (CronRunReconciler enrich phase, /logs/query endpoint, /logs/tail
    probe) catch this and degrade gracefully — they never let it propagate as an
    unhandled crash.

    ``status_code`` is the upstream HTTP status when the error came from a
    non-200 RESPONSE; it is None for transport errors / timeouts (no response).
    The /logs/tail probe uses it to map VL 4xx -> 422 and VL 5xx/transport -> 502.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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


@dataclass(slots=True, frozen=True)
class HitsSeries:
    """One per-field-value time series from VictoriaLogs ``/select/logsql/hits``.

    With ``field=severity``, VL returns ONE HitsSeries per distinct raw severity
    token. ``timestamps`` are bucket-START times (ISO-8601 UTC), aligned to
    VL's step/epoch grid — NOT to the caller's ``start`` — so callers re-bin
    onto their own start-aligned buckets. ``counts`` is parallel to
    ``timestamps`` (count of matching lines in each bucket). ``field_value`` is
    the raw severity token for this series (None when VL emits a ``fields:{}``
    total series, e.g. an un-grouped call).
    """

    field_value: str | None
    timestamps: list[str]
    counts: list[int]


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
            raise VictoriaLogsClientError(msg, resp.status_code)

        return self._parse_ndjson(resp.text)

    async def stream_query(
        self,
        *,
        expr: str,
        start: str,
        end: str,
        limit: int,
    ) -> AsyncIterator[VlLogLine]:
        """Stream up to ``limit`` matching lines over [start, end] one at a time.

        Unlike ``query()`` this does NOT buffer the whole NDJSON body. It opens a
        streaming HTTP request to VictoriaLogs ``/select/logsql/query`` and yields
        each parsed ``VlLogLine`` as the line arrives, stopping after ``limit``
        non-empty parseable lines. Malformed NDJSON lines are skipped (mirrors
        ``_parse_ndjson``). O(1) memory: only one line is held at a time.

        ``limit`` is passed to VL verbatim as the HTTP ``limit`` cap; we ALSO
        enforce it locally so a VL that ignores/overshoots the cap can never make
        us emit more than ``limit`` lines (cap-enforcement test depends on this).

        Raises VictoriaLogsClientError on transport error, timeout, or non-200 —
        the non-200 check happens AFTER the stream context is entered but BEFORE
        any line is yielded, so callers can map it to a 502 pre-flight.
        """
        params = {
            "query": expr,
            "start": start,
            "end": end,
            "limit": str(limit),
        }
        url = f"{self._vl_url}/select/logsql/query"
        try:
            async with self._http_client.stream(
                "GET",
                url,
                params=params,
                timeout=self._limits.timeout_seconds,
            ) as resp:
                if resp.status_code != _HTTP_OK:
                    self._log.warning("victorialogs_client.stream_status", status=resp.status_code)
                    # SECURITY: do not relay body (may carry cross-query excerpts).
                    msg = f"victorialogs returned status {resp.status_code}"
                    raise VictoriaLogsClientError(msg, resp.status_code)
                emitted = 0
                async for raw_line in resp.aiter_lines():
                    if emitted >= limit:
                        break
                    if not raw_line.strip():
                        continue
                    parsed = self._parse_one(raw_line)
                    if parsed is not None:
                        emitted += 1
                        yield parsed
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            self._log.warning("victorialogs_client.stream_error", error=str(exc))
            msg = f"victorialogs stream failed: {exc}"
            raise VictoriaLogsClientError(msg) from exc

    async def field_names(
        self,
        *,
        expr: str,
        start: str,
        end: str,
    ) -> list[tuple[str, int]]:
        """Return (field_name, hits) pairs matching `expr` over [start, end].

        Calls VictoriaLogs ``/select/logsql/field_names``, which returns a JSON
        object ``{"values": [{"value": "<field>", "hits": <int>}, ...]}`` — the
        authoritative complete field-name list with per-field hit counts. The
        ``_msg`` entry's hits equals the total number of matching lines (every
        line has ``_msg``), so callers can derive exact per-field coverage.

        Unlike ``query()`` this is NOT a bounded NDJSON stream — the response is
        a single small JSON object (one row per distinct field), so no
        max_lines/max_bytes cap applies. Same error contract as ``query()``:
        raises VictoriaLogsClientError on transport error, timeout, or non-200.
        Malformed / non-conforming rows are skipped silently.
        """
        params = {"query": expr, "start": start, "end": end}
        try:
            resp = await self._http_client.get(
                f"{self._vl_url}/select/logsql/field_names",
                params=params,
                timeout=self._limits.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            self._log.warning("victorialogs_client.field_names_error", error=str(exc))
            msg = f"victorialogs field_names failed: {exc}"
            raise VictoriaLogsClientError(msg) from exc

        if resp.status_code != _HTTP_OK:
            self._log.warning("victorialogs_client.field_names_status", status=resp.status_code)
            # SECURITY: do not relay resp.text (may carry cross-query excerpts).
            msg = f"victorialogs field_names returned status {resp.status_code}"
            raise VictoriaLogsClientError(msg, resp.status_code)

        return self._parse_field_names(resp.text)

    async def hits(
        self,
        *,
        expr: str,
        start: str,
        end: str,
        step: str,
        field: str = "severity",
    ) -> list[HitsSeries]:
        """Per-bucket hit counts grouped by ``field`` over [start, end].

        Calls VictoriaLogs ``/select/logsql/hits`` (landed v0.8.0;
        ``field``-grouping on the HTTP endpoint landed v0.25.0). Returns one
        HitsSeries per distinct value of ``field`` (default ``severity``). The
        response is::

            {"hits": [
                {"fields": {"severity": "<raw>"},
                 "timestamps": ["<ISO>", ...],
                 "values": [<count>, ...],
                 "total": N},
                ...
            ]}

        ``step`` is a VL duration string (e.g. ``"60000ms"``); VL's bucket-start
        timestamps are aligned to the step/epoch grid, NOT to ``start``, so the
        caller re-bins. ``end`` is INCLUSIVE in v0.30.0.

        Like ``field_names()`` this is a single small JSON object (NOT a bounded
        NDJSON stream) — no max_lines/max_bytes cap applies. Same error contract:
        raises VictoriaLogsClientError on transport error, timeout, or non-200.
        Malformed / non-conforming rows are skipped silently.
        """
        params = {"query": expr, "start": start, "end": end, "step": step, "field": field}
        try:
            resp = await self._http_client.get(
                f"{self._vl_url}/select/logsql/hits",
                params=params,
                timeout=self._limits.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            self._log.warning("victorialogs_client.hits_error", error=str(exc))
            msg = f"victorialogs hits failed: {exc}"
            raise VictoriaLogsClientError(msg) from exc

        if resp.status_code != _HTTP_OK:
            self._log.warning("victorialogs_client.hits_status", status=resp.status_code)
            # SECURITY: do not relay resp.text (may carry cross-query excerpts).
            msg = f"victorialogs hits returned status {resp.status_code}"
            raise VictoriaLogsClientError(msg, resp.status_code)

        return self._parse_hits(resp.text, field=field)

    @staticmethod
    def _parse_field_names(body: str) -> list[tuple[str, int]]:
        """Parse the field_names JSON object into (name, hits) pairs.

        Tolerant: a malformed body, a non-object top level, a missing/non-list
        ``values`` key, or any individual row that is not a dict with a string
        ``value`` + integer-coercible ``hits`` is skipped. Returns [] on a
        wholly-unparseable body rather than raising — an empty field set is a
        valid (if degenerate) scope result.
        """
        try:
            obj_raw: object = json.loads(body)
        except ValueError:
            return []
        if not isinstance(obj_raw, dict):
            return []
        obj = cast(dict[str, Any], obj_raw)
        values_raw = obj.get("values")
        if not isinstance(values_raw, list):
            return []
        values = cast(list[Any], values_raw)
        out: list[tuple[str, int]] = []
        for row_raw in values:
            if not isinstance(row_raw, dict):
                continue
            row = cast(dict[str, Any], row_raw)
            name = row.get("value")
            hits_raw = row.get("hits")
            if not isinstance(name, str):
                continue
            try:
                hits = int(hits_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            out.append((name, hits))
        return out

    @staticmethod
    def _parse_hits(body: str, *, field: str) -> list[HitsSeries]:
        """Parse the ``/hits`` JSON object into HitsSeries list.

        Tolerant: a malformed body, a non-object top level, a missing/non-list
        ``hits`` key, or any individual row that is not a dict yields a skip.
        Within a row, ``timestamps``/``values`` must both be lists; they are
        zipped to the SHORTER length (defensive against VL length mismatch).
        A value that is not int-coercible is treated as 0. ``field_value`` is
        read from ``row["fields"][field]`` when present and a string, else None.
        Returns [] on a wholly-unparseable body rather than raising.
        """
        try:
            obj_raw: object = json.loads(body)
        except ValueError:
            return []
        if not isinstance(obj_raw, dict):
            return []
        obj = cast(dict[str, Any], obj_raw)
        hits_raw = obj.get("hits")
        if not isinstance(hits_raw, list):
            return []
        hits_list = cast(list[Any], hits_raw)
        out: list[HitsSeries] = []
        for row_raw in hits_list:
            if not isinstance(row_raw, dict):
                continue
            row = cast(dict[str, Any], row_raw)
            ts_raw = row.get("timestamps")
            vals_raw = row.get("values")
            if not isinstance(ts_raw, list) or not isinstance(vals_raw, list):
                continue
            ts_list = cast(list[Any], ts_raw)
            vals_list = cast(list[Any], vals_raw)
            field_value: str | None = None
            fields_raw = row.get("fields")
            if isinstance(fields_raw, dict):
                fv = cast(dict[str, Any], fields_raw).get(field)
                if isinstance(fv, str):
                    field_value = fv
            timestamps: list[str] = []
            counts: list[int] = []
            for ts_item, val_item in zip(ts_list, vals_list, strict=False):
                if not isinstance(ts_item, str):
                    continue
                try:
                    count = int(val_item)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    count = 0
                timestamps.append(ts_item)
                counts.append(count)
            out.append(HitsSeries(field_value=field_value, timestamps=timestamps, counts=counts))
        return out

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
    "HitsSeries",
    "VictoriaLogsClient",
    "VictoriaLogsClientError",
    "VlLogLine",
    "VlQueryResult",
    "build_amode_query",
    "build_bmode_query",
    "logsql_quote_phrase",
]
