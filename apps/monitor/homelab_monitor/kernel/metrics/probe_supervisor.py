"""ProbeSupervisor — registered Collector + per-container asyncio.Task model.

D-PROBE-RUNNER-HYBRID:
  - One registered Collector (this class). Periodic tick reconciles the
    set of per-container asyncio.Task instances against the DB.
  - Per-container long-lived asyncio.Tasks (spawned in lifespan) loop
    through enabled probes for ONE container, executing each on schedule.

D-PER-CONTAINER-CONCURRENCY:
  - Each per-container task runs probes serially within the container,
    preventing per-service DDOS.

D-PROBE-FAIL-NO-RESTART:
  - Probe execution failures are captured into probe_targets.last_status/
    last_error. They NEVER propagate out of the runner; they NEVER restart
    the container.

Self-metric (gauge, NOT counter; NO _total suffix per STAGE-003-005 code-review I2):
  homelab_collector_run_docker_probes_supervisor{phase, result}
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import timedelta
from typing import Any, ClassVar, Final

import httpx

from homelab_monitor.kernel.db.repositories.probe_targets_repository import (
    ProbeTargetRow,
    ProbeTargetsRepository,
)
from homelab_monitor.kernel.db.repositories.targets_repository import TargetsRepository
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.label_parser import ProbeDescriptor
from homelab_monitor.kernel.docker.probe_executor import (
    ProbeOutcome,
    execute_resolved_probe,
)
from homelab_monitor.kernel.docker.probe_resolver import resolve_probe
from homelab_monitor.kernel.docker.socket_client import DockerSocketClient
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

_DEFAULT_HOST_IP: Final[str] = "127.0.0.1"
_DEFAULT_TICK_FLOOR_SECONDS: Final[float] = 1.0
_DEFAULT_BACKOFF_AFTER_ERROR_SECONDS: Final[float] = 5.0


class ProbeSupervisor(BaseCollector):
    """Registered periodic Collector that reconciles per-container runner tasks."""

    name: ClassVar[str] = "docker_probes_supervisor"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "docker.probes_supervisor"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(
        self,
        *,
        db: SqliteRepository | None = None,
        http_client: httpx.AsyncClient | None = None,
        socket_client: DockerSocketClient | None = None,
        host_ip: str | None = None,
        exec_enabled: bool | None = None,
    ) -> None:
        # 5-kwarg DI; all defaultable to None for safe early-return in run().
        self._db: SqliteRepository | None = db
        self._http_client: httpx.AsyncClient | None = http_client
        self._socket_client: DockerSocketClient | None = socket_client
        self._host_ip: str = host_ip or _DEFAULT_HOST_IP
        self._exec_enabled: bool = exec_enabled if exec_enabled is not None else False
        self._per_container_tasks: dict[str, asyncio.Task[None]] = {}
        self._ctx: CollectorContext | None = None

    # ---- BaseCollector tick (periodic reconciliation) ----

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Periodic reconciliation: spawn tasks for new containers, cancel for vanished."""
        start = time.monotonic()
        self._ctx = ctx
        if self._db is None or self._http_client is None:
            ctx.log.warning("probe_supervisor.dependencies_unwired")
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["dependencies_unwired"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )
        repo = ProbeTargetsRepository(self._db)
        try:
            container_names = await repo.list_distinct_container_names_with_enabled_probes()
        except Exception as exc:  # pragma: no cover -- defensive
            ctx.log.warning("probe_supervisor.reconcile_query_failed", error=str(exc))
            self._emit_metric(ctx, phase="reconcile", result="error")
            return CollectorResult(
                ok=False,
                metrics_emitted=1,
                errors=[f"reconcile_query_failed: {exc}"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Spawn tasks for new container_names; cancel for vanished ones.
        active = set(self._per_container_tasks.keys())
        wanted = set(container_names)
        spawned = wanted - active
        cancelled = active - wanted
        for cn in spawned:
            self._spawn_container_task(ctx, cn)
        for cn in cancelled:
            await self._cancel_container_task(cn)

        ctx.log.info(
            "probe_supervisor.reconcile_complete",
            active_tasks=len(self._per_container_tasks),
            spawned=len(spawned),
            cancelled=len(cancelled),
        )
        self._emit_metric(ctx, phase="reconcile", result="ok")
        return CollectorResult(
            ok=True,
            metrics_emitted=1,
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    # ---- Lifecycle (called from lifespan) ----

    async def start_per_container_tasks(self, ctx: CollectorContext) -> None:
        """One-shot startup reconciliation; called by lifespan after scheduler.start()."""
        self._ctx = ctx
        if self._db is None:  # pragma: no cover -- lifespan always wires db
            return
        repo = ProbeTargetsRepository(self._db)
        try:
            container_names = await repo.list_distinct_container_names_with_enabled_probes()
        except Exception as exc:  # pragma: no cover -- defensive
            ctx.log.warning("probe_supervisor.startup_query_failed", error=str(exc))
            return
        for cn in container_names:
            self._spawn_container_task(ctx, cn)

    async def stop_per_container_tasks(self) -> None:
        """Cancel all per-container tasks."""
        for cn in list(self._per_container_tasks.keys()):
            await self._cancel_container_task(cn)

    def _spawn_container_task(self, ctx: CollectorContext, container_name: str) -> None:
        """Spawn a per-container probe loop task if not already running."""
        if container_name in self._per_container_tasks:
            return
        task = asyncio.create_task(
            self.run_container_probe_loop(ctx, container_name),
            name=f"probe_supervisor.container.{container_name}",
        )
        self._per_container_tasks[container_name] = task

    async def _cancel_container_task(self, container_name: str) -> None:
        """Cancel and await a per-container task."""
        task = self._per_container_tasks.pop(container_name, None)
        if task is None:  # pragma: no cover -- defensive
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ---- Per-container loop ----

    async def run_container_probe_loop(
        self,
        ctx: CollectorContext,
        container_name: str,
    ) -> None:
        """Long-lived loop. Iterates enabled probes for THIS container.

        Tick interval = min(interval_seconds) across this container's probes.
        Each probe is executed serially (per D-PER-CONTAINER-CONCURRENCY).
        Failures captured into DB, never propagated.
        """
        if self._db is None or self._http_client is None:  # pragma: no cover -- defensive guard
            return
        repo = ProbeTargetsRepository(self._db)
        targets_repo = TargetsRepository(self._db)
        while True:
            try:
                probes = await repo.list_for_container(container_name=container_name)
                enabled = [p for p in probes if p.enabled and p.hidden_at is None]
                if not enabled:  # pragma: no cover -- defensive; reconcile should cancel us
                    await asyncio.sleep(_DEFAULT_BACKOFF_AFTER_ERROR_SECONDS)
                    continue

                container_meta = await self._lookup_container_meta(targets_repo, container_name)
                tick_interval = max(
                    _DEFAULT_TICK_FLOOR_SECONDS,
                    float(min(p.interval_seconds for p in enabled)),
                )

                for probe in enabled:
                    await self._execute_one_probe(ctx, probe, container_meta)
                await asyncio.sleep(tick_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover -- defensive top-level catch
                ctx.log.warning(
                    "probe_supervisor.container_loop_error",
                    container_name=container_name,
                    error=str(exc),
                )
                await asyncio.sleep(_DEFAULT_BACKOFF_AFTER_ERROR_SECONDS)

    async def _execute_one_probe(
        self,
        ctx: CollectorContext,
        probe: ProbeTargetRow,
        container_meta: dict[str, Any],
    ) -> None:
        """Execute one probe; persist outcome; emit metrics. Never raises."""
        descriptor = ProbeDescriptor(
            kind=probe.kind,
            name=probe.name,
            raw_value=probe.target_value,
        )
        resolved = resolve_probe(
            descriptor,
            network_mode=str(container_meta.get("network_mode") or "bridge"),
            container_ip=container_meta.get("container_ip"),
            container_id=str(container_meta.get("container_id") or ""),
            host_ip=self._host_ip,
            exec_enabled=self._exec_enabled,
            exec_authorized=bool(container_meta.get("exec_authorized")),
        )
        if resolved is None:
            await self._persist_outcome(
                probe.id,
                ProbeOutcome(up=False, duration_seconds=0.0, error="not_resolvable"),
            )
            self._emit_probe_metrics(
                ctx, probe, ProbeOutcome(up=False, duration_seconds=0.0, error="not_resolvable")
            )
            return
        outcome = await execute_resolved_probe(
            resolved,
            http_client=self._require_http(),
            socket_client=self._socket_client,
            timeout_seconds=float(probe.timeout_seconds),
        )
        await self._persist_outcome(probe.id, outcome)
        self._emit_probe_metrics(ctx, probe, outcome)

    def _require_http(self) -> httpx.AsyncClient:
        """Get HTTP client; guaranteed to be non-None by run() early-return guard."""
        assert self._http_client is not None
        return self._http_client

    async def _persist_outcome(self, probe_id: str, outcome: ProbeOutcome) -> None:
        """Update probe_targets row with execution outcome."""
        if self._db is None:  # pragma: no cover -- defensive; run() guards this
            return
        async with self._db.transaction() as conn:
            await ProbeTargetsRepository.update_run_outcome_conn(
                conn,
                probe_id=probe_id,
                status="ok" if outcome.up else "fail",
                error=outcome.error,
                now=utc_now_iso(),
            )

    async def _lookup_container_meta(
        self,
        targets_repo: TargetsRepository,
        container_name: str,
    ) -> dict[str, Any]:
        """Look up network_mode + container_id + container_ip for container_name.

        container_ip is fetched live via the docker socket's inspect endpoint
        (NetworkSettings.IPAddress or the first non-empty Networks IP). For
        host-network containers (network_mode='host'), container_ip is left
        as None — the resolver collapses `container` sentinel to host_ip
        in that case.

        exec_authorized comes from the labels JSON.
        """
        rows = await targets_repo.list_docker_containers(include_hidden=False)
        match = next((r for r in rows if r.name == container_name), None)
        if match is None:
            if self._ctx is not None:
                self._ctx.log.warning(
                    "probe_supervisor.container_meta_missing",
                    container_name=container_name,
                )
            return {
                "network_mode": "bridge",
                "container_id": None,
                "container_ip": None,
                "exec_authorized": False,
            }
        exec_authorized = (  # pragma: no cover -- requires populated targets_docker
            match.labels.get("homelab-monitor.exec_authorized", "").strip().lower() == "true"
        )
        network_mode = (
            match.network_mode or "bridge"
        )  # pragma: no cover -- requires populated targets_docker
        container_ip = await self._fetch_container_ip(
            match.container_id, network_mode
        )  # pragma: no cover -- requires populated targets_docker
        return {  # pragma: no cover -- requires populated targets_docker
            "network_mode": network_mode,
            "container_id": match.container_id,
            "container_ip": container_ip,
            "exec_authorized": exec_authorized,
        }

    async def _fetch_container_ip(self, container_id: str | None, network_mode: str) -> str | None:
        """Inspect the container to find its IP address.

        Returns None when:
        - socket_client is not wired (test mode)
        - container_id is None
        - network_mode is "host" (caller substitutes host_ip in resolver)
        - inspect fails or no IP is available
        """
        if self._socket_client is None or container_id is None:
            return None
        if network_mode == "host":
            return None
        try:  # pragma: no cover -- defensive; docker socket error is environment-specific
            inspect = await self._socket_client.inspect_container(container_id)
        except Exception as exc:  # pragma: no cover
            if self._ctx is not None:  # pragma: no cover
                self._ctx.log.warning(  # pragma: no cover
                    "probe_supervisor.container_inspect_failed",
                    container_id=container_id,
                    error=str(exc),
                )
            return None
        network_settings = inspect.get("NetworkSettings") or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
        # Prefer the top-level IPAddress (default bridge / single-network case).
        ip = network_settings.get("IPAddress")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
        if ip:
            return str(ip)  # pyright: ignore[reportUnknownArgumentType]
        # Fall back to the first non-empty IP from Networks dict.
        networks = network_settings.get("Networks") or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
        if isinstance(
            networks, dict
        ):  # pragma: no cover -- defensive; docker response should always have this
            for net_info in networks.values():  # pyright: ignore[reportUnknownVariableType]  # pragma: no cover -- defensive
                if isinstance(net_info, dict):  # pragma: no cover -- defensive
                    net_ip = net_info.get("IPAddress")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]  # pragma: no cover -- defensive
                    if net_ip:  # pragma: no cover -- defensive
                        return str(net_ip)  # pyright: ignore[reportUnknownArgumentType]
        return None

    # ---- Metrics ----

    @staticmethod
    def _emit_metric(ctx: CollectorContext, *, phase: str, result: str) -> None:
        """Emit homelab_collector_run_docker_probes_supervisor gauge."""
        ctx.vm.write_gauge(
            "homelab_collector_run_docker_probes_supervisor",
            1.0,
            {"phase": phase, "result": result},
        )

    @staticmethod
    def _emit_probe_metrics(
        ctx: CollectorContext,
        probe: ProbeTargetRow,
        outcome: ProbeOutcome,
    ) -> None:
        """Emit homelab_probe_up and homelab_probe_duration_seconds gauges."""
        labels = {
            "container": probe.container_name,
            "kind": probe.kind,
            "name": probe.name,
        }
        ctx.vm.write_gauge("homelab_probe_up", 1.0 if outcome.up else 0.0, labels)
        ctx.vm.write_gauge("homelab_probe_duration_seconds", outcome.duration_seconds, labels)


__all__ = ["ProbeSupervisor"]
