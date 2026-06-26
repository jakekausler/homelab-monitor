"""SSH probe bundle (EPIC-017).

Exposes :func:`register_all`, which the FastAPI lifespan calls once at startup.
Each ssh probe is registered with per-probe failure isolation. Wave-B stages
(EPIC-007, EPIC-008) add probes here.
"""

from __future__ import annotations

from typing import Final

import structlog

from homelab_monitor.kernel.plugins.loader import PluginLoader, config_from_classvars
from homelab_monitor.kernel.ssh import load_ssh_target_configs
from homelab_monitor.plugins.collectors.ssh.synology import SynologyProbe
from homelab_monitor.plugins.collectors.ssh.uptime import make_uptime_probe

_log = structlog.get_logger()

_SYNOLOGY_TARGET_ID: Final[str] = "synology"


def register_all(loader: PluginLoader) -> None:
    """Register SSH probes per configured target, with per-probe failure isolation.

    The `synology` target gets the combined SynologyProbe (which subsumes liveness); every
    other target gets the generic UptimeProbe. The old `uptime-synology` probe is intentionally
    not registered.
    """
    for target_id in load_ssh_target_configs():
        if target_id == _SYNOLOGY_TARGET_ID:
            try:
                loader.register(SynologyProbe, config_from_classvars(SynologyProbe))
            except Exception as exc:
                _log.warning(
                    "ssh_bundle.probe_register_failed",
                    name=SynologyProbe.name,
                    error=str(exc),
                )
            continue
        try:
            cls = make_uptime_probe(target_id)
            loader.register(cls, config_from_classvars(cls))
        except Exception as exc:
            _log.warning(
                "ssh_bundle.probe_register_failed",
                name=f"uptime-{target_id}",
                error=str(exc),
            )
