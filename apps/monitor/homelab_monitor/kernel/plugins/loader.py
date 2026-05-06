"""In-process plugin discovery for STAGE-001-007.

This stage ships only the **programmatic registry** path: callers explicitly
``register(...)`` collector classes with optional config overrides. The loader
returns :class:`LoadedCollector` records that the scheduler consumes.

Future stages extend this without changing the consumer (scheduler) API:

- STAGE-001-009 — filesystem scan over ``homelab_monitor/plugins/collectors/builtin/``
  and ``integrations/``; subprocess-runner fallback for UNTRUSTED collectors.
- EPIC-002+ — entry-point scan via ``importlib.metadata.entry_points(group=...)``.

The split exists so the scheduler can be tested today against a known fixed
collector set, and so plugin discovery can grow into a richer surface
(filesystem layout, manifest validation, version pinning) without churning the
scheduler.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import structlog
import yaml
from pydantic import ValidationError
from sqlalchemy import text
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.plugins.base import Collector
from homelab_monitor.kernel.plugins.manifest import SubprocessManifest
from homelab_monitor.kernel.plugins.subprocess_collector import make_subprocess_collector
from homelab_monitor.kernel.plugins.types import CollectorConfig


@dataclass(frozen=True, slots=True)
class LoadedCollector:
    """Pairing of a constructed collector instance with its validated config.

    The scheduler treats this as opaque: ``collector`` exposes the
    :class:`Collector` Protocol (``name``, ``interval``, ``timeout``,
    ``run_kind``, ``trust_level``, ``concurrency_group``, async ``run``); the
    ``config`` is the source of truth for runtime parameters (interval seconds,
    timeout seconds, enabled flag, plus any plugin-specific subclass fields).

    ``frozen=True`` because the scheduler should never mutate this record after
    discovery.
    """

    collector: Collector
    # SCAFFOLDING: STAGE-001-010 ctx_factory will use this config to override
    # ClassVar interval/timeout via CollectorContext.config field. The
    # scheduler currently reads c.interval / c.timeout from ClassVars only
    # (intentional for STAGE-001-007); runtime override delivery is the
    # integration point in STAGE-001-010 FastAPI lifespan.
    config: CollectorConfig


class PluginLoader:
    """Programmatic in-memory registry of in-process Python collectors.

    For STAGE-001-007 this is the complete loader: callers populate it with
    :meth:`register` and consume the contents via :meth:`load_all`. Filesystem
    + entry-point scans land in later stages (see module docstring).

    Usage::

        loader = PluginLoader()
        loader.register(NoopCollector, {"name": "noop", "interval_seconds": 60})
        loaded = loader.load_all()
        # -> [LoadedCollector(collector=<NoopCollector>, config=CollectorConfig(...))]
    """

    def __init__(self, log: BoundLogger | None = None) -> None:
        """Construct an empty registry."""
        self._loaded: list[LoadedCollector] = []
        self._log: BoundLogger = log if log is not None else structlog.stdlib.get_logger().bind()
        self._subprocess_declared_secrets: dict[str, list[str]] = {}

    def register(
        self,
        collector_cls: type[Collector],
        config_overrides: dict[str, object] | None = None,
    ) -> LoadedCollector:
        """Instantiate ``collector_cls`` and validate its config.

        ``config_overrides`` is a flat dict whose keys map to fields on
        :class:`CollectorConfig` (or a plugin-defined subclass). It MUST at
        minimum contain ``"name"`` — the regex-checked collector name. If
        omitted, the call fails Pydantic validation as ``name`` has no default.

        Returns the constructed :class:`LoadedCollector` AND appends it to the
        loader's internal list (so subsequent :meth:`load_all` calls include it).

        Raises:
            pydantic.ValidationError: when ``config_overrides`` violates
                :class:`CollectorConfig` constraints (e.g. ``name`` regex fails,
                ``interval_seconds < 1``, unknown extra fields with
                ``extra="forbid"``).
            TypeError: when ``collector_cls()`` fails to instantiate (most
                commonly because BaseCollector's ``__init_subclass__`` rejected
                a concrete subclass missing required ClassVars — but those raise
                at class-creation time, so the more likely cause here is a
                non-zero-arg ``__init__``).
        """
        overrides = dict(config_overrides) if config_overrides else {}
        # CollectorConfig validates name pattern + interval/timeout bounds + extra="forbid".
        config = CollectorConfig.model_validate(overrides)
        # Collector Protocol implies a zero-arg constructor; BaseCollector subclasses
        # inherit ABC's __init__ which is zero-arg.
        instance = collector_cls()
        loaded = LoadedCollector(collector=instance, config=config)
        self._loaded.append(loaded)
        return loaded

    def load_all(self) -> list[LoadedCollector]:
        """Return all registered :class:`LoadedCollector` records (defensive copy).

        # SCAFFOLDING: STAGE-001-009 will extend this to also walk the built-in
        # filesystem layout (``homelab_monitor/plugins/collectors/builtin/`` and
        # ``integrations/``) and read ``/config/plugins/collectors/<name>.yaml``
        # for per-collector overrides.
        # SCAFFOLDING: EPIC-002+ will extend this to scan
        # ``importlib.metadata.entry_points(group="homelab_monitor.collectors")``
        # and validate manifests.
        """
        return list(self._loaded)

    def config_for(self, name: str) -> CollectorConfig:
        """Get the CollectorConfig for a named collector.

        Args:
            name: Collector name.

        Returns:
            CollectorConfig: The collector's configuration.

        Raises:
            KeyError: If the collector is not found.
        """
        for lc in self._loaded:  # pragma: no cover
            if lc.config.name == name:
                return lc.config
        msg = f"unknown collector: {name}"  # pragma: no cover
        raise KeyError(msg)  # pragma: no cover

    def declared_secrets(self, name: str) -> list[str]:
        """Get the list of secrets declared by a subprocess collector.

        Args:
            name: Collector name.

        Returns:
            List of secret names declared in the manifest (empty for non-subprocess collectors).
        """
        return list(self._subprocess_declared_secrets.get(name, []))

    async def persist_to_db(self, repo: SqliteRepository) -> None:
        """INSERT OR IGNORE collector rows for all registered collectors.

        Idempotent. STAGE-001-010 lifespan calls this after `load_all()` and
        before `scheduler.start()` so FailureBudget UPDATEs have rows to target.

        SCAFFOLDING: closes STAGE-001-008's loader-INSERT gap discovered during
        Refinement.
        """
        async with repo.transaction() as conn:
            for loaded in self._loaded:
                await conn.execute(
                    text(
                        "INSERT OR IGNORE INTO collectors "
                        "(id, name, config, created_at) "
                        "VALUES (:id, :name, :config, :created_at)"
                    ),
                    {
                        "id": uuid7(),
                        "name": loaded.config.name,
                        "config": json.dumps(loaded.config.model_dump(mode="json")),
                        "created_at": utc_now_iso(),
                    },
                )

    def load_subprocess_plugins(self, plugins_dir: Path) -> int:
        """Walk plugins_dir for plugin.yaml manifests; register each as a SubprocessCollector.

        Each found manifest is validated. Per-manifest validation failures are
        logged at warning level; the scan continues (one bad manifest does not
        block the rest).

        SCAFFOLDING: STAGE-001-010 lifespan will call this with the user's
        /plugins mount path. For STAGE-009, only the runbooks/_examples
        directory is wired (via tests).

        Args:
            plugins_dir: Directory tree to walk recursively for `plugin.yaml`.

        Returns:
            Number of plugins successfully registered.
        """
        if not plugins_dir.exists():
            return 0
        count = 0
        for manifest_path in plugins_dir.rglob("plugin.yaml"):
            try:
                manifest = SubprocessManifest.load_from_path(manifest_path)
            except (ValidationError, OSError, yaml.YAMLError, ValueError) as e:
                self._log.warning(
                    "loader.subprocess_plugin_invalid",
                    manifest_path=str(manifest_path),
                    error=str(e),
                )
                continue

            # Verify cmd[0] is executable
            cmd0 = manifest.command[0]
            if "/" in cmd0:  # pragma: no cover -- relative-path integration tested
                # Relative path resolved against manifest dir
                resolved = (manifest_path.parent / cmd0).resolve()
                if not (
                    resolved.exists() and os.access(resolved, os.X_OK)
                ):  # pragma: no cover -- non-executable integration tested
                    self._log.warning(
                        "loader.subprocess_plugin_invalid",
                        manifest_path=str(manifest_path),
                        error=f"command[0] not executable: {resolved}",
                    )
                    continue
            # Bare command name resolved via PATH
            elif shutil.which(cmd0) is None:  # pragma: no cover -- not-on-path integration tested
                self._log.warning(
                    "loader.subprocess_plugin_invalid",
                    manifest_path=str(manifest_path),
                    error=f"command[0] not on PATH: {cmd0}",
                )
                continue

            cls = make_subprocess_collector(manifest, manifest_dir=manifest_path.parent)
            self.register(cls, config_overrides={"name": manifest.name})
            # Store declared secrets for this subprocess plugin
            self._subprocess_declared_secrets[manifest.name] = list(manifest.secrets)
            count += 1
        return count
