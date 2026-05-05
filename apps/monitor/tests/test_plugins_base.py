"""Tests for Collector Protocol + BaseCollector ABC defaults and structural typing."""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar, cast

import pytest

from homelab_monitor.kernel.plugins.base import BaseCollector, Collector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel


class _FreestandingCollector:
    """A class that satisfies the Collector Protocol structurally — no inheritance."""

    name: ClassVar[str] = "freestanding"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=5)
    concurrency_group: ClassVar[str] = "default"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.TRUSTED

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        del ctx
        return CollectorResult(ok=True)


def test_freestanding_class_assignable_to_collector() -> None:
    """A class with the right shape satisfies the Protocol structurally (static-typing check)."""
    obj = _FreestandingCollector()
    as_collector: Collector = obj
    assert as_collector.name == "freestanding"


# --- BaseCollector defaults -----------------------------------------------------------------


class _ConcreteCollector(BaseCollector):
    name: ClassVar[str] = "concrete"
    interval: ClassVar[timedelta] = timedelta(seconds=10)
    timeout: ClassVar[timedelta] = timedelta(seconds=2)

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        del ctx
        return CollectorResult(ok=True, metrics_emitted=0)


def test_base_collector_default_run_kind_is_async() -> None:
    """BaseCollector subclasses default to RunKind.ASYNC."""
    assert _ConcreteCollector.run_kind is RunKind.ASYNC


def test_base_collector_default_trust_level_is_builtin() -> None:
    """BaseCollector subclasses default to TrustLevel.BUILTIN."""
    assert _ConcreteCollector.trust_level is TrustLevel.BUILTIN


def test_base_collector_default_concurrency_group_is_default() -> None:
    """BaseCollector subclasses default ``concurrency_group`` to ``\"default\"``."""
    assert _ConcreteCollector.concurrency_group == "default"


def test_concrete_subclass_is_instantiable() -> None:
    """A subclass that overrides ``run`` can be instantiated."""
    c = _ConcreteCollector()
    assert c.name == "concrete"


def test_abstract_subclass_cannot_be_instantiated() -> None:
    """A subclass that does NOT override ``run`` is abstract."""

    class _Abstract(BaseCollector):
        name: ClassVar[str] = "abstract"
        interval: ClassVar[timedelta] = timedelta(seconds=1)
        timeout: ClassVar[timedelta] = timedelta(seconds=1)

    with pytest.raises(TypeError):
        _Abstract()  # type: ignore[abstract]


async def test_concrete_run_returns_collector_result() -> None:
    """The overridden ``run`` returns a CollectorResult."""
    c = _ConcreteCollector()
    result = await c.run(cast(CollectorContext, object()))
    assert isinstance(result, CollectorResult)
    assert result.ok is True


def test_concrete_satisfies_collector_protocol() -> None:
    """A BaseCollector subclass is assignable to the Collector Protocol."""
    c: Collector = _ConcreteCollector()
    assert c.name == "concrete"


def test_base_collector_subclass_missing_classvar_raises() -> None:
    """A concrete subclass that forgets a required ClassVar fails clearly at class-creation time."""
    with pytest.raises(TypeError, match="must define ClassVar `name`"):

        class BadCollector(BaseCollector):  # pyright: ignore[reportUnusedClass]
            # Missing `name`, `interval`, `timeout` — but provides run, so not abstract.
            # The class definition itself raises TypeError via __init_subclass__,
            # so the class is never actually used; pyright can't infer this.
            async def run(self, ctx: CollectorContext) -> CollectorResult:
                _ = ctx
                return CollectorResult(
                    ok=True,
                    metrics_emitted=0,
                    errors=[],
                    events=[],
                    duration_seconds=0.0,
                )


def test_base_collector_abstract_subclass_skipped() -> None:
    """Abstract subclasses are exempt from required-ClassVar enforcement.

    Covers the early-return branch in ``__init_subclass__``: subclasses that
    haven't implemented ``run`` are intermediate ABCs and shouldn't need to
    set ``name``/``interval``/``timeout``.
    """

    # No exception expected — class definition succeeds despite missing ClassVars
    # because the class is still abstract (run is not overridden).
    class IntermediateCollector(BaseCollector):  # pyright: ignore[reportUnusedClass]
        # Missing all required ClassVars and missing run() override: still abstract.
        pass

    # If we got here, the early-return branch was exercised.
    assert IntermediateCollector.__abstractmethods__
