"""IO Protocols for the collector layer + in-memory test doubles.

Defines :class:`MetricsWriter`, :class:`LogsWriter`, :class:`SshClientFactory`,
:class:`SshConnection`, and :class:`HomeAssistantClient` as Protocol stubs. Real
implementations land in later stages:

- ``InMemoryMetricsWriter`` / ``InMemoryLogsWriter``: real now (test doubles).
- ``VictoriaMetricsWriter``: STAGE-001-015.
- ``VictoriaLogsWriter``: STAGE-001-016.
- ``SshClientFactory`` real impl: STAGE-017 (SSH probes).
- ``HomeAssistantClient`` real impl: EPIC-005.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast, runtime_checkable

from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.ha.client import (
    HaConfigResult,
    HaErrorLogResult,
    HaServiceResult,
    HaState,
)
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.pihole.client import PiholeResponse
from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.ssh.result import SshCommandResult
from homelab_monitor.kernel.unifi.client import UnifiResponse
from homelab_monitor.kernel.unifi.errors import UnifiError


@dataclass(frozen=True, slots=True)
class MetricEntry:
    """A single recorded metric write — used by :class:`InMemoryMetricsWriter`.

    ``kind`` is one of ``"gauge"``, ``"counter"``, ``"summary"``. ``labels`` is
    a flat dict of label name -> value.
    """

    kind: Literal["gauge", "counter", "summary"]
    name: str
    value: float
    labels: dict[str, str] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class LatestMetricEntry:
    """Latest-value entry retained by :class:`MemoryRetainingMetricsWriter`.

    Carries ``ts`` and ``kind`` for the snapshot endpoint. NOT used by the
    base :class:`InMemoryMetricsWriter` (which keeps the leaner :class:`MetricEntry`
    for the IPC-bound buffering path).
    """

    name: str
    value: float
    labels: dict[str, str]
    kind: Literal["gauge", "counter", "summary"]
    ts: str


@dataclass(frozen=True, slots=True)
class LogEntry:
    """A single recorded log write — used by :class:`InMemoryLogsWriter`."""

    stream: str
    line: str
    ts: str
    service: str | None = None
    source_type: str | None = None
    client_ip: str | None = None


@runtime_checkable
class MetricsWriter(Protocol):
    """Minimal symmetric metrics-write surface.

    Production wires this to a ``MultiplexMetricsWriter`` that fans out to
    ``MemoryRetainingMetricsWriter`` (for ``/api/metrics/snapshot``) AND
    ``PrometheusRegistryWriter`` (for ``/metrics`` scrape by vmagent into
    VictoriaMetrics). All three methods take a single observation.
    """

    def write_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a gauge observation."""
        ...

    def write_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a counter increment."""
        ...

    def write_counter_absolute(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record an absolute cumulative counter value (SET, not increment).

        Backed by a Gauge so VictoriaMetrics still computes ``rate()`` /
        ``increase()`` correctly; the caller passes the kernel's absolute
        cumulative total each tick (e.g. psutil ``read_bytes``).
        """
        ...

    def write_summary(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a summary observation."""
        ...


@runtime_checkable
class LogsWriter(Protocol):
    """Minimal log-ingest surface. ``ts`` defaults to "now" (UTC ISO).

    ``service`` / ``source_type`` / ``client_ip`` are OPTIONAL queryable identity
    fields. When provided they are written into the VL event dict as top-level
    fields so the logs query filter (``service:"X" AND source_type:"Y"`` or an exact
    ``client_ip:"1.2.3.4"`` forensic filter) can scope to the stream. When omitted the
    event carries only the builtins (backward-compatible).
    """

    def ingest(  # noqa: PLR0913 -- queryable VL identity fields (service/source_type/client_ip), kw-only
        self,
        stream: str,
        line: str,
        ts: str | None = None,
        *,
        service: str | None = None,
        source_type: str | None = None,
        client_ip: str | None = None,
    ) -> None:
        """Record a single log line on ``stream``."""
        ...


@runtime_checkable
class SshConnection(Protocol):
    """A live SSH connection that runs a single command (STAGE-017-001).

    The concrete implementation is
    :class:`homelab_monitor.kernel.ssh.client._AsyncSshConnection`. A non-zero
    ``exit_status`` is NOT an error; transport failures raise the
    ``kernel.ssh.errors`` hierarchy instead.
    """

    async def run(self, command: str = "") -> SshCommandResult:
        """Run ``command`` on the remote and return its captured output."""
        ...


@runtime_checkable
class SshClientFactory(Protocol):
    """Opens connections to known targets, yielding via async context manager."""

    def open(self, target_id: str) -> AbstractAsyncContextManager[SshConnection]:
        """Open a connection to ``target_id`` and yield an :class:`SshConnection`.

        SCAFFOLDING: real impl in EPIC-017 SSH probes.
        """
        ...


@runtime_checkable
class HomeAssistantClient(Protocol):
    """REST surface for reaching Home Assistant (STAGE-005-001).

    Every method is return-not-raise: it returns a success result OR an
    :class:`HaError`, so an HA 5xx / timeout / auth failure never propagates
    as our own 5xx. The concrete implementation is
    :class:`homelab_monitor.kernel.ha.client.HomeAssistantRestClient`.
    """

    async def get_config(self) -> HaConfigResult | HaError:
        """GET /api/config — HA version + time_zone, or a typed HaError."""
        ...

    async def get_states(self) -> list[HaState] | HaError:
        """GET /api/states — list of entity state objects, or a typed HaError."""
        ...

    async def get_error_log(self) -> HaErrorLogResult | HaError:
        """GET /api/error_log — plain-text error log, or a typed HaError."""
        ...

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> HaServiceResult | HaError:
        """POST /api/services/<domain>/<service> — changed states, or a typed HaError."""
        ...


@runtime_checkable
class UnifiClient(Protocol):
    """Read-only REST surface for reaching the Unifi controller (STAGE-007-001).

    Every method is return-not-raise: it returns a :class:`UnifiResponse` OR a
    :class:`UnifiError`, so a UDM 5xx / timeout / auth failure never propagates as
    our own 5xx. OBSERVE-ONLY — only GET helpers exist. The concrete implementation
    is :class:`homelab_monitor.kernel.unifi.client.UnifiRestClient`.

    SCAFFOLDING: collectors consuming these helpers land in Wave B/C
    (STAGE-007-005..014).
    """

    # Classic API site NAME (default "default"); used in /api/s/{name}/ paths.
    site_name: str
    # v1 site UUID resolved from v1/sites; used in /sites/{uuid}/ paths.
    v1_site_id: str

    async def v1_sites(self) -> UnifiResponse | UnifiError:
        """GET v1 /sites — controller sites, or a typed UnifiError."""
        ...

    async def v1_devices(self) -> UnifiResponse | UnifiError:
        """GET v1 /sites/{site_id}/devices — device inventory, or a typed UnifiError."""
        ...

    async def v1_device(self, device_id: str) -> UnifiResponse | UnifiError:
        """GET v1 /devices/{device_id} — one device, or a typed UnifiError."""
        ...

    async def v1_device_stats(self, device_id: str) -> UnifiResponse | UnifiError:
        """GET v1 /devices/{device_id}/statistics/latest — latest stats, or a typed UnifiError."""
        ...

    async def v1_clients(self) -> UnifiResponse | UnifiError:
        """GET v1 /sites/{site_id}/clients — coarse client list, or a typed UnifiError."""
        ...

    async def stat_device(self) -> UnifiResponse | UnifiError:
        """GET classic stat/device — fat per-device records, or a typed UnifiError."""
        ...

    async def stat_sta(self) -> UnifiResponse | UnifiError:
        """GET classic stat/sta — active-client identity + stats, or a typed UnifiError."""
        ...

    async def stat_alluser(self) -> UnifiResponse | UnifiError:
        """GET classic stat/alluser — all known clients, or a typed UnifiError."""
        ...

    async def stat_health(self) -> UnifiResponse | UnifiError:
        """GET classic stat/health — subsystem/WAN health, or a typed UnifiError."""
        ...

    async def stat_stadpi(self) -> UnifiResponse | UnifiError:
        """GET classic stat/stadpi — per-client per-app DPI counters, or a typed UnifiError."""
        ...

    async def rest_networkconf(self) -> UnifiResponse | UnifiError:
        """GET classic rest/networkconf — DHCP/DNS config, or a typed UnifiError."""
        ...

    async def rest_alarm(self) -> UnifiResponse | UnifiError:
        """GET classic rest/alarm?archived=false — active alarms, or a typed UnifiError."""
        ...

    async def stat_sysinfo(self) -> UnifiResponse | UnifiError:
        """GET classic stat/sysinfo — controller version/system info, or a typed UnifiError."""
        ...

    async def v2_traffic(self, start_ms: int, end_ms: int) -> UnifiResponse | UnifiError:
        """GET v2 traffic — per-client per-app usage for [start_ms, end_ms] (epoch-ms)."""
        ...

    async def resolve_site_id(self) -> UnifiError | None:
        """Resolve + cache the site id from v1/sites; None on success, UnifiError on failure."""
        ...


@runtime_checkable
class PiholeClient(Protocol):
    """Read surface for reaching a Pi-hole v6 instance (STAGE-006-001).

    Every method is return-not-raise: it returns a :class:`PiholeResponse` OR a
    :class:`PiholeError`, so a Pi-hole 5xx / timeout / auth failure never propagates
    as our own 5xx. The concrete implementation is
    :class:`homelab_monitor.kernel.pihole.client.PiholeRestClient`.

    SCAFFOLDING: collectors consuming these helpers land in Wave B/C
    (STAGE-006-005..015).
    """

    async def info_version(self) -> PiholeResponse | PiholeError:
        """GET /api/info/version — Pi-hole / FTL version, or a typed PiholeError."""
        ...

    async def info_ftl(self) -> PiholeResponse | PiholeError:
        """GET /api/info/ftl — FTL process info, or a typed PiholeError."""
        ...

    async def info_database(self) -> PiholeResponse | PiholeError:
        """GET /api/info/database — query database info, or a typed PiholeError."""
        ...

    async def info_messages(self) -> PiholeResponse | PiholeError:
        """GET /api/info/messages — diagnostic messages, or a typed PiholeError."""
        ...

    async def info_system(self) -> PiholeResponse | PiholeError:
        """GET /api/info/system — host system metrics, or a typed PiholeError."""
        ...

    async def stats_summary(self) -> PiholeResponse | PiholeError:
        """GET /api/stats/summary — top-line query stats, or a typed PiholeError."""
        ...

    async def stats_upstreams(self) -> PiholeResponse | PiholeError:
        """GET /api/stats/upstreams — per-upstream counts, or a typed PiholeError."""
        ...

    async def stats_query_types(self) -> PiholeResponse | PiholeError:
        """GET /api/stats/query_types — per-type query counts, or a typed PiholeError."""
        ...

    async def stats_top_clients(
        self, *, blocked: bool = False, count: int | None = None
    ) -> PiholeResponse | PiholeError:
        """GET /api/stats/top_clients — top querying clients, or a typed PiholeError."""
        ...

    async def stats_top_domains(
        self, *, blocked: bool = False, count: int | None = None
    ) -> PiholeResponse | PiholeError:
        """GET /api/stats/top_domains — top queried domains, or a typed PiholeError."""
        ...

    async def stats_recent_blocked(self) -> PiholeResponse | PiholeError:
        """GET /api/stats/recent_blocked — recently blocked domains, or a typed PiholeError."""
        ...

    async def dns_blocking(self) -> PiholeResponse | PiholeError:
        """GET /api/dns/blocking — blocking enabled state, or a typed PiholeError."""
        ...

    async def config(self) -> PiholeResponse | PiholeError:
        """GET /api/config — full Pi-hole config, or a typed PiholeError."""
        ...

    async def lists(self) -> PiholeResponse | PiholeError:
        """GET /api/lists — adlists, or a typed PiholeError."""
        ...

    async def network_devices(self) -> PiholeResponse | PiholeError:
        """GET /api/network/devices — network device inventory, or a typed PiholeError."""
        ...

    async def queries(self, params: dict[str, str]) -> PiholeResponse | PiholeError:
        """GET /api/queries — recent queries (filtered by ``params``), or a typed PiholeError."""
        ...

    async def aclose(self) -> None:
        """Best-effort logout (DELETE /api/auth); never raises."""
        ...


class InMemoryMetricsWriter:
    """In-memory test double for :class:`MetricsWriter`. Records every call."""

    def __init__(self) -> None:
        self._entries: list[MetricEntry] = []
        # SCAFFOLDING: These timestamp logs are a temporary workaround to track
        # tick events. VictoriaMetrics queries (STAGE-015) will replace this
        # O(n) scan pattern.
        self._ts_log: list[tuple[str, str]] = []  # (metric_name, ts)
        self._collector_ts: dict[str, str] = {}  # collector_name -> last tick ISO ts

    @property
    def recorded(self) -> list[MetricEntry]:
        """Return all entries written since construction (insertion order)."""
        return list(self._entries)

    def write_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a gauge observation."""
        self._entries.append(MetricEntry(kind="gauge", name=name, value=value, labels=dict(labels)))

    def write_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a counter increment."""
        self._entries.append(
            MetricEntry(kind="counter", name=name, value=value, labels=dict(labels))
        )
        # Track timestamps for success/failure metrics
        if name in ("homelab_collector_run_success_total", "homelab_collector_run_failure_total"):
            now_iso = utc_now_iso()
            self._ts_log.append((name, now_iso))
            collector_name = labels.get("name")
            if collector_name is not None:
                self._collector_ts[collector_name] = now_iso

    def write_counter_absolute(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record an absolute cumulative counter value (stored as gauge-kind)."""
        self._entries.append(MetricEntry(kind="gauge", name=name, value=value, labels=dict(labels)))

    def write_summary(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a summary observation."""
        self._entries.append(
            MetricEntry(kind="summary", name=name, value=value, labels=dict(labels))
        )

    def last_tick_at(self) -> str | None:
        """ISO-8601 UTC timestamp of the most recent success/failure tick, or None."""
        if not self._ts_log:
            return None
        return self._ts_log[-1][1]

    def last_tick_at_for(self, collector: str) -> str | None:
        """ISO-8601 UTC timestamp of the most recent success/failure for the named collector."""
        return self._collector_ts.get(collector)

    def last_error_for(self, collector: str) -> str | None:
        """Reason of the most recent failure_total for the named collector, or None."""
        for entry in reversed(self._entries):
            if (
                entry.name == "homelab_collector_run_failure_total"
                and entry.labels.get("name") == collector
            ):
                return entry.labels.get("reason")
        return None

    def failures_in_window(self, seconds: int) -> int:
        """Count of failure_total emissions within the last `seconds` (UTC wall-clock).

        SCAFFOLDING: currently counts all failures regardless of `seconds`. Real
        time-windowing lands when VictoriaMetrics queries replace this in STAGE-015.
        """
        del seconds
        count = 0
        for entry in self._entries:
            if entry.name == "homelab_collector_run_failure_total":
                count += int(entry.value)
        return count


class MemoryRetainingMetricsWriter(InMemoryMetricsWriter):
    """:class:`InMemoryMetricsWriter` plus a latest-value-by-(name, frozen-labels) map.

    Used at process boot when no real backend is configured. Replaced by the
    VictoriaMetrics-backed writer in STAGE-001-015. Adds:

    - ``snapshot()`` — returns latest :class:`LatestMetricEntry` per (name, labels)
    - ``replace_family(name, entries)`` — atomically wipe + rewrite a family

    Append-only history (``recorded``) and tick-tracking helpers (``last_tick_at``,
    ``last_tick_at_for``, ``last_error_for``, ``failures_in_window``) inherit
    unchanged.
    """

    def __init__(self) -> None:
        super().__init__()
        self._latest: dict[tuple[str, frozenset[tuple[str, str]]], LatestMetricEntry] = {}

    def _set_latest(self, kind: str, name: str, value: float, labels: dict[str, str]) -> None:
        key = (name, frozenset(labels.items()))
        self._latest[key] = LatestMetricEntry(
            name=name,
            value=value,
            labels=dict(labels),
            kind=cast(Literal["gauge", "counter", "summary"], kind),
            ts=utc_now_iso(),
        )

    def write_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a gauge observation; also update the latest-value map."""
        super().write_gauge(name, value, labels)
        self._set_latest("gauge", name, value, labels)

    def write_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a counter increment; also update the latest-value map."""
        super().write_counter(name, value, labels)
        self._set_latest("counter", name, value, labels)

    def write_counter_absolute(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record an absolute counter value; also update the latest-value map."""
        super().write_counter_absolute(name, value, labels)
        self._set_latest("gauge", name, value, labels)

    def write_summary(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a summary observation; also update the latest-value map."""
        super().write_summary(name, value, labels)
        self._set_latest("summary", name, value, labels)

    @property
    def gauges(self) -> list[tuple[str, float, dict[str, str]]]:
        """All gauge writes as (name, value, labels) tuples, in order."""
        return [(e.name, e.value, e.labels) for e in self._entries if e.kind == "gauge"]

    def last_gauge(self, name: str) -> float | None:
        """Return the most-recently written value for gauge `name`, or None."""
        for e in reversed(self._entries):
            if e.kind == "gauge" and e.name == name:
                return e.value
        return None

    def replace_family(self, name: str, entries: list[tuple[float, dict[str, str]]]) -> None:
        """Wipe latest-value entries for ``name`` and replace with ``entries``.

        Each ``(value, labels)`` becomes a new gauge entry (kind="gauge"). Also
        appended to the inherited ``_entries`` list — history stays append-only.

        Atomicity: this method has no awaits, so concurrent ``snapshot()``
        readers on the same event loop see either pre- or post-replacement
        state for THIS family. Multi-family ticks (e.g. consecutive
        replace_family calls for two different metric families) are NOT
        atomic across families — a snapshot mid-tick can observe one family
        updated and another stale. Acceptable for v1 Overview tile use case;
        revisit when VM-backed writer ships in STAGE-001-015.
        """
        stale_keys = [k for k in self._latest if k[0] == name]
        for k in stale_keys:
            del self._latest[k]
        for value, labels in entries:
            self.write_gauge(name, value, labels)

    def snapshot(self) -> list[LatestMetricEntry]:
        """Return all currently-retained latest entries (insertion order).

        Returns a defensive list copy; safe to iterate while subsequent
        writes mutate the underlying ``_latest`` dict.
        """
        return list(self._latest.values())


class InMemoryLogsWriter:
    """In-memory test double for :class:`LogsWriter`. Records every call."""

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []

    @property
    def recorded(self) -> list[LogEntry]:
        """Return all entries written since construction (insertion order)."""
        return list(self._entries)

    def ingest(  # noqa: PLR0913 -- queryable VL identity fields (service/source_type/client_ip), kw-only
        self,
        stream: str,
        line: str,
        ts: str | None = None,
        *,
        service: str | None = None,
        source_type: str | None = None,
        client_ip: str | None = None,
    ) -> None:
        """Record a single log line on ``stream``; defaults ``ts`` to current UTC."""
        self._entries.append(
            LogEntry(
                stream=stream,
                line=line,
                ts=ts or utc_now_iso(),
                service=service,
                source_type=source_type,
                client_ip=client_ip,
            )
        )
