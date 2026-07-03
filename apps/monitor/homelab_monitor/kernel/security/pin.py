"""Session-PIN primitive for confirm-on-destructive gates.

Hashing (bcrypt), rate-limit + progressive backoff, credential dispatch helper.
"""

from __future__ import annotations

import math
import re
import time
from collections import deque
from collections.abc import Callable
from enum import StrEnum
from typing import Literal, NamedTuple

import bcrypt
from fastapi import HTTPException, status

from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.auth.passwords import DEFAULT_BCRYPT_COST
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)

# Constants
PIN_REGEX_STR: str = r"^[0-9]{4,12}$"
PIN_PATTERN: re.Pattern[str] = re.compile(PIN_REGEX_STR)
PIN_HASH_KEY: Literal["security.pin_hash"] = "security.pin_hash"


class PinRateLimitConfig(NamedTuple):
    """Configuration for PIN rate limiting.

    window_seconds: sliding window duration (900.0 = 15 minutes)
    curve: tuple of (failure_count, lockout_seconds) pairs, ordered by count
    """

    window_seconds: float
    curve: tuple[tuple[int, float], ...]


# Default: 15-minute window, progressive backoff (5s @ 3 fails, 30s @ 4, 300s @ 5)
DEFAULT_PIN_RATE_LIMIT = PinRateLimitConfig(
    window_seconds=900.0,
    curve=((3, 5.0), (4, 30.0), (5, 300.0)),
)


# Types
CredentialType = Literal["pin", "phrase"]


class PhraseMatchMode(StrEnum):
    """Credential phrase matching mode for destructive actions."""

    EXACT = "exact"
    CASE_FOLD = "case_fold"


# Hash helpers (mirror kernel/auth/passwords.py)


def hash_pin(pin: str, *, cost: int | None = None) -> str:
    """Hash a PIN using bcrypt.

    Args:
        pin: PIN string (4-12 digits).
        cost: bcrypt cost factor; defaults to DEFAULT_BCRYPT_COST.

    Returns:
        Bcrypt hash string (printable).

    Raises:
        ValueError: if PIN is not 4-12 digits.
    """
    if not PIN_PATTERN.fullmatch(pin):
        raise ValueError("PIN must be 4-12 digits")
    rounds = cost if cost is not None else DEFAULT_BCRYPT_COST
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(pin.encode("utf-8"), salt).decode("utf-8")


def verify_pin(pin: str, hash_str: str) -> bool:
    """Constant-time bcrypt PIN verification.

    Returns False on any failure (no leak of cause).
    """
    try:
        return bcrypt.checkpw(pin.encode("utf-8"), hash_str.encode("utf-8"))
    except Exception:
        return False


# Rate limiter class


