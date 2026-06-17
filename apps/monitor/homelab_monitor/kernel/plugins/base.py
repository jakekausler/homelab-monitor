"""Collector Protocol and BaseCollector ABC.

Plugin authors typically subclass :class:`BaseCollector` for the ergonomic defaults.
Subprocess plugins (STAGE-001-009) satisfy the :class:`Collector` Protocol structurally
via the JSON-RPC bridge, without importing this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta
from typing import ClassVar, Protocol, runtime_checkable

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel


@runtime_checkable
class Collector(Protocol):
    """Public structural contract every collector must satisfy.

    Plugin authors implement this either by subclassing :class:`BaseCollector`
    or by writing a free-standing class with the same shape (subprocess plugins
    do the latter via JSON-RPC).
    """

    name: ClassVar[str]
    interval: ClassVar[timedelta]
    timeout: ClassVar[timedelta]
    concurrency_group: ClassVar[str]
    run_kind: ClassVar[RunKind]
    trust_level: ClassVar[TrustLevel]

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Execute the collector against ``ctx`` and return a :class:`CollectorResult`."""
        ...


class BaseCollector(ABC):
    """Ergonomic ABC base for in-process Python collectors.

    Provides sensible defaults: ``run_kind = ASYNC``, ``trust_level = BUILTIN``,
    ``concurrency_group = "default"``. Subclasses MUST set ``name``, ``interval``,
    ``timeout``, and implement :meth:`run`.
    """

    name: ClassVar[str]
    interval: ClassVar[timedelta]
    timeout: ClassVar[timedelta]
    # TODO: validate against pattern when scheduler lands (STAGE-001-008+)
    concurrency_group: ClassVar[str] = "default"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN
    # Intermediate-base marker: a subclass that sets ``abstract = True`` in its OWN
    # body is exempt from required-ClassVar enforcement (it's a framework layer, not
    # an instantiable collector). Concrete subclasses do NOT set it (default False),
    # so they remain enforced. Needed for bases like ``SshProbe`` that override
    # ``run()`` and thus defeat the ``cls.run is BaseCollector.run`` heuristic below.
    # Intentionally NOT part of the `Collector` Protocol — this is an ABC-enforcement
    # detail (skips required-ClassVar checks for abstract intermediates like SshProbe).
    abstract: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Enforce required ClassVars on concrete subclasses.

        Abstract subclasses (those that don't override ``run``) are exempt — they're
        meant to be intermediate layers, not instantiated. We check this by comparing
        ``cls.run`` against this class's ``run``; ``__abstractmethods__`` is not yet
        populated when ``__init_subclass__`` fires.
        """
        super().__init_subclass__(**kwargs)
        # Skip enforcement for explicit intermediate bases (those that set
        # ``abstract = True`` in their OWN body). ``cls.__dict__.get`` (NOT
        # ``getattr``) is deliberate: it reads only THIS class's attribute so the
        # marker never inherits to concrete subclasses, which stay enforced.
        if cls.__dict__.get("abstract") is True:
            return
        # Skip enforcement for abstract subclasses (those that didn't override run).
        if cls.run is BaseCollector.run:
            return
        for required in ("name", "interval", "timeout"):
            if not getattr(cls, required, None):
                msg = f"{cls.__name__} must define ClassVar `{required}`"
                raise TypeError(msg)

    @abstractmethod
    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Execute the collector against ``ctx`` and return a :class:`CollectorResult`."""
        ...
