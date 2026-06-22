"""Typed Pi-hole client errors (STAGE-006-001).

Return-not-raise discriminated union mirroring
:class:`homelab_monitor.kernel.unifi.errors.UnifiError`.

D-PIHOLE-CLIENT-RETURN-NOT-RAISE: every Pi-hole client helper returns a
:class:`PiholeResponse` OR a :class:`PiholeError`, so a Pi-hole 5xx / timeout /
auth failure never propagates as our own 5xx. The collector converts a
PiholeError into a failed run; the FailureBudget handles quarantine + recovery.

SECURITY: ``message`` must NEVER contain the Pi-hole app password. Error messages
are built only from the request method + path + status (and, for auth failures,
the Pi-hole-provided ``session.message``) — never from the ``password`` field, the
``X-FTL-SID`` header, or the SID value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PiholeErrorReason = Literal[
    "unreachable",
    "timeout",
    "auth",
    "rate_limited",
    "http_error",
    "bad_response",
]


@dataclass(frozen=True, slots=True)
class PiholeError:
    """A typed, non-raising Pi-hole client failure.

    ``reason`` discriminates the failure class:
      - ``unreachable`` — connect-level failure (httpx.ConnectError / ConnectTimeout).
      - ``timeout`` — read/overall timeout (httpx.ReadTimeout / TimeoutException).
      - ``auth`` — HTTP 401 (after one re-auth retry), a failed login
        (``session.valid`` False / missing sid), OR no password configured.
      - ``rate_limited`` — HTTP 429 (FTL is throttling us).
      - ``http_error`` — any other non-2xx status (``status`` carries the code);
        a Pi-hole 5xx surfaces here, never as our own 5xx.
      - ``bad_response`` — 2xx but the body could not be parsed / shaped as expected.

    ``status`` is the HTTP status code when known (set for ``auth`` from a real 401,
    for ``rate_limited`` from 429, and for ``http_error``; None for ``unreachable`` /
    ``timeout`` / no-password / login-shape failures / ``bad_response``).
    """

    reason: PiholeErrorReason
    message: str
    status: int | None = None
