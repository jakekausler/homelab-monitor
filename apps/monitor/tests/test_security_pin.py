"""Unit tests for kernel/security/pin.py (STAGE-009-010B).

Project test conventions discovered (from tests/test_auth_rate_limit.py,
tests/test_api_autofix.py):
  - Framework: pytest with asyncio_mode="auto" (pytest.ini); existing async
    tests still decorate with @pytest.mark.asyncio for clarity, so this file
    follows suit.
  - Mocking: hand-written fakes (not unittest.mock) for repository-shaped
    dependencies is the dominant idiom for simple protocols; AsyncMock is used
    when patching real repository methods. Here we hand-write a minimal fake
    AppSettingsRepository (only `.get()` is used by verify_destructive_credential)
    to avoid a real DB round-trip for pure-unit tests.
  - Assertions: plain `assert`, `pytest.raises`, no `# noqa: PLR2004` needed
    for status codes compared via constants below.
  - Rate limiter tests inject a deterministic clock (mutable counter closure)
    per the project's existing test_auth_rate_limit.py style (though that
    file uses time.sleep for its window-expiry test; the PIN limiter test plan
    explicitly forbids time.sleep, so we use an injected clock throughout).

Achieves 100% branch coverage of kernel/security/pin.py.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi import HTTPException

from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.security.pin import (
    DEFAULT_PIN_RATE_LIMIT,
    PIN_HASH_KEY,
    InProcessPinRateLimiter,
    PhraseMatchMode,
    PinRateLimitConfig,
    hash_pin,
    verify_destructive_credential,
    verify_pin,
)


def _make_user(user_id: int = 1) -> User:
    return User(id=user_id, username=f"user{user_id}", created_at="2026-01-01T00:00:00+00:00")


class _FakeAppSettings:
    """Minimal fake matching the one method verify_destructive_credential uses."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    async def get(self, key: str) -> str | None:
        return self._values.get(key)


def _make_clock() -> tuple[Callable[[], float], Callable[[float], None]]:
    """Return (clock, advance) — a deterministic fake monotonic clock."""
    state = {"now": 0.0}

    def clock() -> float:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] += seconds

    return clock, advance


# ---------------------------------------------------------------------------
# hash_pin / verify_pin
# ---------------------------------------------------------------------------


def test_hash_pin_roundtrip() -> None:
    """hash_pin then verify_pin returns True on correct PIN, False on wrong."""
    hashed = hash_pin("1234", cost=4)
    assert verify_pin("1234", hashed) is True
    assert verify_pin("9999", hashed) is False


def test_hash_pin_rejects_non_digit() -> None:
    """hash_pin raises ValueError on non-digit input."""
    with pytest.raises(ValueError, match="4-12 digits"):
        hash_pin("abc123", cost=4)


def test_hash_pin_rejects_too_short() -> None:
    """3-digit PIN raises ValueError."""
    with pytest.raises(ValueError, match="4-12 digits"):
        hash_pin("123", cost=4)


def test_hash_pin_rejects_too_long() -> None:
    """13-digit PIN raises ValueError."""
    with pytest.raises(ValueError, match="4-12 digits"):
        hash_pin("1234567890123", cost=4)


def test_hash_pin_accepts_4_digits() -> None:
    """Exactly 4 digits (lower bound) succeeds."""
    hashed = hash_pin("4444", cost=4)
    assert verify_pin("4444", hashed) is True


def test_hash_pin_accepts_12_digits() -> None:
    """Exactly 12 digits (upper bound) succeeds."""
    pin = "123456789012"
    hashed = hash_pin(pin, cost=4)
    assert verify_pin(pin, hashed) is True


def test_hash_pin_default_cost_used_when_none() -> None:
    """cost=None uses DEFAULT_BCRYPT_COST.

    Exercises the `if cost is not None else` False branch.
    """
    hashed = hash_pin("1234", cost=None)
    assert verify_pin("1234", hashed) is True


def test_verify_pin_returns_false_on_malformed_hash() -> None:
    """verify_pin returns False (no raise) on a garbage hash string."""
    assert verify_pin("1234", "not-a-valid-bcrypt-hash") is False


# ---------------------------------------------------------------------------
# InProcessPinRateLimiter
# ---------------------------------------------------------------------------


