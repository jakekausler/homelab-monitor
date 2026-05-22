"""STAGE-003-005: DockerDiscoverer plugin.

Two concurrent code paths share a single `_upsert_suggestion()` writer
behind an asyncio.Lock:

  - events_task: long-lived stream from /events?filters=type=container.
    Reacts to container.create (upsert) and container.destroy
    (mark_container_gone). Exponential backoff on disconnect (1s → 60s).

  - periodic run() (BaseCollector tick): every N seconds (default 300),
    enumerate ALL containers, upsert anyone that matches a detection rule
    (no homelab-monitor labels, disabled profile, label collision). Fills
    any gap left by missed event-stream notifications.

Detection rules (see `_extract_detection_reason`):
  - `no_homelab_monitor_label` — container has NO `homelab-monitor.*` labels
  - `disabled_profile` — `com.docker.compose.config.profiles` contains 'disabled'
  - `label_collision` — two labels resolve to the same (kind, name) on same
    container; emits `kind="docker_label_collision"` instead of the default
    `kind="docker_container_discovered"`.

Self-metrics:
  homelab_collector_run_docker_discoverer{phase="events|periodic",
                                         result="ok|error"} (gauge, updated via write_gauge)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from datetime import timedelta
from typing import Any, ClassVar, Final

from homelab_monitor.kernel.db.repositories.suggestions_repository import (
    SuggestionsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.socket_client import (
    ContainerInspect,
    DockerSocketClient,
    DockerSocketError,
)
from homelab_monitor.kernel.metrics.docker_socket_collector import (
    derive_docker_logical_key,
    encode_logical_key,
)
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

_BACKOFF_INITIAL_SECONDS: Final[float] = 1.0
_BACKOFF_MAX_SECONDS: Final[float] = 60.0
_DEFAULT_SCAN_INTERVAL: Final[int] = 300

_HOMELAB_LABEL_PREFIX: Final[str] = "homelab-monitor."
_COMPOSE_PROFILE_LABEL: Final[str] = "com.docker.compose.config.profiles"
_COMPOSE_PROJECT_LABEL: Final[str] = "com.docker.compose.project"
_COMPOSE_SERVICE_LABEL: Final[str] = "com.docker.compose.service"
_COMPOSE_CONFIG_FILES_LABEL: Final[str] = "com.docker.compose.project.config_files"
_DISABLED_PROFILE_TOKEN: Final[str] = "disabled"

_KIND_DISCOVERED: Final[str] = "docker_container_discovered"
_KIND_COLLISION: Final[str] = "docker_label_collision"


def _resolve_scan_interval() -> int:
    raw = os.environ.get(
        "HOMELAB_MONITOR_DOCKER_DISCOVERER_SCAN_INTERVAL_SECONDS",
        str(_DEFAULT_SCAN_INTERVAL),
    )
    try:
        v = int(raw)
        return max(1, v)
    except ValueError:
        return _DEFAULT_SCAN_INTERVAL


class DockerDiscoverer(BaseCollector):
    """Docker discoverer — events stream + periodic re-scan."""

    name: ClassVar[str] = "docker_discoverer"
    interval: ClassVar[timedelta] = timedelta(seconds=_resolve_scan_interval())
    timeout: ClassVar[timedelta] = timedelta(seconds=60)
    concurrency_group: ClassVar[str] = "docker.discoverer"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(
        self,
        *,
        socket_client: DockerSocketClient | None = None,
        suggestions_repo: SuggestionsRepository | None = None,
        db: SqliteRepository | None = None,
        scan_interval_seconds: int | None = None,
    ) -> None:
        self._socket_client: DockerSocketClient | None = socket_client
        self._suggestions_repo: SuggestionsRepository | None = suggestions_repo
        self._db: SqliteRepository | None = db
        self._scan_interval_seconds: int = scan_interval_seconds or _resolve_scan_interval()
        self._lock: asyncio.Lock = asyncio.Lock()
        self._events_task: asyncio.Task[None] | None = None
        # Reference to ctx (set on first run) so the events loop can emit
        # self-metrics via ctx.vm.write_gauge (or write_counter if available).
        self._ctx: CollectorContext | None = None

    # ---- Lifecycle ----

    def start_events_loop(self, ctx: CollectorContext) -> None:
        """Launch the long-lived events task. Called by lifespan.py AFTER
        ctx_factory is wired. Safe to call multiple times — idempotent.
        """
        if self._events_task is not None and not self._events_task.done():
            return
        self._ctx = ctx
        self._events_task = asyncio.create_task(
            self.run_events_loop(ctx),
            name="docker_discoverer.events",
        )

    async def stop_events_loop(self) -> None:
        """Cancel + await the events task. Called from lifespan shutdown."""
        if self._events_task is None:
            return
        self._events_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._events_task
        self._events_task = None

    # ---- BaseCollector tick (periodic) ----

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Periodic full-list scan. Emits self-metric phase='periodic'."""
        start = time.monotonic()
        self._ctx = ctx
        if self._socket_client is None or self._suggestions_repo is None or self._db is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["dependencies_unwired"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )
        # Lazily start the events loop on the first scheduled tick. lifespan
        # also calls start_events_loop(), so this is a defensive belt-and-
        # braces; the idempotency guard makes double-calls cheap.
        if self._events_task is None or self._events_task.done():
            self.start_events_loop(ctx)

        try:
            entries = await self._socket_client.list_containers()
        except DockerSocketError as exc:
            ctx.log.warning("docker_discoverer.periodic.list_failed", error=str(exc))
            self._emit_metric(ctx, phase="periodic", result="error")
            return CollectorResult(
                ok=False,
                metrics_emitted=1,
                errors=[f"list_failed: {exc}"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        errors: list[str] = []
        for entry in entries:
            try:
                inspect = await self._socket_client.inspect_container(entry["Id"])
            except DockerSocketError as exc:
                ctx.log.warning(
                    "docker_discoverer.periodic.inspect_failed",
                    id=entry["Id"],
                    error=str(exc),
                )
                errors.append(f"inspect_failed({entry['Id'][:12]}): {exc}")
                continue
            await self._upsert_suggestion(ctx, inspect)

        self._emit_metric(ctx, phase="periodic", result="ok" if not errors else "error")
        return CollectorResult(
            ok=not errors,
            metrics_emitted=1,
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    # ---- Events loop ----

    async def run_events_loop(self, ctx: CollectorContext) -> None:
        """Connect, consume events forever. On disconnect, reconnect with
        exponential backoff capped at 60s. Cancellation exits cleanly.
        """
        backoff = _BACKOFF_INITIAL_SECONDS
        if self._socket_client is None:
            return
        while True:
            try:
                async for event in self._socket_client.events(filters={"type": ["container"]}):
                    backoff = _BACKOFF_INITIAL_SECONDS  # reset on each successful event
                    try:
                        await self._handle_event(ctx, event)
                        self._emit_metric(ctx, phase="events", result="ok")
                    except Exception as exc:  # pragma: no cover -- defensive
                        ctx.log.warning(
                            "docker_discoverer.events.handle_failed",
                            error=str(exc),
                        )
                        self._emit_metric(ctx, phase="events", result="error")
            except asyncio.CancelledError:
                raise
            except DockerSocketError as exc:
                ctx.log.warning(
                    "docker_discoverer.events.stream_failed",
                    error=str(exc),
                    next_backoff_seconds=backoff,
                )
                self._emit_metric(ctx, phase="events", result="error")
            except Exception as exc:
                ctx.log.warning(
                    "docker_discoverer.events.unexpected_error",
                    error=str(exc),
                    next_backoff_seconds=backoff,
                )
                self._emit_metric(ctx, phase="events", result="error")
            # TODO(STAGE-003-006+): track consecutive_failures counter; emit error log
            # after >= 5 consecutive failures (currently retries silently forever with
            # warning-level logs). See code review I1.
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)

    async def _handle_event(self, ctx: CollectorContext, event: dict[str, Any]) -> None:
        """Dispatch on Docker event type."""
        action = event.get("Action")
        actor: dict[str, Any] = event.get("Actor") or {}
        attributes: dict[str, Any] = actor.get("Attributes") or {}
        container_id: str | None = actor.get("ID") or attributes.get("id")
        if not container_id:
            return

        if action == "create" and self._socket_client is not None:
            try:
                inspect = await self._socket_client.inspect_container(container_id)
            except DockerSocketError as exc:
                ctx.log.warning(
                    "docker_discoverer.events.inspect_failed",
                    id=container_id,
                    error=str(exc),
                )
                return
            await self._upsert_suggestion(ctx, inspect)
            return

        if action == "destroy" and self._suggestions_repo is not None and self._db is not None:
            async with self._lock, self._db.transaction() as conn:
                await SuggestionsRepository.mark_container_gone_conn(
                    conn,
                    container_id=container_id,
                    now=utc_now_iso(),
                )
            return
        # Any other action (start, stop, die, etc.) is ignored for this stage.

    # ---- Shared writer (lock-protected) ----

    async def _upsert_suggestion(
        self,
        ctx: CollectorContext,
        inspect: ContainerInspect,
    ) -> None:
        """Compute detection rule + upsert anchor+sidecar.

        Both code paths funnel through here; the lock serialises concurrent
        writes for the same container so the unique constraint on
        (kind, deduplication_key) cannot race.
        """
        if self._suggestions_repo is None or self._db is None:
            return

        container_id = inspect["Id"]
        # Strip leading "/" Docker prepends to inspect Name.
        raw_name = str(inspect.get("Name") or "")
        container_name = raw_name[1:] if raw_name.startswith("/") else raw_name
        image_ref = str(inspect.get("Image") or "")
        cfg_obj: dict[str, Any] = inspect.get("Config") or {}
        labels: dict[str, Any] = cfg_obj.get("Labels") or {}
        # Normalise to dict[str, str] — values from the Docker API are always str.

        reason = _extract_detection_reason(labels)
        if reason is None:
            return  # container is healthy + labeled → no suggestion

        kind = _KIND_COLLISION if reason == "label_collision" else _KIND_DISCOVERED
        # STAGE-003-005 Refinement: dedup_key is now the encoded logical-key string,
        # not container_id. This ensures one suggestion per logical service per kind,
        # surviving docker compose up --force-recreate (which changes container_id).
        labels_str: dict[str, str] = {str(k): str(v) for k, v in labels.items()}
        logical_key_kind, logical_key = derive_docker_logical_key(
            labels=labels_str,
            name=container_name,
        )
        dedup_key = encode_logical_key(logical_key_kind, logical_key)
        compose_project: str | None = labels.get(_COMPOSE_PROJECT_LABEL)
        compose_service: str | None = labels.get(_COMPOSE_SERVICE_LABEL)
        compose_file_path: str | None = labels.get(_COMPOSE_CONFIG_FILES_LABEL)
        # Compose containers should set all three labels together. Surface any
        # observed gaps so STAGE-003-010's Pull & Restart action can rely on
        # `service` being present when `project` is.
        if compose_project is not None and compose_service is None:
            ctx.log.warning(
                "docker_discoverer.compose_service_missing",
                container_name=container_name,
                compose_project=compose_project,
                compose_file_path=compose_file_path,
            )
        if compose_project is not None and compose_file_path is None:
            ctx.log.warning(
                "docker_discoverer.compose_file_path_missing",
                container_name=container_name,
                compose_project=compose_project,
                compose_service=compose_service,
            )

        async with self._lock, self._db.transaction() as conn:
            await SuggestionsRepository.insert_or_update_docker_suggestion_conn(
                conn,
                kind=kind,
                deduplication_key=dedup_key,
                container_id=container_id,
                container_name=container_name,
                image_ref=image_ref,
                labels={str(k): str(v) for k, v in labels.items()},
                compose_project=compose_project,
                compose_service=compose_service,
                compose_file_path=compose_file_path,
                detection_reason=reason,
                now=utc_now_iso(),
            )

    # ---- Self-metrics ----

    @staticmethod
    def _emit_metric(ctx: CollectorContext, *, phase: str, result: str) -> None:
        """Record homelab_collector_run_docker_discoverer{phase, result} gauge."""
        ctx.vm.write_gauge(
            "homelab_collector_run_docker_discoverer",
            1.0,
            {"phase": phase, "result": result},
        )


def _extract_detection_reason(labels: dict[str, str]) -> str | None:
    """Return a reason code, or None when the container is healthy + labeled.

    Rules (first match wins):
      1. 'label_collision' — two homelab-monitor.* labels collide on
         (kind, name). E.g. both `homelab-monitor.http.foo` and
         `homelab-monitor.http.foo` from different sources. Detected by
         hashing each key's normalized (kind, name) tuple and looking for
         duplicates.
      2. 'disabled_profile' — `com.docker.compose.config.profiles` contains
         the token 'disabled'.
      3. 'no_homelab_monitor_label' — no labels start with the
         `homelab-monitor.` prefix.

    Returns None when the container has at least one homelab-monitor label
    AND is not in the disabled profile AND no collision detected. Those
    cases are handed off to STAGE-003-006's label-config path.
    """
    homelab_labels = {k: v for k, v in labels.items() if k.startswith(_HOMELAB_LABEL_PREFIX)}

    # Rule 1: collision among homelab-monitor.* labels.
    # Key shape: `homelab-monitor.<kind>.<name>[.<sub>]`. We extract
    # (kind, name) — the first two dotted segments after the prefix.
    seen_tuples: dict[tuple[str, str], str] = {}
    for key in homelab_labels:
        suffix = key[len(_HOMELAB_LABEL_PREFIX) :]
        parts = suffix.split(".", 2)
        if len(parts) < 2:  # noqa: PLR2004
            continue
        identity = (parts[0], parts[1])
        if identity in seen_tuples and seen_tuples[identity] != key:
            return "label_collision"
        seen_tuples[identity] = key

    # Rule 2: disabled profile.
    profile_raw = labels.get(_COMPOSE_PROFILE_LABEL, "")
    if profile_raw:
        profiles = [p.strip() for p in profile_raw.split(",")]
        if _DISABLED_PROFILE_TOKEN in profiles:
            return "disabled_profile"

    # Rule 3: no homelab-monitor labels.
    if not homelab_labels:
        return "no_homelab_monitor_label"

    return None


__all__ = ["DockerDiscoverer"]
