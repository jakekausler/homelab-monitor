"""ComposeActionRunner — STAGE-003-010.

Runs `docker compose -f <file> pull <service> && docker compose -f <file>
up -d <service>` as a background task, captures stdout/stderr, writes a
compose_actions row + audit_log row, emits Prometheus metrics.

Design decisions (locked by STAGE-003-010 Design):
- Per-container asyncio.Lock; dict[str, asyncio.Lock] with setdefault.
- Background task via asyncio.create_task; tracked on self._active_tasks.
- 300s default timeout (env: HOMELAB_MONITOR_COMPOSE_ACTION_TIMEOUT_SECONDS).
- SIGTERM, 10s grace, then SIGKILL.
- stdout/stderr truncated to 1 MB on write (with "... [truncated]" marker).
- Compose file path resolved via BuildSourcesLoader (single source of truth).
- Subprocess uses arg-list (NOT shell=True) — compose service name is validated
  against the parsed compose file before exec.
- Per ADDENDUM Q2: Container's compose.service label is read via socket_client.inspect_container
  to resolve the service name.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

from prometheus_client import Counter

from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repositories.compose_actions_repository import (
    ComposeActionsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.compose_reader import (
    ComposeReadError,
    read_compose_set,
)
from homelab_monitor.kernel.docker.path_resolver import PathResolver
from homelab_monitor.kernel.docker.socket_client import DockerSocketError

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry
    from structlog.stdlib import BoundLogger

    from homelab_monitor.kernel.docker.build_sources_loader import BuildSourcesLoader
    from homelab_monitor.kernel.docker.socket_client import DockerSocketClient


_DEFAULT_TIMEOUT_SECONDS: Final[int] = 300
_SIGTERM_GRACE_SECONDS: Final[float] = 10.0
OUTPUT_MAX_CHARS: Final[int] = 1_000_000  # ~1 MB cap per stream (char count; ASCII is 1B/char)
TRUNCATION_MARKER: Final[str] = "\n... [truncated]"
_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 30.0


def resolve_timeout_seconds() -> float:
    raw = os.environ.get("HOMELAB_MONITOR_COMPOSE_ACTION_TIMEOUT_SECONDS")
    if not raw:
        return float(_DEFAULT_TIMEOUT_SECONDS)
    try:
        v = float(raw)
        return v if v > 0 else float(_DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        return float(_DEFAULT_TIMEOUT_SECONDS)


@dataclass(frozen=True, slots=True)
class ResolvedCompose:
    """A container's compose context, as resolved via BuildSourcesLoader + compose_reader."""

    compose_service: str
    compose_file_path: str
    compose_project: str  # "" means unknown; -p flag is omitted
    is_local_build: bool  # True when service has build_context (no registry pull needed)


@dataclass(frozen=True, slots=True)
class _SubprocessOutcome:
    """Internal result of a single subprocess run."""

    exit_code: int
    stdout: str
    stderr: str
    state: str  # 'success' | 'failed' | 'timeout' | 'killed'
    error_reason: str | None


class ImageUpdateRefresher(Protocol):
    """Callable that rechecks image-update state after pull success."""

    async def __call__(self, *, container_name: str, image_ref: str, image_id: str) -> None: ...


class LocalBuildRefresher(Protocol):
    """Callable that resets local-build baseline after rebuild success."""

    async def __call__(self, *, container_name: str) -> None: ...


