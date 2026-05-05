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

from dataclasses import dataclass

from homelab_monitor.kernel.plugins.base import Collector
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

    def __init__(self) -> None:
        """Construct an empty registry."""
        self._loaded: list[LoadedCollector] = []

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
