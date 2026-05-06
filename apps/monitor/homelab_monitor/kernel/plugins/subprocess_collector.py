"""Subprocess collector wrapper.

A Python class-factory that produces a unique BaseCollector subclass for
each SubprocessManifest, wiring the manifest's ClassVars onto the class
and delegating `run()` to subprocess_runner.run_subprocess.

Lives at kernel/plugins/ alongside the runner because it is plugin-tier
infrastructure (the bridge between Collector Protocol and the runner),
not a specific plugin implementation. Built-in plugin implementations
live at homelab_monitor/plugins/<kind>/<name>/ per spec §5.5.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from homelab_monitor.kernel.events import current_tick
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.manifest import SubprocessManifest
from homelab_monitor.kernel.plugins.subprocess_runner import run_subprocess
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind


def make_subprocess_collector(
    manifest: SubprocessManifest,
    manifest_dir: Path,
) -> type[BaseCollector]:
    """Class-factory: produce a unique BaseCollector subclass for this manifest.

    The returned class has ClassVars set from manifest fields:
      name, interval, timeout, concurrency_group, run_kind=SUBPROCESS,
      trust_level (manifest's trust_level).
    Its run() delegates to run_subprocess(manifest, ctx, manifest_dir).

    Pattern matches `_make_collector` test factory: uses `type()` to build
    a named subclass of BaseCollector at runtime.
    """

    async def _run(self: BaseCollector, ctx: CollectorContext) -> CollectorResult:
        del self  # not used; manifest captured via closure
        tick = current_tick()
        extra_env: dict[str, str] = {}
        if (
            tick is not None
        ):  # pragma: no cover -- only reachable when collector runs within scheduler tick context
            tick_id, trigger = tick
            extra_env["HOMELAB_TICK_ID"] = tick_id
            if trigger is not None:
                extra_env["HOMELAB_TRIGGER_KIND"] = trigger.kind
                if (
                    trigger.request_id is not None
                ):  # pragma: no cover -- requires request-triggered tick
                    extra_env["HOMELAB_REQUEST_ID"] = trigger.request_id
        return await run_subprocess(manifest, ctx, manifest_dir=manifest_dir, extra_env=extra_env)

    cls = type(
        f"_SubprocessCollector_{manifest.name.replace('-', '_')}",
        (BaseCollector,),
        {
            "name": manifest.name,
            "interval": manifest.interval,
            "timeout": manifest.timeout,
            "concurrency_group": manifest.concurrency_group,
            "run_kind": RunKind.SUBPROCESS,
            "trust_level": manifest.trust_level,
            "run": _run,
        },
    )
    return cast(type[BaseCollector], cls)


__all__ = ["make_subprocess_collector"]