class InProcessPinRateLimiter:
    """Sliding-window in-process rate limiter for PIN verification.

    State: dict[int, deque[float]] keyed by user_id — each deque holds
    monotonic timestamps of FAILURES only. Successful verify CLEARS the deque.

    Progressive backoff curve: after pruning expired, if len(deque) >= curve[i][0],
    retry_after = curve[i][1] (highest matching threshold wins).

    Lost on restart (acceptable for homelab single-process).
    Test-clock injectable via the `clock` constructor argument.
    """

    def __init__(
        self,
        *,
        config: PinRateLimitConfig = DEFAULT_PIN_RATE_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._clock = clock
        self._buckets: dict[int, deque[float]] = {}

    def check(self, user_id: int) -> int | None:
        """Check if user is currently locked out.

        Lockout is TIME-based: a curve tier that fires at failure N locks the
        user out for `lockout_seconds` measured from that Nth failure's
        timestamp. Once that many seconds elapse, the lockout clears even
        though the failures themselves remain in the sliding window (they
        still count toward the next tier if another failure occurs).

        Args:
            user_id: User identifier.

        Returns:
            retry_after_seconds if currently locked, else None. Prunes expired
            entries older than window_seconds.
        """
        now = self._clock()
        bucket = self._buckets.get(user_id)
        if bucket is None:
            return None

        # Prune entries older than the sliding window.
        cutoff = now - self._config.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if not bucket:
            return None

        # Highest matching curve tier decides lockout duration; compare that
        # duration against elapsed time since the most recent failure.
        last = bucket[-1]
        for threshold, lockout_seconds in reversed(self._config.curve):
            if len(bucket) >= threshold:
                remaining = lockout_seconds - (now - last)
                if remaining > 0:
                    # Round up so we never report 0 while still locked.
                    return max(1, math.ceil(remaining))
                return None

        return None

    def record_failure(self, user_id: int) -> int | None:
        """Record a verification failure.

        Args:
            user_id: User identifier.

        Returns:
            retry_after_seconds if this failure crossed a threshold (lockout
            triggered), else None.
        """
        now = self._clock()
        bucket = self._buckets.get(user_id)
        if bucket is None:
            bucket = deque[float]()
            self._buckets[user_id] = bucket

        # Append failure
        bucket.append(now)

        # Prune expired entries
        cutoff = now - self._config.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        # Check if we just crossed a threshold
        for threshold, lockout_seconds in reversed(self._config.curve):
            if len(bucket) >= threshold:
                return int(lockout_seconds)

        return None

    def record_success(self, user_id: int) -> None:
        """Clear failure history for a user after successful verification.

        Args:
            user_id: User identifier.
        """
        if user_id in self._buckets:
            self._buckets[user_id].clear()

    def reset(self, user_id: int) -> None:
        """Alias for record_success; used by delete-PIN flow.

        Args:
            user_id: User identifier.
        """
        self.record_success(user_id)


# Credential-verification helper


async def verify_destructive_credential(  # noqa: PLR0913 -- credential dispatch helper with distinct security kwargs
    *,
    confirm_pin: str | None,
    confirm_phrase: str | None,
    expected_phrase: str,
    phrase_match_mode: PhraseMatchMode,
    user: User,
    app_settings: AppSettingsRepository,
    rate_limiter: InProcessPinRateLimiter,
) -> CredentialType:
    """Dispatch phrase vs PIN verification for destructive endpoints.

    Logic:
    1) If both confirm_pin and confirm_phrase present → 400
    2) If confirm_pin:
       a) Check rate-limiter lockout → 429 with retry_after_seconds
       b) Get PIN hash from app_settings → 400 if not configured
       c) Verify PIN; on failure, tick rate-limiter → 400
       d) On success, clear rate-limiter history → return "pin"
    3) Elif confirm_phrase:
       a) Match per phrase_match_mode (exact or case_fold)
       b) On mismatch → 400
       c) On match → return "phrase"
    4) Else → 400

    NOTE on PIN state leakage: The "PIN not configured" and "PIN incorrect"
    errors explicitly reveal that a PIN was attempted but failed. This is
    intentional: a session-authenticated caller with a valid CSRF token already
    has destructive-action power (they hold the session cookie). The PIN/phrase
    gates are designed to mitigate CSRF (cross-site framing), not session
    compromise. Leaking PIN state does not materially increase risk beyond
    session compromise, and attempting to hide it would be security theater.

    Args:
        confirm_pin: PIN string if provided, else None.
        confirm_phrase: Phrase string if provided, else None.
        expected_phrase: The phrase this endpoint requires.
        phrase_match_mode: Exact or case-fold matching.
        user: Authenticated User object.
        app_settings: AppSettingsRepository for PIN hash retrieval.
        rate_limiter: InProcessPinRateLimiter instance.

    Returns:
        "pin" if PIN was used, "phrase" if phrase was used.

    Raises:
        HTTPException with status 400, 429, or 500.
    """
    # 1) Check for both provided
    if confirm_pin is not None and confirm_phrase is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supply either confirm_pin or confirm_phrase, not both",
        )

    # 2) PIN path
    if confirm_pin is not None:
        # 2a) Check rate-limiter lockout
        retry_after = rate_limiter.check(user.id)
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "detail": f"PIN entry locked. Try again in {retry_after}s.",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        # 2b) Get PIN hash; if not configured, fall through to phrase check
        pin_hash = await app_settings.get(PIN_HASH_KEY)
        if pin_hash is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No PIN configured; supply confirm_phrase or configure a PIN.",
            )

        # 2c) Verify PIN
        if not verify_pin(confirm_pin, pin_hash):
            rate_limiter.record_failure(user.id)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="PIN incorrect.",
            )

        # 2d) Success
        rate_limiter.record_success(user.id)
        return "pin"

    # 3) Phrase path
    if confirm_phrase is not None:
        if phrase_match_mode == PhraseMatchMode.EXACT:
            matches = confirm_phrase == expected_phrase
        else:  # CASE_FOLD
            matches = confirm_phrase.strip().casefold() == expected_phrase.strip().casefold()

        if not matches:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"confirm_phrase must equal '{expected_phrase}'",
            )

        return "phrase"

    # 4) Neither provided
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="missing confirmation credential (confirm_pin or confirm_phrase required)",
    )


__all__ = [
    "DEFAULT_PIN_RATE_LIMIT",
    "PIN_HASH_KEY",
    "PIN_PATTERN",
    "PIN_REGEX_STR",
    "CredentialType",
    "InProcessPinRateLimiter",
    "PhraseMatchMode",
    "PinRateLimitConfig",
    "hash_pin",
    "verify_destructive_credential",
    "verify_pin",
]
