"""Best-effort DPI application/category ID -> human-name catalog.

UniFi DPI exposes numeric ``application``/``category`` IDs with NO controller
endpoint to resolve them (``rest/dpiapp`` is empty on this firmware). This module
bundles a comprehensive vendored lookup (~2,200 apps, ~22 categories) sourced
from ubntwiki cat_app.json and unpoller dpi.go.

Callers MUST fall back to the stringified raw ID when a key is absent (never
crash, never blank). The ``resolve_app`` / ``resolve_cat`` functions enforce this.

Keying (matches unpoller's DPIApps/DPICats):
- ``APP_NAMES`` is keyed by the COMPOUND id ``(category << 16) + application``.
- ``CAT_NAMES`` is keyed by the ``category`` id alone.

Data lives in ``dpi_catalog_data.py`` (a generated vendored snapshot). To
refresh, re-run the data-generation step documented there and replace that file.
"""

from __future__ import annotations

from typing import Final

from homelab_monitor.plugins.collectors.integrations.unifi.dpi_catalog_data import (
    APP_NAMES as _APP_NAMES,
)
from homelab_monitor.plugins.collectors.integrations.unifi.dpi_catalog_data import (
    CAT_NAMES as _CAT_NAMES,
)

# Re-export as Final so callers that import APP_NAMES/CAT_NAMES directly still work.
APP_NAMES: Final[dict[int, str]] = _APP_NAMES
CAT_NAMES: Final[dict[int, str]] = _CAT_NAMES


def app_key(category: int, application: int) -> int:
    """Compound key for APP_NAMES: ``(category << 16) + application``."""
    return (category << 16) + application


def resolve_app(category: int, application: int) -> str:
    """Resolve an app name, falling back to the raw application id as a string."""
    return APP_NAMES.get(app_key(category, application), str(application))


def resolve_cat(category: int) -> str:
    """Resolve a category name, falling back to the raw category id as a string."""
    return CAT_NAMES.get(category, str(category))
