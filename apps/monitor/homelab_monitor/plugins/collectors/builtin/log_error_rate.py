"""LogErrorRateCollector (STAGE-004-037).

Anomaly Type C — "service-wide error rate spike". Stateless per-60s-window
collector: each tick it runs ONE LogsQL `stats by (service) count()` query over
[now-60s, now] that unions severity:error/critical/fatal with the configured
error patterns (logs.error_patterns), and emits

    homelab_container_error_rate{name=<service>}  (GAUGE)

= the count of error-like lines for that service in the last 60s window.

GAUGE, NOT COUNTER (locked Option B). The value is a per-60s-window COUNT — a
"rate" only because the window is pinned to the 60s collector interval. It is a
PURE FUNCTION of the current VL response: no in-memory accumulator, no
stale-service eviction, restart-safe, trivially 100%-coverable. The downstream
vmalert rule (deploy/vmalert/metrics/error_rate_spike.yaml) does a DIRECT gauge
comparison `value > clamp_min(5*avg_over_time(...[7d:5m]), 10)` — NO rate()
(rate() is counter-only). clamp_min (NOT max) is REQUIRED: max(vector, 10) is an
aggregation that collapses labels to {}, so the comparison matches nothing;
clamp_min is element-wise and preserves {name}. Pinning interval == window keeps
the gauge's units coherent.

Self-metric: NONE emitted here. The scheduler emits homelab_collector_run_*
automatically (success/failure/duration). Returning ok=False on the VL-error
path makes the scheduler record homelab_collector_run_failure_total
{reason="result_error"} (see scheduler.py + log_stream_budget.py precedent).

DEFERRED (NOT in this stage):
  - per-pattern homelab_container_error_rate_pattern{name, pattern_kind} metric
    (kind is carried on ErrorPattern so this is a non-breaking add later).
  - logs.error_rate_overrides application (parsed by load_logs_config, unused
    here; STAGE-042 renders per-service rules).
  - per-service cold-start 1h-baseline fallback (the 10/min absolute floor in
    the rule already blocks young-service false-fires).
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx

from homelab_monitor.kernel.config import ErrorPattern, load_logs_config, load_vl_query_limits
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VictoriaLogsClientError,
)
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

_ERROR_RATE_METRIC = "homelab_container_error_rate"
_WINDOW_SECONDS = 60
_DEFAULT_VL_URL = "http://victorialogs:9428"


def _build_error_rate_query(patterns: tuple[ErrorPattern, ...]) -> str:
    """Assemble the single LogsQL stats query unioning severity + error patterns.

    Severity union is ALWAYS present:
        severity:error OR severity:critical OR severity:fatal
    When `patterns` is non-empty, append an OR'd _msg regex alternation:
        OR _msg:~"<r1>|<r2>|..."
    Empty `patterns` (logs.error_patterns: []) → just the severity union.

    The whole filter is parenthesised, then piped to:
        | stats by (service) count() as count

    Quote-safety: each pattern.regex is OR-joined into a single LogsQL regex
    string inside `_msg:~"..."`. A `"` inside a regex fragment would break the
    quoting, so each fragment's `\\` and `"` are escaped (\\ first, then ")
    before joining. The default patterns contain neither, so this is
    defense-in-depth for operator-supplied patterns. We do NOT use
    logsql_quote_phrase here because that escapes for a PHRASE; a `~` REGEX
    value needs the same `\\`/`"` escaping but the regex metachars (|, [, ],
    etc.) must pass through unescaped, so we apply only the structural-quote
    escape.
    """
    severity_union = "severity:error OR severity:critical OR severity:fatal"
    if patterns:
        joined = "|".join(_escape_regex_for_logsql_quote(p.regex) for p in patterns)
        filter_expr = f'({severity_union} OR _msg:~"{joined}")'
    else:
        filter_expr = f"({severity_union})"
    return f"{filter_expr} | stats by (service) count() as count"


def _escape_regex_for_logsql_quote(regex: str) -> str:
    """Escape only the LogsQL-quote structural chars (\\ then ") in a regex fragment.

    Regex metacharacters (| [ ] ( ) . * + etc.) MUST pass through unescaped so
    the alternation works. Only backslash and double-quote are structurally
    significant inside the surrounding `_msg:~"..."` quoted value.
    """
    return regex.replace("\\", "\\\\").replace('"', '\\"')


class LogErrorRateCollector(BaseCollector):
    """Emit homelab_container_error_rate{name} as a per-60s-window error-count gauge."""

    name: ClassVar[str] = "log_error_rate"
    interval: ClassVar[timedelta] = timedelta(seconds=_WINDOW_SECONDS)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "log_error_rate"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(
        self,
        *,
        client: VictoriaLogsClient | None = None,
        vl_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__()
        # Tests inject `client` (a fake). Production passes nothing; the client
        # is built lazily in run() from ctx.http. `vl_url`/`http_client` are an
        # alternate injection seam mirroring LogStreamBudgetCollector.
        self._client = client
        self._vl_url = (vl_url or os.environ.get("HOMELAB_MONITOR_VL_URL", _DEFAULT_VL_URL)).rstrip(
            "/"
        )
        self._http_client = http_client

    def _resolve_client(self, ctx: CollectorContext) -> VictoriaLogsClient | None:
        """Return the injected client, or build one from ctx.http. None if no http."""
        if self._client is not None:
            return self._client
        http = self._http_client if self._http_client is not None else ctx.http
        if http is None:  # pyright: ignore[reportUnnecessaryComparison]
            return None
        return VictoriaLogsClient(
            vl_url=self._vl_url,
            http_client=http,
            limits=load_vl_query_limits(),
        )

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Run a single tick. Emits one homelab_container_error_rate gauge per service."""
        start = time.monotonic()
        errors: list[str] = []
        emitted = 0

        client = self._resolve_client(ctx)
        if client is None:
            errors.append("http_client_unavailable")
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=errors,
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        expr = _build_error_rate_query(load_logs_config().error_patterns)
        now = datetime.now(tz=UTC)
        end_iso = now.isoformat()
        start_iso = (now - timedelta(seconds=_WINDOW_SECONDS)).isoformat()

        try:
            result = await client.query(expr=expr, start=start_iso, end=end_iso)
        except VictoriaLogsClientError as exc:
            errors.append(f"vl_query: {exc}")
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=errors,
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        for line in result.lines:
            service = line.fields.get("service")
            raw_count = line.fields.get("count")
            if not service or raw_count is None:
                continue
            try:
                count = int(raw_count)
            except ValueError:
                continue
            ctx.vm.write_gauge(
                _ERROR_RATE_METRIC,
                float(count),
                {"name": service},
            )
            emitted += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )


__all__ = ["LogErrorRateCollector"]
