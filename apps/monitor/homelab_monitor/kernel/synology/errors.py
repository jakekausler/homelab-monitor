"""Typed Synology DSM client errors (STAGE-008-001).

Return-not-raise discriminated union mirroring
:class:`homelab_monitor.kernel.pihole.errors.PiholeError` and
:class:`homelab_monitor.kernel.unifi.errors.UnifiError`.

D-SYNOLOGY-CLIENT-RETURN-NOT-RAISE: every Synology client helper returns a
:class:`SynologyResponse` OR a :class:`SynologyError`, so a DSM 5xx / timeout /
auth failure / DSM application-error never propagates as our own 5xx. The
collector converts a SynologyError into a failed run; the FailureBudget handles
quarantine + recovery.

DSM returns HTTP 200 for LOGICAL failures, carrying ``{"success": false,
"error": {"code": N}}`` in the body. So ``status`` is overloaded: for the HTTP
reasons (``http_error`` / ``rate_limited`` / an HTTP-derived ``auth``) it carries
the HTTP status code; for ``api_error`` and the DSM-derived ``auth`` (codes 400 /
119-post-retry) it carries the DSM ERROR CODE, NOT an HTTP status.

SECURITY: ``message`` must NEVER contain the ``synology_dsm_password`` nor the
session id (``_sid``). Error messages are built only from the api name + method +
status / DSM-error-code — never from the ``passwd`` query value, the account, or
the SID.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SynologyErrorReason = Literal[
    "unreachable",
    "timeout",
    "auth",
    "rate_limited",
    "http_error",
    "bad_response",
    "api_error",
]


@dataclass(frozen=True, slots=True)
class SynologyError:
    """A typed, non-raising Synology DSM client failure.

    ``reason`` discriminates the failure class:
      - ``unreachable`` — connect-level failure (httpx.ConnectError / ConnectTimeout).
      - ``timeout`` — read/overall timeout (httpx.ReadTimeout / TimeoutException).
      - ``auth`` — no password configured (no network call), a login HTTP non-2xx,
        a failed login (``success:false`` / missing sid), DSM error 400 (bad
        credentials), OR DSM error 119 that persisted after one re-auth.
      - ``rate_limited`` — HTTP 429 (DSM is throttling us).
      - ``http_error`` — any other non-2xx HTTP status (``status`` carries the HTTP
        code); a DSM 5xx surfaces here, never as our own 5xx.
      - ``bad_response`` — HTTP 2xx but the body could not be parsed as JSON or did
        not have the ``{"success": ...}`` shape.
      - ``api_error`` — DSM returned ``success:false`` with a code we do NOT treat as
        auth (e.g. 105 permission-denied, 402, or any other). ``status`` carries the
        DSM error code.

    ``status`` is the HTTP status code (``http_error`` / ``rate_limited`` / an
    HTTP-derived ``auth``) OR the DSM error code (``api_error`` and the DSM-derived
    ``auth`` for codes 400 / 119); None for ``unreachable`` / ``timeout`` /
    no-password / login-shape failures / ``bad_response``.
    """

    reason: SynologyErrorReason
    message: str
    status: int | None = None
