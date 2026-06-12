"""Shared helpers for building audit-log fields from API principals."""

from __future__ import annotations

from homelab_monitor.kernel.auth.models import ApiToken, User


def principal_label(principal: User | ApiToken) -> str:
    """Format the audit ``who`` column for a user or token principal.

    Returns ``api-token:<id>`` for token auth and ``user:<username>`` for session auth.
    """
    if isinstance(principal, ApiToken):
        return f"api-token:{principal.id}"
    return f"user:{principal.username}"
