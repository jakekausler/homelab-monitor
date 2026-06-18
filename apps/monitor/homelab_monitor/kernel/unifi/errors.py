"""Typed Unifi client errors (STAGE-007-001).

Return-not-raise discriminated union mirroring
:class:`homelab_monitor.kernel.ha.errors.HaError`.

D-UNIFI-CLIENT-RETURN-NOT-RAISE: every Unifi client GET helper returns a
:class:`UnifiResponse` OR a :class:`UnifiError`, so a UDM 5xx / timeout / auth
failure never propagates as our own 5xx. The collector converts a UnifiError
into a failed run; the FailureBudget handles quarantine + recovery.

SECURITY: ``message`` must NEVER contain the ``unifi_api_key``. Error messages
are built only from the request method + endpoint label + status — never from
the ``X-API-KEY`` header or the key value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

UnifiErrorReason = Literal[
    "unreachable",
    "timeout",
    "auth",
    "rate_limited",
    "http_error",
    "bad_response",
]


@dataclass(frozen=True, slots=True)
class UnifiError:
    """A typed, non-raising Unifi client failure.

    ``reason`` discriminates the failure class:
      - ``unreachable`` — connect-level failure (httpx.ConnectError / ConnectTimeout).
      - ``timeout`` — read/overall timeout (httpx.ReadTimeout / TimeoutException).
      - ``auth`` — HTTP 401/403, OR no API key configured at request time.
      - ``rate_limited`` — HTTP 429 (the controller is throttling us).
      - ``http_error`` — any other non-2xx status (``status`` carries the code);
        a UDM 5xx surfaces here, never as our own 5xx.
      - ``bad_response`` — 2xx but the body could not be parsed / shaped as expected.

    ``status`` is the HTTP status code when known (set for ``auth`` from a real
    401/403, for ``rate_limited`` from 429, and for ``http_error``; None for
    ``unreachable`` / ``timeout`` / no-key / ``bad_response``).
    """

    reason: UnifiErrorReason
    message: str
    status: int | None = None