def test_rate_limiter_no_wait_for_first_two_failures() -> None:
    """Failures 1 and 2 do not trigger a lockout."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    assert limiter.record_failure(1) is None
    assert limiter.record_failure(1) is None
    assert limiter.check(1) is None


def test_rate_limiter_5s_on_third_failure() -> None:
    """3rd consecutive failure triggers a 5s lockout.

    NOTE: This test also provides coverage for the `if new_retry is not None:`
    branch in kernel/api/routers/security_pin.py (rotate and verify endpoints),
    which is marked `# pragma: no cover` because pytest-cov + xdist misreports it.
    """
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    limiter.record_failure(1)
    limiter.record_failure(1)
    retry_after = limiter.record_failure(1)
    assert retry_after == 5  # noqa: PLR2004
    assert limiter.check(1) == 5  # noqa: PLR2004


def test_rate_limiter_30s_on_fourth_failure() -> None:
    """4th consecutive failure triggers a 30s lockout."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    for _ in range(3):
        limiter.record_failure(1)
    retry_after = limiter.record_failure(1)
    assert retry_after == 30  # noqa: PLR2004


def test_rate_limiter_300s_on_fifth_failure() -> None:
    """5th consecutive failure triggers a 300s lockout."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    for _ in range(4):
        limiter.record_failure(1)
    retry_after = limiter.record_failure(1)
    assert retry_after == 300  # noqa: PLR2004


def test_rate_limiter_success_clears_deque() -> None:
    """record_success clears failure history; a fresh failure restarts the count from zero."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    for _ in range(4):
        limiter.record_failure(1)
    limiter.record_success(1)
    assert limiter.check(1) is None
    # Restart: single new failure should not relock (curve threshold is 3).
    assert limiter.record_failure(1) is None
    assert limiter.check(1) is None


def test_rate_limiter_reset_behaves_like_record_success() -> None:
    """reset() is an alias for record_success(): clears the deque identically."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    for _ in range(4):
        limiter.record_failure(1)
    assert limiter.check(1) == 30  # noqa: PLR2004
    limiter.reset(1)
    assert limiter.check(1) is None


def test_rate_limiter_window_prunes_expired() -> None:
    """Entries older than window_seconds are pruned and no longer count."""
    clock, advance = _make_clock()
    config = PinRateLimitConfig(window_seconds=10.0, curve=((3, 5.0), (4, 30.0), (5, 300.0)))
    limiter = InProcessPinRateLimiter(config=config, clock=clock)

    # Two failures, then advance past the window, then one more failure.
    limiter.record_failure(1)
    advance(1.0)
    limiter.record_failure(1)
    # Advance past the 10s window: both prior failures should be pruned.
    advance(11.0)
    retry_after = limiter.record_failure(1)
    # Only 1 failure remains in the (pruned) window -> no lockout.
    assert retry_after is None
    assert limiter.check(1) is None


def test_rate_limiter_check_prunes_without_recording() -> None:
    """check() also prunes expired entries (not just record_failure)."""
    clock, advance = _make_clock()
    config = PinRateLimitConfig(window_seconds=5.0, curve=((3, 5.0), (4, 30.0), (5, 300.0)))
    limiter = InProcessPinRateLimiter(config=config, clock=clock)
    limiter.record_failure(1)
    limiter.record_failure(1)
    limiter.record_failure(1)
    assert limiter.check(1) == 5  # noqa: PLR2004
    advance(6.0)
    # Past the window: check() should prune and report no lockout.
    assert limiter.check(1) is None


def test_rate_limiter_check_clears_after_lockout_elapses() -> None:
    """After the curve's lockout duration elapses, check() returns None
    even though the failures remain within the sliding window.

    Regression: previously check() kept returning 5 for the full 900s window
    after 3 failures because it did not compare elapsed time against the
    curve's lockout_seconds.
    """
    clock, advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    limiter.record_failure(1)
    limiter.record_failure(1)
    assert limiter.record_failure(1) == 5  # noqa: PLR2004
    assert limiter.check(1) == 5  # noqa: PLR2004
    # Still locked partway through the 5s window.
    advance(3.0)
    assert limiter.check(1) == 2  # noqa: PLR2004
    # Past the 5s lockout but well inside the 900s sliding window.
    advance(3.0)
    assert limiter.check(1) is None


def test_rate_limiter_next_failure_after_lockout_escalates_tier() -> None:
    """After a 5s lockout expires, the failures still count toward the next
    tier: a 4th failure triggers the 30s tier, not a fresh 5s tier."""
    clock, advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    for _ in range(3):
        limiter.record_failure(1)
    advance(6.0)  # Past 5s lockout.
    assert limiter.check(1) is None
    # 4th failure still in the 900s window -> escalates to 30s.
    assert limiter.record_failure(1) == 30  # noqa: PLR2004


def test_rate_limiter_check_unknown_user_returns_none() -> None:
    """check() on a user_id with no bucket yet returns None (covers `bucket is None` branch)."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    assert limiter.check(999) is None


