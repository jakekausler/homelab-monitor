"""Deterministic fingerprint helpers for alerts.

Two flavors:
- ``compute_fingerprint``: derive a fingerprint from an Alertmanager v2 alert.
  Prefer the upstream ``fingerprint`` field when present; otherwise hash the
  sorted-by-key labels JSON so identical label sets always produce the same
  fingerprint regardless of caller-supplied dict ordering.
- ``quarantine_fingerprint``: deterministic fingerprint for collector
  quarantine alerts (synthesised by the scheduler in Spec B). Distinct
  collectors / reasons MUST produce distinct fingerprints.
"""

from __future__ import annotations

import hashlib
import json

from homelab_monitor.kernel.alerts.types import AlertmanagerV2AlertItem


def compute_fingerprint(item: AlertmanagerV2AlertItem) -> str:
    """Return a stable fingerprint for an Alertmanager v2 alert.

    If the upstream payload supplied a ``fingerprint`` (non-empty), trust it.
    Otherwise compute SHA-256 over the labels mapping serialised as JSON with
    keys sorted, so different insertion orders of the same labels collide
    correctly.
    """
    if item.fingerprint:
        return item.fingerprint
    sorted_labels = json.dumps(item.labels, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(sorted_labels.encode("utf-8")).hexdigest()


def quarantine_fingerprint(collector_name: str, reason: str) -> str:
    """Return a deterministic fingerprint for a collector-quarantine alert.

    The scheduler synthesises an alert when a collector enters quarantine
    (Spec B). The fingerprint MUST be deterministic so re-firings dedupe to
    the same row, but distinct (collector, reason) pairs MUST yield distinct
    fingerprints.

    F19: serialise via JSON instead of colon-joined string so a
    ``collector_name`` containing ``":"`` (legal in some user inputs)
    cannot collide with a different ``(collector_name, reason)`` pair.
    Compatibility note: this CHANGES the fingerprint format. STAGE-013's
    alerts table is fresh, so no alert rows persist across the upgrade.
    Subsequent stages that may persist alerts MUST run a one-shot
    re-fingerprint migration if any quarantine alerts are in flight.
    """
    payload = json.dumps(
        {
            "alertname": "collector_quarantined",
            "name": collector_name,
            "reason": reason,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
