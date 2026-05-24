"""Shared Docker container name utilities (STAGE-003-008).

Extracted from image_update_collector.py for reuse with docker_discoverer.py.
"""

from __future__ import annotations

from typing import Final

_DOCKER_ID_PREFIX_HEX_LENGTH: Final[int] = 13  # 12 hex chars + underscore


def canonicalize_container_name(raw: str) -> str:
    """Strip leading '/' and a '<12hex>_' prefix from a Docker container name.

    Docker compose --force-recreate temporarily renames the old container as
    "<12-hex>_<original>". Strip that prefix so callers key on the canonical
    name. A container literally named "<12-hex>_<X>" by the operator would be
    mis-stripped, but this pattern is astronomically unlikely.
    """
    name = raw[1:] if raw.startswith("/") else raw
    # <12hex>_ prefix detection: 12 hex chars + '_' = index 12 is '_'
    if len(name) >= _DOCKER_ID_PREFIX_HEX_LENGTH and name[12] == "_":
        head = name[:12]
        if all(
            c in "0123456789abcdef" for c in head.lower()
        ):  # pragma: no branch -- covered by test_image_update_collector tests
            return name[_DOCKER_ID_PREFIX_HEX_LENGTH:]
    return name


__all__ = ["_DOCKER_ID_PREFIX_HEX_LENGTH", "canonicalize_container_name"]