class ComposeActionRunner:
    """Runner for Pull & Restart compose actions.

    Owns:
      - per-container asyncio.Lock dict
      - background task set
      - Prometheus counters (registered against the shared CollectorRegistry)
    """

    def __init__(  # noqa: PLR0913 -- keyword-only collaborators for dependency injection
        self,
        *,
        repo: SqliteRepository,
        actions_repo: ComposeActionsRepository,
        build_sources_loader: BuildSourcesLoader,
        socket_client: DockerSocketClient,
        prom_registry: CollectorRegistry,
        log: BoundLogger,
        timeout_seconds: float | None = None,
        image_update_refresher: ImageUpdateRefresher | None = None,
        local_build_refresher: LocalBuildRefresher | None = None,
    ) -> None:
        self._repo: SqliteRepository = repo
        self._actions_repo: ComposeActionsRepository = actions_repo
        self._build_sources_loader: BuildSourcesLoader = build_sources_loader
        self._socket_client: DockerSocketClient = socket_client
        self._log: BoundLogger = log
        self._timeout_seconds: float = (
            timeout_seconds if timeout_seconds is not None else resolve_timeout_seconds()
        )
        self._image_update_refresher: ImageUpdateRefresher | None = image_update_refresher
        self._local_build_refresher: LocalBuildRefresher | None = local_build_refresher
        # TODO: Lock dict grows unbounded across container lifetime; mirrors scheduler
        # `_group_locks` pattern. Accepted because container churn is low in a homelab
        # and lock entries are tiny. Revisit if memory becomes a concern.
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_tasks: set[asyncio.Task[None]] = set()
        # Register or look up Prometheus counters on the shared registry.
        self._success_total: Counter = get_or_create_counter(
            prom_registry,
            "homelab_compose_action_success_total",
            "Compose action successes",
            ["container", "action"],
        )
        self._failed_total: Counter = get_or_create_counter(
            prom_registry,
            "homelab_compose_action_failed_total",
            "Compose action failures",
            ["container", "action", "reason"],
        )

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def set_image_update_refresher(self, refresher: ImageUpdateRefresher) -> None:
        """Wire the post-pull image-update refresher.

        Called from lifespan after both objects exist.
        """
        self._image_update_refresher = refresher

    def set_local_build_refresher(self, refresher: LocalBuildRefresher) -> None:
        """Wire the post-rebuild local-build refresher."""
        self._local_build_refresher = refresher

    async def resolve_compose(  # noqa: PLR0911 -- step-by-step resolver with documented early returns
        self, container_name: str
    ) -> ResolvedCompose | None:
        """Resolve a container_name to (compose_service, compose_file_path).

        Uses the container's `com.docker.compose.service` label as the authoritative
        source of the compose service name (per STAGE-003-010 Design ADDENDUM Q2).
        This is robust to docker-compose project-name prefixes and v1/v2 naming.

        Returns None when:
          - The container cannot be inspected (DockerSocketError)
          - The container has no `com.docker.compose.service` label
          - BuildSourcesLoader has no current_config (no build-sources.yaml)
          - read_compose_set fails to parse the YAML
          - The labeled service does not appear in any loaded compose file
        Callers receive None and surface as 404 / state=failed,
        error_reason='container_not_resolvable' (or more specific).
        """
        # Step 1: Inspect the container to read its compose.service label.
        try:
            inspect = await self._socket_client.inspect_container(container_name)
        except DockerSocketError as exc:
            self._log.warning(
                "compose_action_runner.inspect_failed",
                container_name=container_name,
                error=str(exc),
            )
            return None

        # Step 2: Extract the compose.service label.
        # ContainerInspect is a TypedDict; .get("Config") returns dict[str, object] | None.
        # Labels live under Config but Docker's payload may shape them in different ways,
        # so we validate the runtime structure defensively.
        config_section = inspect.get("Config")
        labels_obj: object = (
            config_section.get("Labels") if isinstance(config_section, dict) else None
        )
        compose_service_obj: object = (
            cast("dict[str, object]", labels_obj).get("com.docker.compose.service")
            if isinstance(labels_obj, dict)
            else None
        )
        compose_service: str | None = (
            compose_service_obj if isinstance(compose_service_obj, str) else None
        )
        if compose_service is None or not compose_service:
            self._log.warning(
                "compose_action_runner.no_compose_service_label",
                container_name=container_name,
            )
            return None

        # Extract optional compose.project label (used to pass -p to subprocess).
        compose_project_obj: object = (
            cast("dict[str, object]", labels_obj).get("com.docker.compose.project")
            if isinstance(labels_obj, dict)
            else None
        )
        compose_project: str = compose_project_obj if isinstance(compose_project_obj, str) else ""

        # Step 3: Load compose files via BuildSourcesLoader.
        build_config = self._build_sources_loader.current_config
        if build_config is None:
            self._log.warning(
                "compose_action_runner.no_build_config",
                container_name=container_name,
            )
            return None

        compose_paths = [Path(e.container_path) for e in build_config.compose_files]
        resolver = PathResolver(build_config.build_context_roots)
        try:
            compose = read_compose_set(
                compose_paths,
                path_resolver=resolver,
                log=self._log,  # pyright: ignore[reportArgumentType]
            )
        except ComposeReadError as exc:
            self._log.warning(
                "compose_action_runner.compose_read_failed",
                container_name=container_name,
                compose_service=compose_service,
                reason=exc.reason,
                error=str(exc),
            )
            return None

        # Step 4: Match the labeled service to a parsed compose service.
        svc = compose.services.get(compose_service)
        if svc is None:
            self._log.warning(
                "compose_action_runner.service_not_in_compose",
                container_name=container_name,
                compose_service=compose_service,
            )
            return None

        compose_file = svc.source_compose_path
        if compose_file is None:  # pragma: no cover -- defensive; read_compose_set sets it
            return None

        return ResolvedCompose(
            compose_service=svc.name,
            compose_file_path=str(compose_file),
            compose_project=compose_project,
            is_local_build=svc.build_context is not None,
        )

    async def trigger_pull_and_restart(
        self,
        *,
        container_name: str,
        who: str,
        client_ip: str | None,
    ) -> int:
        """Insert a running row, spawn the background task, return action_id.

        The compose path is resolved INSIDE the background task (under the
        per-container lock) so concurrent triggers are serialized correctly.
        If resolution fails inside the task, the row terminates with
        state="failed", error_reason set to one of:
          - container_not_managed_by_compose
          - compose_service_not_in_file
          - container_not_resolvable
        """
        # Pre-resolve once for the command preview in the row.
        resolved = await self.resolve_compose(container_name)
        if resolved is None:
            # Insert a failed row immediately so the API can return its id.
            now = utc_now_iso()
            audit_id = uuid7()
            action_id = await self._actions_repo.insert_running(
                action="pull_and_restart",
                container_name=container_name,
                compose_service="(unresolved)",
                command="(unresolved)",
                started_at=now,
                who=who,
                client_ip=client_ip,
            )
            # Determine the error reason by inspecting again.
            error_reason = "container_not_resolvable"
            try:
                inspect_result = await self._socket_client.inspect_container(container_name)
                config_dict = inspect_result.get("Config")
                if isinstance(config_dict, dict):
                    labels = config_dict.get("Labels", {})
                    if isinstance(labels, dict):
                        if "com.docker.compose.service" not in labels:
                            error_reason = "container_not_managed_by_compose"
                        else:
                            error_reason = "compose_service_not_in_file"
            except DockerSocketError:
                pass

            await self._actions_repo.update_terminal_state(
                action_id=action_id,
                state="failed",
                stdout=None,
                stderr=None,
                exit_code=None,
                ended_at=now,
                duration_seconds=0.0,
                error_reason=error_reason,
                audit_log_id=audit_id,
            )
            self._failed_total.labels(
                container=container_name,
                action="pull_and_restart",
                reason=error_reason,
            ).inc()
            await self._write_audit_row(
                action_id=action_id,
                container_name=container_name,
                compose_service="(unresolved)",
                who=who,
                client_ip=client_ip,
                state="failed",
                error_reason=error_reason,
                audit_id=audit_id,
            )
            return action_id

        command = self._build_command(
            compose_file_path=resolved.compose_file_path,
            compose_service=resolved.compose_service,
            compose_project=resolved.compose_project,
            is_local_build=resolved.is_local_build,
        )
        now = utc_now_iso()
        action_id = await self._actions_repo.insert_running(
            action="pull_and_restart",
            container_name=container_name,
            compose_service=resolved.compose_service,
            command=command,
            started_at=now,
            who=who,
            client_ip=client_ip,
            initial_state="building" if resolved.is_local_build else "pulling",
        )
        task = asyncio.create_task(
            self._run_pull_and_restart_locked(
                action_id=action_id,
                container_name=container_name,
                resolved=resolved,
                command=command,
                who=who,
                client_ip=client_ip,
            ),
            name=f"compose_action.{container_name}.{action_id}",
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return action_id

    async def shutdown(self) -> None:
        """Cancel + gather all active background tasks with a 30s timeout."""
        if not self._active_tasks:
            return
        tasks = list(self._active_tasks)
        for t in tasks:
            t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except TimeoutError:  # pragma: no cover -- shutdown best-effort
            self._log.warning(
                "compose_action_runner.shutdown_timeout",
                active_count=len(self._active_tasks),
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _build_command(
        *,
        compose_file_path: str,
        compose_service: str,
        compose_project: str = "",
        is_local_build: bool = False,
    ) -> str:
        """Human-readable command string for the row's `command` column.

        The actual subprocess uses argv lists (no shell). This string is for
        the audit row + UI display only.
        """
        p_flag = f"-p {compose_project} " if compose_project else ""
        first_cmd_verb = "build" if is_local_build else "pull"
        first_cmd = (
            f"docker compose {p_flag}-f {compose_file_path} {first_cmd_verb} {compose_service}"
        )
        up_cmd = (
            f"docker compose {p_flag}-f {compose_file_path} "
            f"up --force-recreate -d {compose_service}"
        )
        return f"{first_cmd} && {up_cmd}"

    async def _run_pull_and_restart_locked(  # noqa: PLR0913 -- keyword-only collaborators for DI
        self,
        *,
        action_id: int,
        container_name: str,
        resolved: ResolvedCompose,
        command: str,
        who: str,
        client_ip: str | None,
    ) -> None:
        """Acquire the per-container lock, run pull+up, persist terminal state."""
        del command  # already persisted; arg kept for future log-line use
        lock = self._locks.setdefault(container_name, asyncio.Lock())
        async with lock:
            start = time.monotonic()
            outcome = await self._run_first_then_up(
                action_id=action_id,
                compose_file_path=resolved.compose_file_path,
                compose_service=resolved.compose_service,
                compose_project=resolved.compose_project,
                is_local_build=resolved.is_local_build,
            )
            duration = time.monotonic() - start
            ended_at = utc_now_iso()
            audit_id = uuid7()
            await self._actions_repo.update_terminal_state(
                action_id=action_id,
                state=outcome.state,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                exit_code=outcome.exit_code,
                ended_at=ended_at,
                duration_seconds=duration,
                error_reason=outcome.error_reason,
                audit_log_id=audit_id,
            )
            await self._write_audit_row(
                action_id=action_id,
                container_name=container_name,
                compose_service=resolved.compose_service,
                who=who,
                client_ip=client_ip,
                state=outcome.state,
                error_reason=outcome.error_reason,
                audit_id=audit_id,
            )
            if outcome.state == "success":
                self._success_total.labels(
                    container=container_name,
                    action="pull_and_restart",
                ).inc()
                if resolved.is_local_build:
                    if self._local_build_refresher is not None:
                        await self._trigger_local_build_recheck(container_name=container_name)
                elif self._image_update_refresher is not None:
                    await self._trigger_image_recheck(container_name=container_name)
            else:
                self._failed_total.labels(
                    container=container_name,
                    action="pull_and_restart",
                    reason=outcome.error_reason or "unknown",
                ).inc()

    async def _run_first_then_up(
        self,
        *,
        action_id: int,
        compose_file_path: str,
        compose_service: str,
        compose_project: str = "",
        is_local_build: bool = False,
    ) -> _SubprocessOutcome:
        """Run `docker compose build` (local) or `pull` (remote) then `up -d`."""
        p_args = ["-p", compose_project] if compose_project else []
        first_verb = "build" if is_local_build else "pull"

        first_args = [
            "docker",
            "compose",
            *p_args,
            "-f",
            compose_file_path,
            first_verb,
            compose_service,
        ]
        first = await self._run_one(args=first_args)
        if first.state != "success":
            return first
        # Transition row to 'restarting' phase before invoking up -d.
        await self._actions_repo.update_phase(action_id=action_id, phase="restarting")
        up = await self._run_one(
            args=[
                "docker",
                "compose",
                *p_args,
                "-f",
                compose_file_path,
                "up",
                "--force-recreate",
                "-d",
                compose_service,
            ],
        )
        combined_stdout = truncate(first.stdout + up.stdout)
        combined_stderr = truncate(first.stderr + up.stderr)
        return _SubprocessOutcome(
            exit_code=up.exit_code,
            stdout=combined_stdout,
            stderr=combined_stderr,
            state=up.state,
            error_reason=up.error_reason,
        )

    async def _run_one(self, *, args: list[str]) -> _SubprocessOutcome:
        """Run a single subprocess with the configured timeout + SIGTERM/SIGKILL semantics."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            # `docker` not installed in the container.
            return _SubprocessOutcome(
                exit_code=-1,
                stdout="",
                stderr=f"docker CLI not found: {exc}",
                state="failed",
                error_reason="docker_cli_missing",
            )
        except OSError as exc:  # pragma: no cover -- defensive
            return _SubprocessOutcome(
                exit_code=-1,
                stdout="",
                stderr=f"subprocess spawn failed: {exc}",
                state="failed",
                error_reason="spawn_failed",
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            # SIGTERM with grace period, then SIGKILL — signals subprocess directly via
            # asyncio's wrapper (no process-group semantics; MagicMock.pid=1 in tests
            # would otherwise signal the user-systemd manager and kill the session).
            with contextlib.suppress(ProcessLookupError):  # pragma: no cover -- raced with exit
                proc.terminate()
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_SIGTERM_GRACE_SECONDS,
                )
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):  # pragma: no cover -- raced with exit
                    proc.kill()
                try:
                    stdout_b, stderr_b = await proc.communicate()
                except Exception:  # pragma: no cover -- defensive after kill
                    stdout_b, stderr_b = b"", b""
            return _SubprocessOutcome(
                exit_code=-1,
                stdout=truncate(stdout_b.decode("utf-8", errors="replace")),
                stderr=truncate(stderr_b.decode("utf-8", errors="replace")),
                state="timeout",
                error_reason="timeout",
            )

        stdout_s = truncate(stdout_b.decode("utf-8", errors="replace"))
        stderr_s = truncate(stderr_b.decode("utf-8", errors="replace"))
        rc = proc.returncode if proc.returncode is not None else -1
        if rc == 0:
            return _SubprocessOutcome(
                exit_code=rc,
                stdout=stdout_s,
                stderr=stderr_s,
                state="success",
                error_reason=None,
            )
        return _SubprocessOutcome(
            exit_code=rc,
            stdout=stdout_s,
            stderr=stderr_s,
            state="failed",
            error_reason="exit_nonzero",
        )

    async def _trigger_image_recheck(self, *, container_name: str) -> None:
        """Re-inspect the container post-pull and invoke the image-update refresher."""
        if self._image_update_refresher is None:
            return  # pragma: no cover
        try:
            inspect = await self._socket_client.inspect_container(container_name)
        except DockerSocketError as exc:
            self._log.warning(
                "compose_action_runner.recheck_inspect_failed",
                container_name=container_name,
                error=str(exc),
            )
            return
        # Extract image ref and image id from inspect.
        config = inspect.get("Config")
        image_ref: str = ""
        if isinstance(config, dict):
            img = config.get("Image")
            if isinstance(img, str):
                image_ref = img
        image_id: str = str(inspect.get("Image") or "")
        try:
            await self._image_update_refresher(
                container_name=container_name,
                image_ref=image_ref,
                image_id=image_id,
            )
        except Exception as exc:  # pragma: no cover -- defensive
            self._log.warning(
                "compose_action_runner.recheck_refresher_failed",
                container_name=container_name,
                error=str(exc),
            )

    async def _trigger_local_build_recheck(self, *, container_name: str) -> None:
        """Invoke the local-build refresher post-rebuild."""
        if self._local_build_refresher is None:
            return  # pragma: no cover
        try:
            await self._local_build_refresher(container_name=container_name)
        except Exception as exc:  # pragma: no cover -- defensive
            self._log.warning(
                "compose_action_runner.local_build_recheck_failed",
                container_name=container_name,
                error=str(exc),
            )

    async def _write_audit_row(  # noqa: PLR0913 -- keyword-only collaborators for DI
        self,
        *,
        action_id: int,
        container_name: str,
        compose_service: str,
        who: str,
        client_ip: str | None,
        state: str,
        error_reason: str | None,
        audit_id: str | None = None,
    ) -> None:
        """Insert an audit_log row for this action attempt."""
        before = {
            "action_id": action_id,
            "action": "pull_and_restart",
            "container_name": container_name,
            "compose_service": compose_service,
        }
        after = {
            "action_id": action_id,
            "state": state,
            "error_reason": error_reason,
        }
        async with self._repo.transaction() as conn:
            await insert_audit(
                conn,
                who=who,
                what="docker.compose.pull_and_restart",
                before=before,
                after=after,
                ip=client_ip,
                when=utc_now_iso(),
                audit_id=audit_id,
            )


def truncate(s: str) -> str:
    """Truncate to <= 1 MB with a marker. Returns unmodified if under cap."""
    if len(s) <= OUTPUT_MAX_CHARS:
        return s
    return s[:OUTPUT_MAX_CHARS] + TRUNCATION_MARKER


def get_or_create_counter(
    registry: CollectorRegistry,
    name: str,
    documentation: str,
    labelnames: list[str],
) -> Counter:
    """Return an existing Counter on `registry` or register a new one.

    Tests reuse the same registry across multiple ComposeActionRunner instances
    (lifespan creates one per app start); prometheus_client raises if you
    register twice. This helper handles the dedup.
    """
    existing = getattr(registry, "_names_to_collectors", {}).get(name)
    if existing is not None:
        return existing  # pyright: ignore[reportReturnType]
    return Counter(name, documentation, labelnames, registry=registry)


__all__ = [
    "ComposeActionRunner",
    "LocalBuildRefresher",
    "ResolvedCompose",
]
