"""ImageUpdateCollector (STAGE-003-008).

BaseCollector with 6h default tick (D-COLLECTOR-CADENCE-6H). Walks
running containers, fetches local + registry digests, emits
homelab_image_update_available gauge, persists state to
image_update_state.

D-PER-IMAGE-FAILURE-ISOLATION: per-container try/except.
D-RATE-LIMIT-HARD-CAP-PLUS-BANNER: skips remaining checks for a registry
when last-known rate_limit_remaining < hard_cap (default 10); emits
homelab_image_update_check_skipped{reason='rate_limit'} 1 + tracks
skipped_count for the API banner.

Self-metric: homelab_collector_run_image_update_checker{phase, result}
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import ClassVar, Final, TypedDict, cast

import httpx

from homelab_monitor.kernel.db.repositories.image_update_state_repository import (
    ImageUpdateStateRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.image_ref_parser import (
    ImageRefParseError,
    parse_image_ref,
)
from homelab_monitor.kernel.docker.names import canonicalize_container_name
from homelab_monitor.kernel.docker.registry_digest_client import (
    FetchedDigest,
    RegistryDigestClient,
)
from homelab_monitor.kernel.docker.socket_client import DockerSocketClient
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

_DEFAULT_INTERVAL_SECONDS: Final[int] = 21600  # 6h
_DEFAULT_TIMEOUT_SECONDS: Final[int] = 300
_DEFAULT_HARD_CAP_REMAINING: Final[int] = 10
_RATE_LIMIT_TTL_SECONDS: Final[int] = 3600  # 1h — rate-limit awareness TTL


class _UpsertPayload(TypedDict):
    """Batch upsert payload for a single container."""

    container_name: str
    last_image_ref: str
    last_local_digest: str | None
    last_registry_digest: str | None
    last_checked_at: str
    check_failed_at: str | None
    check_error_reason: str | None
    update_available: bool


def _resolve_interval_seconds() -> int:
    raw = os.environ.get("HOMELAB_MONITOR_IMAGE_UPDATE_INTERVAL_SECONDS")
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        v = int(raw)
        if v < 1:
            return _DEFAULT_INTERVAL_SECONDS
        return v
    except ValueError:  # pragma: no cover -- importlib.reload path not tracked
        return _DEFAULT_INTERVAL_SECONDS


class ImageUpdateCollector(BaseCollector):
    """Periodic registry-digest fetcher."""

    name: ClassVar[str] = "image_update_checker"
    # IMPORTANT: Resolved at MODULE IMPORT time. Env override
    # (HOMELAB_MONITOR_IMAGE_UPDATE_INTERVAL_SECONDS) must be set BEFORE this
    # module is first imported (in practice: in dev.env / .env / docker-compose
    # env_file loaded before the FastAPI app boots). Tests must use
    # importlib.reload(iuc_module) to pick up changes.
    interval: ClassVar[timedelta] = timedelta(seconds=_resolve_interval_seconds())
    # NOTE: resolved at import time from HOMELAB_MONITOR_IMAGE_UPDATE_INTERVAL_SECONDS env var.
    # Changing the env var requires process restart; the ClassVar is read once at module load.
    timeout: ClassVar[timedelta] = timedelta(seconds=_DEFAULT_TIMEOUT_SECONDS)
    concurrency_group: ClassVar[str] = "docker.image_updates"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(  # noqa: PLR0913
        self,
        *,
        db: SqliteRepository | None = None,
        socket_client: DockerSocketClient | None = None,
        registry_client: RegistryDigestClient | None = None,
        image_update_state_repo: ImageUpdateStateRepository | None = None,
        http_client: httpx.AsyncClient | None = None,
        hard_cap_remaining: int = _DEFAULT_HARD_CAP_REMAINING,
    ) -> None:
        self._db: SqliteRepository | None = db
        self._socket_client: DockerSocketClient | None = socket_client
        self._registry_client: RegistryDigestClient | None = registry_client
        self._state_repo: ImageUpdateStateRepository | None = image_update_state_repo
        self._http_client: httpx.AsyncClient | None = http_client
        self._hard_cap_remaining: int = hard_cap_remaining
        # Public read state (API surface).
        # Tuple: (remaining, recorded_at_monotonic). Entries older than 1h are ignored.
        self._last_rate_limit_remaining: dict[str, tuple[int, float]] = {}
        self._last_skipped_count: int = 0

    def _get_rate_limit_remaining(self, registry: str) -> int | None:
        """Return cached rate-limit remaining if fresh (< 1h), else None."""
        entry = self._last_rate_limit_remaining.get(registry)
        if entry is None:
            return None
        remaining, recorded_at = entry
        if (
            time.monotonic() - recorded_at > _RATE_LIMIT_TTL_SECONDS
        ):  # pragma: no cover -- TTL prune; requires >1h elapsed
            return None  # stale — treat as unknown
        return remaining

    def current_rate_limit_remaining(self) -> Mapping[str, int]:
        now = time.monotonic()
        return MappingProxyType(
            {
                reg: val
                for reg, (val, ts) in self._last_rate_limit_remaining.items()
                if now - ts <= _RATE_LIMIT_TTL_SECONDS
            }
        )

    def current_skipped_count(self) -> int:
        return self._last_skipped_count

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        start = time.monotonic()
        if (  # pragma: no cover -- defensive init guard
            self._db is None
            or self._socket_client is None
            or self._registry_client is None
            or self._state_repo is None
        ):
            ctx.log.warning("image_update_collector.dependencies_unwired")
            self._emit_self_metric(ctx, phase="tick", result="error")
            return CollectorResult(
                ok=False,
                metrics_emitted=1,
                errors=["dependencies_unwired"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        try:
            entries = await self._socket_client.list_containers()
        except Exception as exc:  # pragma: no cover -- defensive
            ctx.log.warning("image_update_collector.list_failed", error=str(exc))
            self._emit_self_metric(ctx, phase="tick", result="error")
            return CollectorResult(
                ok=False,
                metrics_emitted=1,
                errors=[f"list_failed: {exc}"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Reset per-tick counters BUT keep rate_limit_remaining across ticks
        # so the hard-cap check applies to the NEXT tick's window.
        self._last_skipped_count = 0
        metrics_emitted = 0
        now = utc_now_iso()
        upsert_queue: list[_UpsertPayload] = []

        for entry in entries:
            raw_names = entry.get("Names") or []
            if not raw_names:
                continue
            container_name = canonicalize_container_name(str(raw_names[0]))
            image_ref = str(entry.get("Image") or "")
            image_id = str(entry.get("ImageID") or "")
            try:
                metrics_count, payload = await self._process_one_container(
                    ctx,
                    container_name=container_name,
                    image_ref=image_ref,
                    image_id=image_id,
                    now=now,
                )
                metrics_emitted += metrics_count
                if payload is not None:
                    upsert_queue.append(payload)
            except Exception as exc:  # pragma: no cover -- defensive per-image guard
                ctx.log.warning(
                    "image_update_collector.per_container_failed",
                    container_name=container_name,
                    error=str(exc),
                )

        # C2: Reconcile — delete rows for containers no longer running.
        seen_names: set[str] = set()
        for entry in entries:
            raw_names = entry.get("Names") or []
            if raw_names:
                seen_names.add(canonicalize_container_name(str(raw_names[0])))

        assert self._db is not None
        async with self._db.transaction() as conn:
            existing_rows = await ImageUpdateStateRepository.list_all_conn(conn)
            stale_names = {r.container_name for r in existing_rows} - seen_names
            if stale_names:
                deleted = await ImageUpdateStateRepository.delete_by_container_conn(
                    conn, container_names=stale_names
                )
                ctx.log.info(
                    "image_update_collector.reconcile_deleted",
                    count=deleted,
                    names=sorted(stale_names),
                )
            # I2: Single transaction for all upserts
            for payload in upsert_queue:
                await ImageUpdateStateRepository.upsert_state_conn(conn, now=now, **payload)

        # Emit per-registry rate-limit gauge.
        for registry, remaining in self.current_rate_limit_remaining().items():
            ctx.vm.write_gauge(
                "homelab_registry_rate_limit_remaining",
                float(remaining),
                {"registry": registry},
            )
            metrics_emitted += 1

        self._emit_self_metric(ctx, phase="tick", result="ok")
        metrics_emitted += 1

        ctx.log.info(
            "image_update_collector.tick_complete",
            containers_total=len(entries),
            containers_skipped_rate_limit=self._last_skipped_count,
            rate_limit_observed_registries=list(self._last_rate_limit_remaining.keys()),
            duration_seconds=time.monotonic() - start,
        )

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    async def _process_one_container(
        self,
        ctx: CollectorContext,
        *,
        container_name: str,
        image_ref: str,
        image_id: str,
        now: str,
    ) -> tuple[int, _UpsertPayload | None]:
        """Process one container. Returns (metrics_count, payload).

        Payload is queued for batch upsert by run(); this method does not
        open DB transactions.
        """
        # 1. Parse image ref.
        try:
            parsed = parse_image_ref(image_ref)
        except ImageRefParseError:
            # Truly unparseable (e.g. <none>, sha256-only) -> skip silently.
            ctx.log.info(
                "image_update_collector.skip_unparseable",
                container_name=container_name,
                image_ref=image_ref,
            )
            return (0, None)

        registry = parsed.registry

        # 2. Rate-limit hard-cap check (D-RATE-LIMIT-HARD-CAP-PLUS-BANNER).
        remaining = self._get_rate_limit_remaining(registry)
        if remaining is not None and remaining < self._hard_cap_remaining:
            self._last_skipped_count += 1
            ctx.vm.write_gauge(
                "homelab_image_update_check_skipped",
                1.0,
                {"reason": "rate_limit", "registry": registry},
            )
            ctx.log.info(
                "image_update_collector.skip_rate_limit",
                container_name=container_name,
                registry=registry,
                remaining=remaining,
            )
            return (1, None)

        # 2.5: Retry-After cooldown check (I14)
        if self._registry_client is not None and hasattr(
            self._registry_client, "cooldown_until_for"
        ):  # pragma: no branch -- hasattr always True for real RegistryDigestClient
            cooldown_until = self._registry_client.cooldown_until_for(registry)
            if (
                isinstance(cooldown_until, (int, float)) and time.monotonic() < cooldown_until
            ):  # pragma: no cover -- requires real 429 with Retry-After
                self._last_skipped_count += 1
                ctx.vm.write_gauge(
                    "homelab_image_update_check_skipped",
                    1.0,
                    {"reason": "retry_after_cooldown", "registry": registry},
                )
                ctx.log.info(
                    "image_update_collector.skip_retry_after_cooldown",
                    container_name=container_name,
                    registry=registry,
                )
                return (1, None)

        # 3. Get local digest from image_inspect.
        assert self._socket_client is not None
        local_digest: str | None = None
        try:
            inspect_data = await self._socket_client.image_inspect(image_id)
        except Exception as exc:
            ctx.log.warning(
                "image_update_collector.image_inspect_failed",
                container_name=container_name,
                image_id=image_id,
                error=str(exc),
            )
            ctx.vm.write_gauge(
                "homelab_image_update_image_inspect_failures",
                1.0,
                {"container_name": container_name},
            )
            inspect_data = None
        if inspect_data is not None:
            raw_digests: object = inspect_data.get("RepoDigests")
            repo_digests: list[str] = (
                cast("list[str]", raw_digests) if isinstance(raw_digests, list) else []
            )
            if repo_digests:
                # I1: Filter by matching registry/repo to avoid picking the wrong digest
                # when a container has multiple repo digests (e.g. after re-tagging).
                target_prefix = f"{parsed.registry}/{parsed.repo}@"
                # docker.io images are stored as 'docker.io/library/X@...' or 'X@...'
                matched = next(
                    (d for d in repo_digests if str(d).startswith(target_prefix)),
                    None,
                )
                if matched is None and repo_digests:
                    # Fall back to first entry if no prefix match (non-standard registries)
                    matched = repo_digests[0]
                if (
                    matched is not None
                ):  # pragma: no branch -- None case is fallback when repo_digests empty
                    first = str(matched)
                    local_digest = first.split("@", 1)[1] if "@" in first else first

        # 4. Fetch registry digest.
        assert self._registry_client is not None
        result = await self._registry_client.fetch_latest_digest(image_ref)

        # 5. Branch on outcome.
        if isinstance(result, FetchedDigest):
            # Update rate-limit tracking with timestamp.
            if (
                result.rate_limit_remaining is not None
            ):  # pragma: no branch -- None case = no header (non-Docker-Hub registries)
                self._last_rate_limit_remaining[registry] = (
                    result.rate_limit_remaining,
                    time.monotonic(),
                )
            # If None (no header), preserve existing entry (or leave absent)
            update_available = local_digest is not None and result.digest != local_digest
            ctx.vm.write_gauge(
                "homelab_image_update_available",
                1.0 if update_available else 0.0,
                {
                    "name": container_name,
                    "image": image_ref,
                    "current_digest": local_digest or "",
                    "latest_digest": result.digest,
                },
            )
            payload: _UpsertPayload = {
                "container_name": container_name,
                "last_image_ref": image_ref,
                "last_local_digest": local_digest,
                "last_registry_digest": result.digest,
                "last_checked_at": now,
                "check_failed_at": None,
                "check_error_reason": None,
                "update_available": update_available,
            }
            return (1, payload)

        # FetchError branch
        # Emit gauge with 0 (don't claim update on failure).
        ctx.vm.write_gauge(
            "homelab_image_update_available",
            0.0,
            {
                "name": container_name,
                "image": image_ref,
                "current_digest": local_digest or "",
                "latest_digest": "",
            },
        )
        payload = {
            "container_name": container_name,
            "last_image_ref": image_ref,
            "last_local_digest": local_digest,
            "last_registry_digest": None,
            "last_checked_at": now,
            "check_failed_at": now,
            "check_error_reason": result.reason,
            "update_available": False,
        }
        ctx.log.info(
            "image_update_collector.fetch_error",
            container_name=container_name,
            registry=registry,
            reason=result.reason,
            message=result.message,
        )
        return (1, payload)

    @staticmethod
    def _emit_self_metric(ctx: CollectorContext, *, phase: str, result: str) -> None:
        ctx.vm.write_gauge(
            "homelab_collector_run_image_update_checker",
            1.0,
            {"phase": phase, "result": result},
        )


__all__ = ["ImageUpdateCollector"]
