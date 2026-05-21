"""Docker socket collector — enumerate containers, cache cpu/mem from VM.

STAGE-003-004: Periodically queries the Docker Engine API over
/var/run/docker.sock to list all containers (running + exited), inspects each
for metadata (status, restart count, healthcheck, image, labels, network mode),
and merges cached cpu/mem percentiles from VictoriaMetrics. Persists to
targets + targets_docker sidecar, emits homelab_container_* metrics.

D-DOCKER-SDK-MANUAL-HTTPX: No external Docker SDK; we own the two endpoints:
  - GET /containers/json?all=true    (list)
  - GET /containers/{id}/json        (inspect)

D-SOCKET-READ-ONLY: Never invokes write operations. Socket mount is :ro.
D-MISSING-NOT-DELETED: Containers no longer seen are marked status='missing',
  not deleted (preserves history for alerting + UI drill-down).
D-CADVISOR-VS-SOCKET: VM query errors don't fail the tick; cpu/mem stay cached.
T-HEALTHCHECK: Normalize Health.Status to {"healthy","unhealthy","starting"} or None.
T-MERGE-LOCATION: cadvisor metrics cached into targets_docker via VM batch query.
D-PER-CONTAINER-CARDINALITY: Per-container metric cardinality accepted (~150 series at
  homelab scale; well under VM cardinality budget). See STAGE-003-004.md.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar, TypedDict

import httpx
import structlog

from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.socket_client import (
    ContainerInspect,
    DockerSocketClient,
    DockerSocketError,
)
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel


class _PromVectorEntry(TypedDict):
    """Single vector result entry from Prometheus /api/v1/query."""

    metric: dict[str, str]
    value: tuple[float, str]  # [timestamp, value-as-string]


class _PromQueryData(TypedDict):
    """Prometheus query data section."""

    resultType: str
    result: list[_PromVectorEntry]


class _PromQueryResponse(TypedDict):
    """Full Prometheus /api/v1/query response structure."""

    data: _PromQueryData


@dataclass(frozen=True, slots=True)
class DockerContainerRecord:
    """Extracted container metadata for batch processing."""

    id: str
    name: str
    state: str
    restart_count: int
    exit_code: int
    healthcheck: str | None
    image: str
    network_mode: str
    labels: dict[str, str]


class DockerSocketCollector(BaseCollector):
    """Collect container inventory + status from Docker socket.

    Single instance per process; collector scheduler reuses it across ticks.
    Dependencies (client, vm_url) injected by lifespan.py post-construction.
    """

    name: ClassVar[str] = "docker_socket"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=20)
    concurrency_group: ClassVar[str] = "docker.socket"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    # Healthcheck normalization (T-HEALTHCHECK)
    _ALLOWED_HEALTHCHECK: ClassVar[frozenset[str]] = frozenset({"healthy", "unhealthy", "starting"})

    def __init__(
        self,
        *,
        client: DockerSocketClient | None = None,
        vm_url: str | None = None,
    ) -> None:
        """Initialize. client and vm_url injected by lifespan.py."""
        self._client: DockerSocketClient | None = client
        self._vm_url: str | None = vm_url

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Tick: list containers, inspect each, merge cadvisor cpu/mem, upsert DB."""
        start = time.monotonic()
        errors: list[str] = []

        # Step 1: Verify client configured
        if self._client is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["client_unconfigured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Step 2: List containers
        try:
            entries = await self._client.list_containers()
        except DockerSocketError as exc:
            ctx.log.warning("docker_socket.list_failed", error=str(exc))
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[f"list_failed: {exc}"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Step 3: Inspect each container
        seen_ids: set[str] = set()
        records: list[DockerContainerRecord] = []

        for entry in entries:
            try:
                inspect = await self._client.inspect_container(entry["Id"])
            except DockerSocketError as exc:
                ctx.log.warning(
                    "docker_socket.inspect_failed",
                    id=entry["Id"],
                    error=str(exc),
                )
                errors.append(f"inspect_failed({entry['Id'][:12]}): {exc}")
                continue

            # Extract metadata
            name = self._strip_name(inspect["Name"] or "/<unknown>")
            state = self._extract_state(inspect)
            restart_count = int(
                inspect.get("RestartCount") or inspect.get("State", {}).get("RestartCount", 0)
            )
            exit_code = int(inspect["State"].get("ExitCode", 0))
            healthcheck = self._normalize_healthcheck(
                inspect["State"].get("Health", {}).get("Status"),
                ctx.log,
            )
            labels = entry.get("Labels") or {}
            image_ref = entry["Image"]
            network_mode = inspect.get("HostConfig", {}).get("NetworkMode", "default")

            records.append(
                DockerContainerRecord(
                    id=entry["Id"],
                    name=name,
                    state=state,
                    restart_count=restart_count,
                    exit_code=exit_code,
                    healthcheck=healthcheck,
                    image=image_ref,
                    network_mode=network_mode,
                    labels=labels,
                )
            )
            seen_ids.add(entry["Id"])

        # Step 4: Query VM for cpu/mem (batch)
        try:
            cpu_mem = await self._query_vm_cpu_mem(ctx, [r.name for r in records])
        except (httpx.HTTPError, ValueError) as exc:
            ctx.log.warning("docker_socket.vm_query_failed", error=str(exc))
            cpu_mem = {}  # Leave cached values stale

        # Step 5: Upsert DB rows + mark missing
        metrics_emitted = 0
        now = utc_now_iso()
        async with ctx.db.transaction() as conn:
            for r in records:
                cpu, mem = cpu_mem.get(r.name, (None, None))
                await TargetsRepository.upsert_docker_container_conn(
                    conn,
                    target_id=r.id,
                    name=r.name,
                    status=r.state,
                    image=r.image,
                    restart_count=r.restart_count,
                    exit_code=r.exit_code,
                    healthcheck=r.healthcheck,
                    network_mode=r.network_mode,
                    labels=r.labels,
                    now=now,
                    cpu_pct=cpu,
                    mem_mib=mem,
                )

            # Mark containers no longer seen as 'missing'
            await TargetsRepository.mark_missing_except_conn(conn, seen_ids=seen_ids, now=now)

        # Step 6: Emit per-container metrics
        for r in records:
            labels = {"name": r.name, "id": r.id[:12]}
            # homelab_container_status: value always 1.0; state label carries the actual state.
            # Query `count by (state) (homelab_container_status)` for per-state counts.
            ctx.vm.write_gauge(
                "homelab_container_status",
                1.0,
                {**labels, "state": r.state},
            )
            ctx.vm.write_gauge("homelab_container_restart_count", float(r.restart_count), labels)
            ctx.vm.write_gauge("homelab_container_last_exit_code", float(r.exit_code), labels)
            metrics_emitted += 3
            if r.healthcheck is not None:
                # Healthcheck metric is only emitted when container has healthcheck configured.
                # Containers without healthcheck do NOT emit a series — use:
                #   present_over_time(homelab_container_healthcheck{name="X"}[5m])
                # to distinguish "no healthcheck" from "unhealthy".
                ctx.vm.write_gauge(
                    "homelab_container_healthcheck",
                    1.0 if r.healthcheck == "healthy" else 0.0,
                    {**labels, "status": r.healthcheck},
                )
                metrics_emitted += 1

        return CollectorResult(
            ok=(len(errors) == 0),
            metrics_emitted=metrics_emitted,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    @staticmethod
    def _normalize_healthcheck(raw: str | None, log: structlog.BoundLogger) -> str | None:  # pyright: ignore[reportArgumentType]
        """Normalize Docker healthcheck status.

        T-HEALTHCHECK: "none" and null -> None; known states pass through;
        unknown values log warning + return None.
        """
        if raw is None or raw == "none":
            return None
        if raw in DockerSocketCollector._ALLOWED_HEALTHCHECK:
            return raw
        log.warning("docker_socket.unknown_healthcheck", value=raw)
        return None

    @staticmethod
    def _strip_name(name_input: str) -> str:
        """Strip leading '/' from container name (Docker convention)."""
        if name_input.startswith("/"):
            return name_input[1:]
        return name_input

    @staticmethod
    def _extract_state(inspect: ContainerInspect) -> str:
        """Extract normalized state from inspect response."""
        state = inspect.get("State", {})
        status = state.get("Status")
        if isinstance(status, str):
            return status
        return "unknown"

    async def _query_vm_cpu_mem(
        self,
        ctx: CollectorContext,
        container_names: list[str],
    ) -> dict[str, tuple[float | None, float | None]]:
        """Query VictoriaMetrics for cadvisor cpu/mem percentiles.

        Returns dict[name, (cpu_pct, mem_mib)]. Names not in VM stay absent.
        T-MERGE-LOCATION: cpu is rate(container_cpu_usage_seconds_total[1m]) * 100,
        mem is container_memory_usage_bytes / 1024 / 1024.
        """
        if not self._vm_url or not container_names:
            return {}

        result: dict[str, tuple[float | None, float | None]] = {}

        # Query CPU
        cpu_query = 'rate(container_cpu_usage_seconds_total{name=~".+"}[1m]) * 100'
        cpu_data = await self._query_prometheus(ctx, cpu_query)
        for entry in cpu_data:
            metric = entry.get("metric", {})
            value_pair = entry.get("value", [None, None])
            name = metric.get("name")
            value_str = value_pair[1]
            if name and value_str:
                with contextlib.suppress(ValueError):
                    result[name] = (float(value_str), None)

        # Query Memory
        mem_query = 'container_memory_usage_bytes{name=~".+"} / 1024 / 1024'
        mem_data = await self._query_prometheus(ctx, mem_query)
        for entry in mem_data:
            metric = entry.get("metric", {})
            value_pair = entry.get("value", [None, None])
            name = metric.get("name")
            value_str = value_pair[1]
            if name and value_str:
                with contextlib.suppress(ValueError):
                    mem_val = float(value_str)
                    if name in result:
                        result[name] = (result[name][0], mem_val)
                    else:
                        result[name] = (None, mem_val)

        return result

    async def _query_prometheus(
        self,
        ctx: CollectorContext,
        query: str,
    ) -> list[_PromVectorEntry]:
        """Query VictoriaMetrics /api/v1/query endpoint."""
        if not self._vm_url:
            return []

        url = f"{self._vm_url}/api/v1/query"
        resp = await ctx.http.get(url, params={"query": query})
        resp.raise_for_status()
        data: _PromQueryResponse = resp.json()  # pyright: ignore[reportAssignmentType]

        result_data = data.get("data", {})
        if result_data.get("resultType") == "vector":
            result = result_data.get("result", [])
            return result
        return []
