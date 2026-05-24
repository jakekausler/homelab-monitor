"""BuildSourcesLoader — periodic /config/docker/build-sources.yaml scanner.

D-BUILD-SOURCES-YAML-CONFIG: supersedes HOMELAB_MONITOR_COMPOSE_DIR when present.
D-HOTRELOAD-PERIODIC-30S: mirrors OverrideLoader's lifecycle exactly. NOT a BaseCollector —
does not register through PluginLoader. Launched and shut down from lifespan.
D-PUBLIC-DEFAULT-UNCHANGED: missing file is success state (current_config=None,
current_error=None); collector falls back to env-var compose_dir.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

from homelab_monitor.kernel.docker.build_sources_schema import (
    BuildSourcesConfig,
    BuildSourcesConfigError,
)

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

_REFRESH_INTERVAL_SECONDS: Final[float] = 30.0


class BuildSourcesLoader:
    """Periodic scanner for /config/docker/build-sources.yaml."""

    def __init__(
        self,
        *,
        config_path: Path,
        log: BoundLogger,
        refresh_interval_seconds: float = _REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self._config_path: Path = config_path
        self._log: BoundLogger = log
        self._refresh_interval_seconds: float = refresh_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._current_config: BuildSourcesConfig | None = None
        self._current_error: BuildSourcesConfigError | None = None

    @property
    def current_config(self) -> BuildSourcesConfig | None:
        """Return the currently loaded config, or None if absent or invalid."""
        return self._current_config

    @property
    def current_error(self) -> BuildSourcesConfigError | None:
        """Return the current config error, or None if config is valid or absent."""
        return self._current_error

    def start_task(self) -> None:
        """Launch the periodic refresh task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._refresh_loop(), name="build_sources_loader.refresh")

    async def stop_task(self) -> None:
        """Cancel + await the refresh task."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _refresh_loop(self) -> None:
        """Run forever, refreshing every `_refresh_interval_seconds`.

        Re-raises asyncio.CancelledError so callers awaiting the task
        observe cancellation. Per-tick exceptions are caught + logged;
        the loop keeps running.
        """
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:  # pragma: no cover -- cancel mid-refresh race
                raise
            except Exception as exc:  # pragma: no cover -- defensive
                self._log.warning("build_sources_loader.refresh_failed", error=str(exc))
            try:
                await asyncio.sleep(self._refresh_interval_seconds)
            except asyncio.CancelledError:
                raise

    async def refresh(self) -> None:
        """Scan config file and load BuildSourcesConfig.

        On file_not_found: sets current_config=None, current_error=None.
        On other BuildSourcesConfigError: sets current_config=None, current_error=exc.
        On success: sets current_config=config, current_error=None.
        """
        start = time.monotonic()
        try:
            config = BuildSourcesConfig.load_from_path(self._config_path)
        except BuildSourcesConfigError as exc:
            if exc.reason == "file_not_found":
                self._current_config = None
                self._current_error = None
                self._log.info(
                    "build_sources_loader.refresh_complete",
                    state="absent",
                    path=str(self._config_path),
                    duration_seconds=round(time.monotonic() - start, 4),
                )
                return
            # Other errors (malformed, invalid schema, non-dict root, unknown)
            self._current_config = None
            self._current_error = exc
            self._log.warning(
                "build_sources_loader.refresh_failed",
                path=str(self._config_path),
                reason=exc.reason,
                error=str(exc),
                duration_seconds=round(time.monotonic() - start, 4),
            )
            return
        # Success
        self._current_config = config
        self._current_error = None
        self._log.info(
            "build_sources_loader.refresh_complete",
            state="loaded",
            compose_files=len(config.compose_files),
            remaps=len(config.build_context_roots),
            duration_seconds=round(time.monotonic() - start, 4),
        )


__all__: Final = ["BuildSourcesLoader"]
