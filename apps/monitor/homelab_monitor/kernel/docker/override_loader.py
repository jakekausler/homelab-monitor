"""OverrideLoader — periodic file-override scanner.

D-HOTRELOAD-PERIODIC-30S: standalone asyncio task running every 30s.
NOT a BaseCollector — does not register through PluginLoader. Mirrors
TtlCachingSecretsResolver.refresh_loop() pattern (asyncio.sleep + try/except
+ cancellable). Launched and shut down from lifespan.

D-OWNERSHIP-TOTAL-PER-CONTAINER + D-OWNERSHIP-COORDINATION-VIA-SQLITE:
each tick replaces the full owned-set in the docker_override_ownership
table; DockerDiscoverer reads this set to skip the label-upsert path for
owned containers.

D-EXEC-DUAL-GATE-FILE-OVERRIDE: kind=exec probes require BOTH the global
env (HOMELAB_MONITOR_DOCKER_PROBES_EXEC_ENABLED=true) AND per-file
exec_authorized=true; otherwise dropped + suggestion emitted.

D-ERROR-UX-BADGE-PLUS-SUGGESTION: validation errors are kept in the
loader's in-memory `_errors_by_container` map (for the row-badge surface
via /probes/summary) AND, when the file is orphaned (no matching live
container), also emitted as a docker_file_override_malformed suggestion.

Per-tick structlog event (`override_loader.refresh_complete`) provides
liveness signal; dedicated collector metric deferred to STAGE-003-012.
TODO(STAGE-003-012): emit homelab_collector_run_override_loader{phase,result} via shared
# metrics writer
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import yaml
from pydantic import ValidationError

from homelab_monitor.kernel.db.repositories.override_ownership_repository import (
    OverrideOwnershipRepository,
)
from homelab_monitor.kernel.db.repositories.probe_targets_repository import (
    ProbeTargetsRepository,
)
from homelab_monitor.kernel.db.repositories.suggestions_repository import (
    SuggestionsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.override_schema import (
    DockerContainerOverride,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection
    from structlog.stdlib import BoundLogger

    from homelab_monitor.kernel.docker.socket_client import DockerSocketClient

_REFRESH_INTERVAL_SECONDS: Final[float] = 30.0
_SUGGESTION_KIND_MALFORMED: Final[str] = "docker_file_override_malformed"
_OVERRIDE_CONFIG_SOURCE: Final[str] = "file_override"


@dataclass(frozen=True, slots=True)
class _ParsedOverride:
    container_name: str  # file stem (canonical name)
    override: DockerContainerOverride
    source_path: str


class OverrideLoader:
    """Periodic scanner for /config/plugins/docker/*.yaml files."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        db: SqliteRepository,
        suggestions_repo: SuggestionsRepository,
        probe_targets_repo: ProbeTargetsRepository,
        ownership_repo: OverrideOwnershipRepository,
        overrides_dir: Path,
        exec_enabled_globally: bool,
        log: BoundLogger,
        socket_client: DockerSocketClient | None = None,
        refresh_interval_seconds: float = _REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self._db: SqliteRepository = db
        self._suggestions_repo: SuggestionsRepository = suggestions_repo
        self._probe_targets_repo: ProbeTargetsRepository = probe_targets_repo
        self._ownership_repo: OverrideOwnershipRepository = ownership_repo
        self._overrides_dir: Path = overrides_dir
        self._exec_enabled_globally: bool = exec_enabled_globally
        self._log: BoundLogger = log
        self._socket_client: DockerSocketClient | None = socket_client
        self._refresh_interval_seconds: float = refresh_interval_seconds
        self._task: asyncio.Task[None] | None = None
        # Map container_name -> list of error messages, populated each tick.
        # Read by the /probes/summary API layer for the row badge.
        self._errors_by_container: dict[str, list[str]] = {}

    # ---- Public read for API ----

    def current_errors_by_container(self) -> Mapping[str, tuple[str, ...]]:
        """Snapshot of current per-container validation errors.

        Read-only mapping — callers must NOT attempt to mutate the result. Returns
        a frozen view of the loader's internal state at call time.
        """
        return MappingProxyType({k: tuple(v) for k, v in self._errors_by_container.items()})

    # ---- Lifecycle ----

    def start_task(self) -> None:
        """Launch the periodic refresh task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.refresh_loop(), name="override_loader.refresh")

    async def stop_task(self) -> None:
        """Cancel + await the refresh task."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def refresh_loop(self) -> None:
        """Run forever, refreshing every `refresh_interval_seconds`.

        Re-raises asyncio.CancelledError so callers awaiting the task
        observe cancellation. Per-tick exceptions are caught + logged;
        the loop keeps running.
        """
        while True:
            try:
                await self.refresh_once()
            except Exception:  # pragma: no cover -- defensive
                self._log.exception("override_loader.refresh_failed")
            # If refresh_once raised, we still sleep before retrying. With 30s default
            # this means up to 30s delay before recovery — acceptable for v1.
            await asyncio.sleep(self._refresh_interval_seconds)

    # ---- One tick ----

    async def refresh_once(self) -> None:
        """Scan dir, parse all files, atomically apply ownership + probe upserts."""
        start = time.monotonic()
        if not self._overrides_dir.is_dir():
            # Empty/missing dir = no-op. Clear any previous owned set so the
            # discoverer reclaims those containers next tick.
            self._errors_by_container = {}
            await self._release_all_ownership()
            self._log.info(
                "override_loader.refresh_complete",
                owned=0,
                errors=0,
                reason="dir_not_present",
                duration_seconds=round(time.monotonic() - start, 4),
            )
            return

        parsed: list[_ParsedOverride] = []
        new_errors: dict[str, list[str]] = {}
        for file_path in self._iter_override_files():
            container_name = file_path.stem
            try:
                override = DockerContainerOverride.load_from_path(file_path)
            except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
                new_errors.setdefault(container_name, []).append(f"{file_path.name}: {exc!s}")
                continue
            parsed.append(
                _ParsedOverride(
                    container_name=container_name,
                    override=override,
                    source_path=str(file_path),
                )
            )

        # Compute owned-set BEFORE filtering for disabled — files that parse
        # OK take ownership even when disabled: true (so labels are NOT
        # re-applied on top of an operator's deliberate suppression).
        owned_set: set[str] = {p.container_name for p in parsed}

        # Apply: per file with valid parse, upsert probes (or wipe if disabled)
        # + mark_missing_except. Ownership table updated atomically at end.
        # Compute live names BEFORE the transaction (avoids holding DB lock across socket I/O)
        live_names = await self._safe_list_live_container_names()
        now = utc_now_iso()
        suggestions_emitted = 0
        emitted_dedup_keys: set[str] = set()
        async with self._db.transaction() as conn:
            for parsed_override in parsed:
                emitted = await self._apply_parsed_override(
                    conn,
                    parsed=parsed_override,
                    now=now,
                    new_errors=new_errors,
                    emitted_dedup_keys=emitted_dedup_keys,
                )
                suggestions_emitted += emitted
                # Track dedup keys emitted for this parsed override
                if not parsed_override.override.disabled and parsed_override.override.probes:
                    emitted_dedup_keys.add(f"malformed::{parsed_override.container_name}")

            # Orphan errors: container_name in new_errors but not currently
            # running (best effort — if socket unavailable, treat as orphan
            # to emit the suggestion since we cannot prove liveness).
            for cn, errs in new_errors.items():
                if cn in live_names:
                    continue
                # Orphan or unknown liveness — emit suggestion.
                for err in errs:
                    await SuggestionsRepository.insert_or_update_docker_suggestion_conn(
                        conn,
                        kind=_SUGGESTION_KIND_MALFORMED,
                        deduplication_key=f"malformed::{cn}",
                        container_id="",
                        container_name=cn,
                        image_ref="",
                        labels={},
                        compose_project=None,
                        compose_service=None,
                        compose_file_path=None,
                        detection_reason=err,
                        now=now,
                    )
                    suggestions_emitted += 1
                    emitted_dedup_keys.add(f"malformed::{cn}")

            # Release ownership for containers previously owned but not in the
            # new set (file was deleted/renamed). mark_missing_except_conn(set())
            # soft-deletes all file_override rows for that container.
            previous_owned = await OverrideOwnershipRepository.list_owned_conn(conn)
            released = previous_owned - owned_set
            for cn in released:
                await ProbeTargetsRepository.mark_missing_except_conn(
                    conn,
                    container_name=cn,
                    kept_keys=set(),
                    now=now,
                )

            # Clear stale malformed-file suggestions when the operator fixes
            # the YAML and the loader stops re-emitting.
            # When emitted_dedup_keys is empty (no overrides or all disabled),
            # mark_resolved_conn marks ALL pending docker_file_override_malformed as
            # container_gone — desired behavior on a fully cleared overrides dir.
            await SuggestionsRepository.mark_resolved_conn(
                conn,
                kind=_SUGGESTION_KIND_MALFORMED,
                kept_dedup_keys=emitted_dedup_keys,
                now=now,
            )

            await OverrideOwnershipRepository.set_owned_conn(
                conn,
                container_names=owned_set,
                now=now,
            )

        self._errors_by_container = new_errors
        self._log.info(
            "override_loader.refresh_complete",
            owned=len(owned_set),
            errors=sum(len(v) for v in new_errors.values()),
            suggestions_emitted=suggestions_emitted,
            duration_seconds=round(time.monotonic() - start, 4),
        )

    # ---- Helpers ----

    def _iter_override_files(self) -> list[Path]:
        """Return *.yaml/*.yml files in the top-level dir (NO subdirs)
        in deterministic alphabetical order (D-OVERRIDE-FILE-NAMING).
        """
        files: list[Path] = []
        for entry in sorted(self._overrides_dir.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in (".yaml", ".yml"):
                continue
            files.append(entry)
        return files

    async def _apply_parsed_override(
        self,
        conn: AsyncConnection,
        *,
        parsed: _ParsedOverride,
        now: str,
        new_errors: dict[str, list[str]],
        emitted_dedup_keys: set[str],
    ) -> int:
        """Apply one parsed override inside an already-open transaction.

        Returns the number of suggestions emitted for exec gating violations.
        """
        cn = parsed.container_name
        override = parsed.override
        suggestions_emitted = 0

        if override.disabled:
            # Wipe all probes for this container (loader takes ownership).
            await ProbeTargetsRepository.mark_missing_except_conn(
                conn,
                container_name=cn,
                kept_keys=set(),
                now=now,
            )
            return 0

        kept_keys: set[tuple[str, str]] = set()
        for probe in override.probes:
            if probe.kind == "exec" and not self._exec_authorized_for(override):
                # Dual-gate fail: drop probe + emit suggestion.
                reason = (
                    "exec_not_authorized: probe kind=exec requires both "
                    "HOMELAB_MONITOR_DOCKER_PROBES_EXEC_ENABLED=true and "
                    "exec_authorized: true in the override file"
                )
                new_errors.setdefault(cn, []).append(
                    f"{parsed.source_path}: probe {probe.kind}.{probe.name}: {reason}"
                )
                await SuggestionsRepository.insert_or_update_docker_suggestion_conn(
                    conn,
                    kind=_SUGGESTION_KIND_MALFORMED,
                    deduplication_key=f"malformed::{cn}::exec::{probe.name}",
                    container_id="",
                    container_name=cn,
                    image_ref="",
                    labels={},
                    compose_project=None,
                    compose_service=None,
                    compose_file_path=None,
                    detection_reason=reason,
                    now=now,
                )
                suggestions_emitted += 1
                emitted_dedup_keys.add(f"malformed::{cn}::exec::{probe.name}")
                continue
            await ProbeTargetsRepository.upsert_probe_target_conn(
                conn,
                container_name=cn,
                kind=probe.kind,
                name=probe.name,
                target_value=probe.target,
                config_source=_OVERRIDE_CONFIG_SOURCE,
                enabled=probe.enabled,
                interval_seconds=probe.interval_seconds,
                timeout_seconds=probe.timeout_seconds,
                exec_authorized=override.exec_authorized,
                now=now,
            )
            kept_keys.add((probe.kind, probe.name))

        # Soft-delete probes for this container that the file no longer mentions.
        await ProbeTargetsRepository.mark_missing_except_conn(
            conn,
            container_name=cn,
            kept_keys=kept_keys,
            now=now,
        )
        return suggestions_emitted

    def _exec_authorized_for(self, override: DockerContainerOverride) -> bool:
        return self._exec_enabled_globally and override.exec_authorized

    async def _safe_list_live_container_names(self) -> set[str]:
        """Best-effort liveness query for orphan-detection.

        Returns the set of canonical container names the Docker socket
        reports. On any socket failure, returns set() — orphan suggestions
        will fire for ALL error-emitting files, which is acceptable
        because the loader cannot prove liveness.
        """
        if self._socket_client is None:
            return set()
        try:
            entries = await self._socket_client.list_containers()
        except Exception:  # pragma: no cover -- defensive
            return set()
        names: set[str] = set()
        for entry in entries:
            raw_names = entry.get("Names") or []
            for n in raw_names:
                stripped = n[1:] if n.startswith("/") else n
                if stripped:  # pragma: no cover -- defensive; Docker names are non-empty
                    names.add(stripped)
        return names

    async def _release_all_ownership(self) -> None:
        """Atomically wipe ownership + soft-delete all file_override rows."""
        now = utc_now_iso()
        async with self._db.transaction() as conn:
            previous_owned = await OverrideOwnershipRepository.list_owned_conn(conn)
            for cn in previous_owned:
                await ProbeTargetsRepository.mark_missing_except_conn(
                    conn,
                    container_name=cn,
                    kept_keys=set(),
                    now=now,
                )
            await OverrideOwnershipRepository.set_owned_conn(
                conn,
                container_names=set(),
                now=now,
            )


__all__ = ["OverrideLoader"]
