"""Unbound recursive-resolver stats collector (STAGE-006-013).

Polls the ``pihole-unbound`` container via docker-exec (consuming the
STAGE-006-003 :func:`fetch_unbound_stats` access layer) and emits unbound
resolver metrics. Degrades gracefully when extended-statistics is off: the
default metric set is always emitted; the extended families (histogram-derived
recursion-time quantiles, per-type query counts, per-rcode answer counts,
DNSSEC secure/bogus) are emitted only when ``extended_enabled`` is True.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar

from homelab_monitor.kernel.config import PiholeUnboundConfig
from homelab_monitor.kernel.docker.socket_client import DockerSocketClient
from homelab_monitor.kernel.pihole import (
    UnboundError,
    fetch_unbound_stats,
)
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult

# Public metric-name constants (contract-tested by literal assertions).
M_QUERIES = "homelab_unbound_queries_total"
M_CACHE_HITS = "homelab_unbound_cache_hits_total"
M_CACHE_MISSES = "homelab_unbound_cache_misses_total"
M_CACHE_HIT_RATIO = "homelab_unbound_cache_hit_ratio"
M_PREFETCH = "homelab_unbound_prefetch_total"
M_RECURSION_TIME = "homelab_unbound_recursion_time_seconds"
M_REQUESTLIST_CURRENT = "homelab_unbound_requestlist_current"
M_REQUESTLIST_EXCEEDED = "homelab_unbound_requestlist_exceeded_total"
M_EXTENDED_ENABLED = "homelab_pihole_unbound_extended_stats_enabled"
M_API_TOOK = "homelab_pihole_api_took_seconds"
M_QUERY_TYPE = "homelab_unbound_query_type"
M_ANSWER_RCODE = "homelab_unbound_answer_rcode"
M_ANSWER_SECURE = "homelab_unbound_answer_secure_total"
M_ANSWER_BOGUS = "homelab_unbound_answer_bogus_total"

# Quantiles derived from the unbound recursion-time histogram.
_QUANTILES: list[tuple[float, str]] = [(0.5, "0.5"), (0.95, "0.95"), (0.99, "0.99")]

# Fetch timeout for docker-exec calls; < 15s collector timeout, > 5s access layer default.
_FETCH_TIMEOUT_SECONDS = 10.0

# (metric constant, raw suffix) pairs for the always-emitted default set.
_DEFAULT_METRICS: list[tuple[str, str]] = [
    (M_QUERIES, "num.queries"),
    (M_CACHE_HITS, "num.cachehits"),
    (M_CACHE_MISSES, "num.cachemiss"),
    (M_PREFETCH, "num.prefetch"),
    (M_REQUESTLIST_CURRENT, "requestlist.current.all"),
    (M_REQUESTLIST_EXCEEDED, "requestlist.exceeded"),
]

_QUERY_TYPE_PREFIX = "num.query.type."
_RCODE_PREFIX = "num.answer.rcode."
_HISTOGRAM_PREFIX = "histogram."
_HISTOGRAM_PARTS_COUNT = 2  # lo and hi split by ".to."


def _read(raw: dict[str, float], suffix: str) -> float | None:
    """Read ``total.<suffix>`` preferred, ``thread0.<suffix>`` fallback.

    Returns None when neither key is present (caller skips emission rather than
    emitting a misleading 0 for a missing key).
    """
    val = raw.get(f"total.{suffix}")
    if val is not None:
        return val
    return raw.get(f"thread0.{suffix}")


def _histogram_quantiles(raw: dict[str, float]) -> dict[str, float]:
    """Compute 0.5/0.95/0.99 recursion-time quantiles from histogram buckets.

    Bucket keys look like ``histogram.000000.008192.to.000000.016384`` where each
    side is a ``DDDDDD.DDDDDD`` seconds float (lo=0.008192, hi=0.016384) and the
    value is the count in that bucket. Returns ``{"0.5": v, "0.95": v, "0.99": v}``
    on success, or an EMPTY dict when no buckets parse or the total count is <= 0.
    """
    buckets: list[tuple[float, float, float]] = []
    for key, count in raw.items():
        if not key.startswith(_HISTOGRAM_PREFIX):
            continue
        body = key[len(_HISTOGRAM_PREFIX) :]
        parts = body.split(".to.")
        if len(parts) != _HISTOGRAM_PARTS_COUNT:
            continue
        try:
            lo = float(parts[0])
            hi = float(parts[1])
        except ValueError:
            continue
        buckets.append((lo, hi, count))

    if not buckets:
        return {}

    buckets.sort(key=lambda b: b[0])
    total = sum(count for _, _, count in buckets)
    if total <= 0:
        return {}

    result: dict[str, float] = {}
    for p, label in _QUANTILES:
        rank = p * total
        cum = 0.0
        for lo, hi, count in buckets:
            if count <= 0:
                continue
            if cum + count >= rank:
                value = lo + (hi - lo) * (rank - cum) / count
                value = min(value, hi)
                break
            cum += count
        else:  # pragma: no cover - unreachable when total>0 (last positive bucket always breaks)
            value = buckets[-1][1]
        result[label] = value
    return result


class UnboundStatsCollector(BaseCollector):
    """Emit unbound recursive-resolver stats; degrade when extended stats off."""

    name: ClassVar[str] = "unbound_stats"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    def __init__(
        self,
        *,
        socket_client: DockerSocketClient | None = None,
        unbound_config: PiholeUnboundConfig | None = None,
    ) -> None:
        """Initialize. socket_client + unbound_config injected by lifespan.py.

        ``unbound_config`` defaults to ``PiholeUnboundConfig()`` so
        ``self._cfg.container`` is always available; ``socket_client`` is the
        real None-guard (collector returns early when it is None).
        """
        self._socket_client: DockerSocketClient | None = socket_client
        self._cfg: PiholeUnboundConfig = unbound_config or PiholeUnboundConfig()

    async def run(self, ctx: CollectorContext) -> CollectorResult:  # noqa: PLR0912
        start = time.monotonic()
        emitted: list[int] = [0]

        if self._socket_client is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["client_unconfigured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        result = await fetch_unbound_stats(
            exec_backend=self._socket_client,
            container=self._cfg.container,
            timeout_seconds=_FETCH_TIMEOUT_SECONDS,
        )

        # api_took emitted regardless of payload shape (the fetch happened).
        took = time.monotonic() - start
        ctx.vm.write_gauge(M_API_TOOK, took, {"endpoint": "unbound/stats_noreset"})
        emitted[0] += 1

        if isinstance(result, UnboundError):
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted[0],
                errors=[f"{result.reason}: {result.message}"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        raw = result.raw

        ctx.vm.write_gauge(M_EXTENDED_ENABLED, 1.0 if result.extended_enabled else 0.0, {})
        emitted[0] += 1

        # Default set (always; skip any key that is absent).
        for metric, suffix in _DEFAULT_METRICS:
            val = _read(raw, suffix)
            if val is not None:
                ctx.vm.write_gauge(metric, val, {})
                emitted[0] += 1

        avg = _read(raw, "recursion.time.avg")
        if avg is not None:
            ctx.vm.write_gauge(M_RECURSION_TIME, avg, {"quantile": "avg"})
            emitted[0] += 1
        median = _read(raw, "recursion.time.median")
        if median is not None:
            ctx.vm.write_gauge(M_RECURSION_TIME, median, {"quantile": "median"})
            emitted[0] += 1

        # Derived cache hit ratio (skip when missing or denom == 0).
        hits = _read(raw, "num.cachehits")
        misses = _read(raw, "num.cachemiss")
        if hits is not None and misses is not None and (hits + misses) > 0:
            ratio = hits / (hits + misses)
            ctx.vm.write_gauge(M_CACHE_HIT_RATIO, ratio, {})
            emitted[0] += 1

        if result.extended_enabled:
            quantiles = _histogram_quantiles(raw)
            for q_label, value in quantiles.items():
                ctx.vm.write_gauge(M_RECURSION_TIME, value, {"quantile": q_label})
                emitted[0] += 1

            for key in sorted(raw):
                if key.startswith(_QUERY_TYPE_PREFIX):
                    qtype = key[len(_QUERY_TYPE_PREFIX) :]
                    ctx.vm.write_gauge(M_QUERY_TYPE, raw[key], {"type": qtype})
                    emitted[0] += 1

            for key in sorted(raw):
                if key.startswith(_RCODE_PREFIX):
                    rcode = key[len(_RCODE_PREFIX) :]
                    ctx.vm.write_gauge(M_ANSWER_RCODE, raw[key], {"rcode": rcode})
                    emitted[0] += 1

            secure = raw.get("num.answer.secure")
            if secure is not None:
                ctx.vm.write_gauge(M_ANSWER_SECURE, secure, {})
                emitted[0] += 1
            bogus = raw.get("num.answer.bogus")
            if bogus is not None:
                ctx.vm.write_gauge(M_ANSWER_BOGUS, bogus, {})
                emitted[0] += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
