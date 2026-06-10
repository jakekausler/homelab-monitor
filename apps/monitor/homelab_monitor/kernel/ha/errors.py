"""Typed HA client errors (STAGE-005-001).

Return-not-raise discriminated union mirroring
:class:`homelab_monitor.kernel.docker.registry_digest_client.FetchError`.

D-HA-CLIENT-RETURN-NOT-RAISE: every HA client method returns a success result
OR an :class:`HaError`, so an HA 5xx / timeout / auth failure never propagates
as our own 5xx. The collector converts an HaError into a failed run; the
FailureBudget handles quarantine + recovery.

SECURITY: ``message`` must NEVER contain the bearer token. Error messages are
built only from the request URL/method and the mapped reason — never from the
Authorization header or token value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

HaErrorReason = Literal["unreachable", "timeout", "auth", "http_error", "bad_response"]


@dataclass(frozen=True, slots=True)
class HaError:
    """A typed, non-raising HA client failure.

    ``reason`` discriminates the failure class:
      - ``unreachable`` — connect-level failure (httpx.ConnectError / ConnectTimeout).
      - ``timeout`` — read/overall timeout (httpx.ReadTimeout / TimeoutException).
      - ``auth`` — HTTP 401/403, OR no token configured at request time.
      - ``http_error`` — any other non-2xx status (``status`` carries the code).
      - ``bad_response`` — 2xx but the body could not be parsed as expected.

    ``status`` is the HTTP status code when known (set for ``auth`` from a real
    401/403 and for ``http_error``; None for ``unreachable`` / ``timeout`` /
    no-token / ``bad_response``).
    """

    reason: HaErrorReason
    message: str
    status: int | None = None
