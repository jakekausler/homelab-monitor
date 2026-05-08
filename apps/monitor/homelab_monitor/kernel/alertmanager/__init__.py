"""Alertmanager render-on-boot + reload module.

Reads ``deploy/alertmanager/alertmanager.yml.template``, substitutes
``${ALERTMANAGER_INGEST_TOKEN}`` with a system-minted API token plaintext,
writes the rendered config to a shared docker volume that the Alertmanager
container reads. Reload-on-rotation re-renders + POSTs ``/-/reload``.
"""

from homelab_monitor.kernel.alertmanager.render import (
    AlertmanagerReloader,
    ensure_ingest_token,
    render_config,
)

__all__ = ["AlertmanagerReloader", "ensure_ingest_token", "render_config"]
