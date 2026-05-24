"""LocalBuildUpdateCollector (STAGE-003-009).

Periodic source-hash scan for locally-built containers (compose entries
with `build:`). Same metric family as ImageUpdateCollector
(D-SHARED-METRIC-FAMILY); discriminated by source="local_build" label.

Tick interval: 30 min default (configurable via
HOMELAB_MONITOR_LOCAL_BUILD_INTERVAL_SECONDS); dev override is 60.

D-PER-IMAGE-FAILURE-ISOLATION: per-container try/except.
D-FIRST-CHECK-BASELINE: first ever check stores hash, emits metric=0.
D-HASHING-LIMITS-STAGEDOC-PLUS-ABORT: on exceed, persist sentinel hash
that always-differs from any prior hash → update_available=1.

Self-metric: homelab_collector_run_local_build_update_checker{phase, result}
Skipped counter: homelab_build_source_hash_skipped_total{reason=...}
Compose-readability gauge: homelab_docker_compose_readable{} 0/1
"""

from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final, TypedDict

from homelab_monitor.kernel.db.repositories.docker_build_hashes_repository import (
    DockerBuildHashesRepository,
    DockerBuildHashRow,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.compose_reader import (
    ComposeReadError,
    read_compose_set,
)
from homelab_monitor.kernel.docker.names import canonicalize_container_name
from homelab_monitor.kernel.docker.path_resolver import PathResolver
from homelab_monitor.kernel.docker.socket_client import DockerSocketClient
from homelab_monitor.kernel.docker.source_hash import (
    SourceHashLimits,
    SourceHashResult,
    compute_source_hash,
)
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

if TYPE_CHECKING:
    from homelab_monitor.kernel.docker.build_sources_loader import BuildSourcesLoader

_DEFAULT_INTERVAL_SECONDS: Final[int] = 1800  # 30 min
_DEFAULT_TIMEOUT_SECONDS: Final[int] = 300


class _UpsertPayload(TypedDict):
    container_name: str
    compose_service: str
    build_context_path: str
    last_source_hash: str | None
    last_checked_at: str
    check_failed_at: str | None
    check_error_reason: str | None
    update_available: bool
    baseline_source_hash: str | None
    baseline_image_id: str | None


def _resolve_interval_seconds() -> int:
    raw = os.environ.get("HOMELAB_MONITOR_LOCAL_BUILD_INTERVAL_SECONDS")
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        v = int(raw)
        if v < 1:
            return _DEFAULT_INTERVAL_SECONDS
        return v
    except ValueError:
        return _DEFAULT_INTERVAL_SECONDS


class LocalBuildUpdateCollector(BaseCollector):
    """Periodic build-context source-hash scanner."""

    name: ClassVar[str] = "local_build_update_checker"
    interval: ClassVar[timedelta] = timedelta(seconds=_resolve_interval_seconds())
    timeout: ClassVar[timedelta] = timedelta(seconds=_DEFAULT_TIMEOUT_SECONDS)
    concurrency_group: ClassVar[str] = "docker.image_updates"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(  # noqa: PLR0913 -- DI surface; mirrors ImageUpdateCollector
        self,
        *,
        db: SqliteRepository | None = None,
        socket_client: DockerSocketClient | None = None,
        build_hashes_repo: DockerBuildHashesRepository | None = None,
        compose_dir: Path | None = None,
        compose_filename: str = "docker-compose.yml",
        limits: SourceHashLimits | None = None,
        build_sources_loader: BuildSourcesLoader | None = None,
    ) -> None:
        self._db: SqliteRepository | None = db
        self._socket_client: DockerSocketClient | None = socket_client
        self._build_hashes_repo: DockerBuildHashesRepository | None = build_hashes_repo
        self._compose_dir: Path | None = compose_dir
        self._compose_filename: str = compose_filename
        self._limits: SourceHashLimits = limits or SourceHashLimits.from_env()
        self._build_sources_loader: BuildSourcesLoader | None = build_sources_loader

    async def run(self, ctx: CollectorContext) -> CollectorResult:  # noqa: PLR0912, PLR0911, PLR0915 -- collector orchestrates 7-step flow with mode dispatch (YAML vs env-var fallback); mirrors complexity of ImageUpdateCollector.collect
        start = time.monotonic()
        if self._db is None or self._socket_client is None or self._build_hashes_repo is None:
            ctx.log.warning("local_build_update_collector.dependencies_unwired")
            self._emit_self_metric(ctx, phase="tick", result="error")
            return CollectorResult(
                ok=False,
                metrics_emitted=1,
                errors=["dependencies_unwired"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Resolve compose paths and build-context remapper based on:
        # - YAML config if BuildSourcesLoader is present and config loaded
        # - Fallback to env-var HOMELAB_MONITOR_COMPOSE_DIR if no YAML
        # - Gracefully disabled (compose_readable=0) if neither available
        config = self._build_sources_loader.current_config if self._build_sources_loader else None
        loader_error = (
            self._build_sources_loader.current_error if self._build_sources_loader else None
        )
        config_loaded_metric = 0.0
        compose_paths: list[Path]
        resolver: PathResolver

        if config is not None:
            config_loaded_metric = 1.0
            compose_paths = [Path(e.container_path) for e in config.compose_files]
            resolver = PathResolver(config.build_context_roots)
        elif loader_error is not None:
            ctx.vm.write_gauge("homelab_build_sources_config_loaded", 0.0, {})
            ctx.vm.write_gauge("homelab_docker_compose_readable", 0.0, {})
            ctx.log.warning(
                "local_build_update_collector.build_sources_config_invalid",
                reason=loader_error.reason,
                error=str(loader_error),
            )
            self._emit_self_metric(ctx, phase="tick", result="error")
            return CollectorResult(
                ok=False,
                metrics_emitted=3,
                errors=[f"build_sources_config_invalid:{loader_error.reason}"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )
        else:
            if self._compose_dir is None:
                ctx.vm.write_gauge("homelab_build_sources_config_loaded", 0.0, {})
                ctx.vm.write_gauge("homelab_docker_compose_readable", 0.0, {})
                ctx.log.info("local_build_update_collector.compose_dir_unset")
                self._emit_self_metric(ctx, phase="tick", result="ok")
                return CollectorResult(
                    ok=True,
                    metrics_emitted=3,
                    errors=[],
                    events=[],
                    duration_seconds=time.monotonic() - start,
                )
            compose_paths = [self._compose_dir / self._compose_filename]
            resolver = PathResolver([])  # identity

        ctx.vm.write_gauge("homelab_build_sources_config_loaded", config_loaded_metric, {})

        try:
            compose = read_compose_set(compose_paths, path_resolver=resolver, log=ctx.log)
        except ComposeReadError as exc:
            ctx.vm.write_gauge("homelab_docker_compose_readable", 0.0, {})
            ctx.log.warning(
                "local_build_update_collector.compose_read_failed",
                paths=[str(p) for p in compose_paths],
                reason=exc.reason,
                error=str(exc),
            )
            self._emit_self_metric(ctx, phase="tick", result="error")
            return CollectorResult(
                ok=False,
                metrics_emitted=3,
                errors=[f"compose_read_failed:{exc.reason}"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        ctx.vm.write_gauge("homelab_docker_compose_readable", 1.0, {})

        try:
            entries = await self._socket_client.list_containers()
        except Exception as exc:  # pragma: no cover -- defensive
            ctx.log.warning("local_build_update_collector.list_failed", error=str(exc))
            self._emit_self_metric(ctx, phase="tick", result="error")
            return CollectorResult(
                ok=False,
                metrics_emitted=3,
                errors=[f"list_failed: {exc}"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        now = utc_now_iso()
        metrics_emitted = (
            3  # build_sources_config_loaded + compose_readable + self_metric (added at end)
        )
        upsert_queue: list[_UpsertPayload] = []
        seen_names: set[str] = set()

        # Build a lookup: container_name (canonicalized) -> (compose_service, build_context).
        # Match by compose.service-label first, then by service-name. Container
        # entries that don't match any build: service are silently skipped.
        build_services = {
            svc_name: svc
            for svc_name, svc in compose.services.items()
            if svc.build_context is not None
        }
        if not build_services:
            # No locally-built services in this compose file — done.
            self._emit_self_metric(ctx, phase="tick", result="ok")
            metrics_emitted += 1
            ctx.log.info(
                "local_build_update_collector.no_build_services",
                num_build_services=len(build_services),
            )
            return CollectorResult(
                ok=True,
                metrics_emitted=metrics_emitted,
                errors=[],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Pattern A: Pre-fetch all prior hashes once before the per-container loop.
        # (IMPORTANT NOTE per spec Step 3.1)
        assert self._db is not None
        async with self._db.transaction() as conn:
            all_prior_rows = await DockerBuildHashesRepository.list_all_conn(conn)
        prior_rows_by_name: dict[str, DockerBuildHashRow] = {
            r.container_name: r for r in all_prior_rows
        }

        for entry in entries:
            raw_names = entry.get("Names") or []
            if not raw_names:
                continue
            container_name = canonicalize_container_name(str(raw_names[0]))
            labels_raw = entry.get("Labels") or {}
            labels: dict[str, str] = {str(k): str(v) for k, v in labels_raw.items()}
            compose_service = labels.get("com.docker.compose.service") or container_name
            svc = build_services.get(compose_service)
            if svc is None or svc.build_context is None:
                continue
            seen_names.add(container_name)

            try:
                prior_row = prior_rows_by_name.get(container_name)
                payload = self._process_one_container(
                    ctx,
                    container_name=container_name,
                    compose_service=compose_service,
                    build_context=svc.build_context,
                    now=now,
                    prior_row=prior_row,
                    current_image_id=str(entry.get("ImageID") or ""),
                )
                if payload is not None:  # pragma: no branch -- always returns payload
                    upsert_queue.append(payload)
                    metrics_emitted += 1
            except Exception as exc:  # pragma: no cover -- defensive per-container guard
                ctx.log.warning(
                    "local_build_update_collector.per_container_failed",
                    container_name=container_name,
                    error=str(exc),
                )

        # Reconcile — delete rows for containers no longer present.
        async with self._db.transaction() as conn:
            existing = await DockerBuildHashesRepository.list_all_conn(conn)
            stale_names = {r.container_name for r in existing} - seen_names
            if stale_names:
                deleted = await DockerBuildHashesRepository.delete_by_container_conn(
                    conn, container_names=stale_names
                )
                ctx.log.info(
                    "local_build_update_collector.reconcile_deleted",
                    count=deleted,
                    names=sorted(stale_names),
                )
            for payload in upsert_queue:
                await DockerBuildHashesRepository.upsert_conn(conn, **payload)

        self._emit_self_metric(ctx, phase="tick", result="ok")
        metrics_emitted += 1

        ctx.log.info(
            "local_build_update_collector.tick_complete",
            containers_checked=len(seen_names),
            build_services_total=len(build_services),
            duration_seconds=time.monotonic() - start,
        )

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    def _process_one_container(  # noqa: PLR0913 -- payload builder; explicit param names for readability
        self,
        ctx: CollectorContext,
        *,
        container_name: str,
        compose_service: str,
        build_context: Path,
        now: str,
        prior_row: DockerBuildHashRow | None,
        current_image_id: str,
    ) -> _UpsertPayload | None:
        """Hash one container's build context. Returns payload (or None on hard skip)."""
        if not build_context.exists():
            ctx.vm.write_gauge(
                "homelab_build_source_hash_skipped_total",
                1.0,
                {"reason": "context_missing", "name": container_name},
            )
            # Preserve check_failed_at if same error reason (don't reset timestamp)
            check_failed_at = (
                prior_row.check_failed_at
                if prior_row and prior_row.check_error_reason == "context_missing"
                else now
            )
            return {
                "container_name": container_name,
                "compose_service": compose_service,
                "build_context_path": str(build_context),
                "last_source_hash": None,
                "last_checked_at": now,
                "check_failed_at": check_failed_at,
                "check_error_reason": "context_missing",
                "update_available": False,
                "baseline_source_hash": prior_row.baseline_source_hash if prior_row else None,
                "baseline_image_id": prior_row.baseline_image_id if prior_row else None,
            }

        result: SourceHashResult = compute_source_hash(build_context, limits=self._limits)

        if result.exceeded is not None:
            ctx.vm.write_gauge(
                "homelab_build_source_hash_skipped_total",
                1.0,
                {"reason": result.exceeded, "name": container_name},
            )
            ctx.vm.write_gauge(
                "homelab_image_update_available",
                1.0,
                {
                    "name": container_name,
                    "image": f"local-build:{compose_service}",
                    "source": "local_build",
                    "current_digest": result.hash,  # sentinel hash
                    "latest_digest": result.hash,
                },
            )
            # Preserve check_failed_at if same error reason (don't reset timestamp)
            check_failed_at = (
                prior_row.check_failed_at
                if prior_row and prior_row.check_error_reason == result.exceeded
                else now
            )
            return {
                "container_name": container_name,
                "compose_service": compose_service,
                "build_context_path": str(build_context),
                "last_source_hash": result.hash,  # sentinel
                "last_checked_at": now,
                "check_failed_at": check_failed_at,
                "check_error_reason": result.exceeded,  # "context_too_large" or "permission_denied"
                "update_available": True,
                "baseline_source_hash": prior_row.baseline_source_hash if prior_row else None,
                "baseline_image_id": prior_row.baseline_image_id if prior_row else None,
            }

        # Determine baseline and update_available per 3-case design:
        # Case 0: missing/empty image_id from socket → treat as first check (defensive)
        # Case 1: first ever check (no row) OR image mismatch
        if (
            prior_row is None
            or not current_image_id
            or (current_image_id and prior_row.baseline_image_id != current_image_id)
        ):
            baseline_source_hash = result.hash
            baseline_image_id = current_image_id
            update_available = False
        # Case 3: image_id unchanged → compare against stable baseline
        else:
            baseline_source_hash = prior_row.baseline_source_hash
            baseline_image_id = prior_row.baseline_image_id
            update_available = (
                baseline_source_hash is not None and baseline_source_hash != result.hash
            )

        ctx.vm.write_gauge(
            "homelab_image_update_available",
            1.0 if update_available else 0.0,
            {
                "name": container_name,
                "image": f"local-build:{compose_service}",
                "source": "local_build",
                "current_digest": baseline_source_hash or "",
                "latest_digest": result.hash,
            },
        )
        return {
            "container_name": container_name,
            "compose_service": compose_service,
            "build_context_path": str(build_context),
            "last_source_hash": result.hash,
            "last_checked_at": now,
            "check_failed_at": None,
            "check_error_reason": None,
            "update_available": update_available,
            "baseline_source_hash": baseline_source_hash,
            "baseline_image_id": baseline_image_id,
        }

    @staticmethod
    def _emit_self_metric(ctx: CollectorContext, *, phase: str, result: str) -> None:
        ctx.vm.write_gauge(
            "homelab_collector_run_local_build_update_checker",
            1.0,
            {"phase": phase, "result": result},
        )


__all__ = ["LocalBuildUpdateCollector"]