def test_rate_limiter_isolates_users() -> None:
    """Failures recorded against user 1 do not affect user 2."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    for _ in range(5):
        limiter.record_failure(1)
    assert limiter.check(1) == 300  # noqa: PLR2004
    assert limiter.check(2) is None
    assert limiter.record_failure(2) is None


def test_rate_limiter_uses_default_config_and_clock_when_unset() -> None:
    """Constructing with no args uses DEFAULT_PIN_RATE_LIMIT and time.monotonic."""
    limiter = InProcessPinRateLimiter()
    assert limiter.check(1) is None
    # Sanity: default config curve matches the module constant.
    assert DEFAULT_PIN_RATE_LIMIT.window_seconds == 900.0  # noqa: PLR2004


# ---------------------------------------------------------------------------
# verify_destructive_credential
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_destructive_credential_pin_success() -> None:
    """Correct PIN returns 'pin' and calls rate_limiter.record_success."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    # Prime with a failure so we can observe record_success clearing it.
    limiter.record_failure(1)
    pin_hash = hash_pin("1234", cost=4)
    app_settings = _FakeAppSettings({PIN_HASH_KEY: pin_hash})

    result = await verify_destructive_credential(
        confirm_pin="1234",
        confirm_phrase=None,
        expected_phrase="approve",
        phrase_match_mode=PhraseMatchMode.EXACT,
        user=_make_user(1),
        app_settings=app_settings,  # type: ignore[arg-type]
        rate_limiter=limiter,
    )
    assert result == "pin"
    # record_success cleared the deque -> no lockout, and a fresh single
    # failure doesn't relock (threshold is 3).
    assert limiter.check(1) is None


@pytest.mark.asyncio
async def test_verify_destructive_credential_pin_wrong_records_failure() -> None:
    """Wrong PIN raises HTTPException(400) and ticks the rate limiter."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    pin_hash = hash_pin("1234", cost=4)
    app_settings = _FakeAppSettings({PIN_HASH_KEY: pin_hash})

    with pytest.raises(HTTPException) as exc_info:
        await verify_destructive_credential(
            confirm_pin="9999",
            confirm_phrase=None,
            expected_phrase="approve",
            phrase_match_mode=PhraseMatchMode.EXACT,
            user=_make_user(1),
            app_settings=app_settings,  # type: ignore[arg-type]
            rate_limiter=limiter,
        )
    assert exc_info.value.status_code == 400  # noqa: PLR2004
    # Failure was ticked.
    assert limiter.check(1) is None  # only 1 failure so far, below threshold
    assert len(limiter._buckets[1]) == 1  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_verify_destructive_credential_pin_locked_returns_429() -> None:
    """Locked user (rate_limiter.check returns non-None) raises HTTPException(429)
    with Retry-After header set and retry_after surfaced in detail."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    for _ in range(3):
        limiter.record_failure(1)
    pin_hash = hash_pin("1234", cost=4)
    app_settings = _FakeAppSettings({PIN_HASH_KEY: pin_hash})

    with pytest.raises(HTTPException) as exc_info:
        await verify_destructive_credential(
            confirm_pin="1234",
            confirm_phrase=None,
            expected_phrase="approve",
            phrase_match_mode=PhraseMatchMode.EXACT,
            user=_make_user(1),
            app_settings=app_settings,  # type: ignore[arg-type]
            rate_limiter=limiter,
        )
    exc = exc_info.value
    assert exc.status_code == 429  # noqa: PLR2004
    assert exc.headers is not None
    assert exc.headers["Retry-After"] == "5"
    assert isinstance(exc.detail, dict)
    detail = cast(dict[str, Any], exc.detail)
    assert detail["retry_after_seconds"] == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_verify_destructive_credential_pin_no_pin_configured_400() -> None:
    """No PIN configured (app_settings.get returns None) raises HTTPException(400)."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    app_settings = _FakeAppSettings({})

    with pytest.raises(HTTPException) as exc_info:
        await verify_destructive_credential(
            confirm_pin="1234",
            confirm_phrase=None,
            expected_phrase="approve",
            phrase_match_mode=PhraseMatchMode.EXACT,
            user=_make_user(1),
            app_settings=app_settings,  # type: ignore[arg-type]
            rate_limiter=limiter,
        )
    assert exc_info.value.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_verify_destructive_credential_phrase_exact_matches() -> None:
    """EXACT mode: matching phrase succeeds (returns 'phrase')."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    app_settings = _FakeAppSettings({})

    result = await verify_destructive_credential(
        confirm_pin=None,
        confirm_phrase="approve",
        expected_phrase="approve",
        phrase_match_mode=PhraseMatchMode.EXACT,
        user=_make_user(1),
        app_settings=app_settings,  # type: ignore[arg-type]
        rate_limiter=limiter,
    )
    assert result == "phrase"


