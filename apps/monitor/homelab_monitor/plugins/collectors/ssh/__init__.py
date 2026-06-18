"""SSH probe bundle (EPIC-017).

Exposes :func:`register_all`, which the FastAPI lifespan calls once at startup.
Each ssh probe is registered with per-probe failure isolation. Wave-B stages
(EPIC-007, EPIC-008) add probes here.
"""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.plugins.loader import PluginLoader, config_from_classvars
from homelab_monitor.kernel.ssh import load_ssh_target_configs
from homelab_monitor.plugins.collectors.ssh.uptime import make_uptime_probe

_log = structlog.get_logger()


def register_all(loader: PluginLoader) -> None:
    """Register one UptimeProbe per configured SSH target, with per-probe isolation."""
    for target_id in load_ssh_target_configs():
        try:
            cls = make_uptime_probe(target_id)
            loader.register(cls, config_from_classvars(cls))
        except Exception as exc:
            _log.warning(
                "ssh_bundle.probe_register_failed",
                name=f"uptime-{target_id}",
                error=str(exc),
            )
