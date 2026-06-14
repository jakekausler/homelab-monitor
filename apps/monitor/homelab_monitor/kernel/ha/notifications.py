"""Shared parsing for Home Assistant persistent-notification WS payloads.

Single source of parsing truth for both the persistent-notification collector
and the live-notifications API endpoint.
"""

from __future__ import annotations

from typing import cast


def extract_notifications(result: dict[str, object] | list[object]) -> list[object]:
    """Defensively extract the notifications list from a ``send_command`` result.

    Handles:
    (a) bare list — return as-is.
    (b) dict wrapping the list under ``notifications`` — return that list.
    (c) any other dict (e.g. the ``{}`` degenerate) — return [].
    """
    payload: object = result  # widen: runtime value may be a list.
    if isinstance(payload, list):
        return payload
    notifications_dict = payload
    candidate = notifications_dict.get("notifications")
    if isinstance(candidate, list):
        return cast("list[object]", candidate)
    return []