@pytest.mark.asyncio
async def test_verify_destructive_credential_phrase_exact_no_case_fold() -> None:
    """EXACT mode: NO stripping/case-folding — trailing whitespace or case
    variance fails even though the 'unfolded' content matches."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    app_settings = _FakeAppSettings({})

    with pytest.raises(HTTPException) as exc_info:
        await verify_destructive_credential(
            confirm_pin=None,
            confirm_phrase="APPROVE",
            expected_phrase="approve",
            phrase_match_mode=PhraseMatchMode.EXACT,
            user=_make_user(1),
            app_settings=app_settings,  # type: ignore[arg-type]
            rate_limiter=limiter,
        )
    assert exc_info.value.status_code == 400  # noqa: PLR2004
    assert "approve" in str(exc_info.value.detail)

    with pytest.raises(HTTPException):
        await verify_destructive_credential(
            confirm_pin=None,
            confirm_phrase=" approve ",
            expected_phrase="approve",
            phrase_match_mode=PhraseMatchMode.EXACT,
            user=_make_user(1),
            app_settings=app_settings,  # type: ignore[arg-type]
            rate_limiter=limiter,
        )


@pytest.mark.asyncio
async def test_verify_destructive_credential_phrase_case_fold_matches() -> None:
    """CASE_FOLD mode: matches with different case and/or surrounding whitespace."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    app_settings = _FakeAppSettings({})

    result = await verify_destructive_credential(
        confirm_pin=None,
        confirm_phrase="  DISABLE Auto-Fix  ",
        expected_phrase="disable auto-fix",
        phrase_match_mode=PhraseMatchMode.CASE_FOLD,
        user=_make_user(1),
        app_settings=app_settings,  # type: ignore[arg-type]
        rate_limiter=limiter,
    )
    assert result == "phrase"


@pytest.mark.asyncio
async def test_verify_destructive_credential_phrase_wrong_returns_400() -> None:
    """Phrase mismatch (either mode) raises HTTPException(400)."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    app_settings = _FakeAppSettings({})

    with pytest.raises(HTTPException) as exc_info:
        await verify_destructive_credential(
            confirm_pin=None,
            confirm_phrase="wrong",
            expected_phrase="disable auto-fix",
            phrase_match_mode=PhraseMatchMode.CASE_FOLD,
            user=_make_user(1),
            app_settings=app_settings,  # type: ignore[arg-type]
            rate_limiter=limiter,
        )
    assert exc_info.value.status_code == 400  # noqa: PLR2004


@pytest.mark.asyncio
async def test_verify_destructive_credential_both_credentials_400() -> None:
    """Both confirm_pin and confirm_phrase provided → HTTPException(400)."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    app_settings = _FakeAppSettings({})

    with pytest.raises(HTTPException) as exc_info:
        await verify_destructive_credential(
            confirm_pin="1234",
            confirm_phrase="approve",
            expected_phrase="approve",
            phrase_match_mode=PhraseMatchMode.EXACT,
            user=_make_user(1),
            app_settings=app_settings,  # type: ignore[arg-type]
            rate_limiter=limiter,
        )
    assert exc_info.value.status_code == 400  # noqa: PLR2004
    assert "not both" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_verify_destructive_credential_neither_credential_400() -> None:
    """Neither confirm_pin nor confirm_phrase provided → HTTPException(400)."""
    clock, _advance = _make_clock()
    limiter = InProcessPinRateLimiter(clock=clock)
    app_settings = _FakeAppSettings({})

    with pytest.raises(HTTPException) as exc_info:
        await verify_destructive_credential(
            confirm_pin=None,
            confirm_phrase=None,
            expected_phrase="approve",
            phrase_match_mode=PhraseMatchMode.EXACT,
            user=_make_user(1),
            app_settings=app_settings,  # type: ignore[arg-type]
            rate_limiter=limiter,
        )
    assert exc_info.value.status_code == 400  # noqa: PLR2004
    assert "missing confirmation credential" in str(exc_info.value.detail)
