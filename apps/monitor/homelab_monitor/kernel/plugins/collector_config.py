"""Per-collector YAML override loading (STAGE-008-032).

Mirrors :func:`homelab_monitor.kernel.config.load_disk_budget_config`'s YAML-load +
type-guard idiom and :class:`OverrideLoader`'s ``/config/plugins/<x>/*.yaml`` directory
convention. Used by :meth:`PluginLoader.register` to populate a collector's
``CollectorConfig`` subclass fields from ``/config/plugins/collectors/<name>.yaml``.

Absent dir or absent file => empty dict (no overrides). A present file whose YAML root is
not a mapping raises ``ValueError``. Validation against the (possibly subclass) config model
happens in the caller, so an unknown key surfaces as a Pydantic ``ValidationError`` there.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final, cast

import yaml

_DEFAULT_COLLECTOR_OVERRIDES_DIR: Final[str] = "/config/plugins/collectors"
_COLLECTOR_OVERRIDES_DIR_ENV: Final[str] = "HOMELAB_MONITOR_COLLECTOR_OVERRIDES_DIR"


def load_collector_overrides(name: str) -> dict[str, object]:
    """Load per-collector YAML overrides for ``name`` from the overrides dir.

    The directory is read from ``HOMELAB_MONITOR_COLLECTOR_OVERRIDES_DIR`` (default
    ``/config/plugins/collectors``). The file is ``<dir>/<name>.yaml``.

    Returns:
        The parsed mapping of override fields, or an empty dict when the file is absent
        or empty.

    Raises:
        ValueError: if the file exists but its YAML root is not a mapping.
        yaml.YAMLError: if the file exists but is not parseable YAML.
    """
    overrides_dir = Path(
        os.environ.get(_COLLECTOR_OVERRIDES_DIR_ENV, _DEFAULT_COLLECTOR_OVERRIDES_DIR)
    )
    path = overrides_dir / f"{name}.yaml"
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        raw_obj: object = yaml.safe_load(f) or {}
    if not isinstance(raw_obj, dict):
        msg = f"collector override root must be a mapping, got {type(raw_obj).__name__}"
        raise ValueError(msg)
    return cast(dict[str, object], raw_obj)


__all__ = ["load_collector_overrides"]
