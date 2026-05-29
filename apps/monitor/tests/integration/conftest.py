"""Integration-test conftest.

The rig health-probe gate (helpers/rig_health.require_rig_components) handles
fast-skip when rig components are unreachable. No session fixture is required:
the gate uses a per-process module-level cache (see helpers/rig_health.py for the
pytest-xdist rationale). This conftest only re-exports the gate for convenience.
"""

from __future__ import annotations

from .helpers.rig_health import require_rig_components

__all__ = ["require_rig_components"]
