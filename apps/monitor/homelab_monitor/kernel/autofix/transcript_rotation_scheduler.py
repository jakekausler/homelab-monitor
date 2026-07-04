"""Daily-at-03:00-UTC + startup-pass loop for the transcript rotator
(STAGE-009-012).

Deliberately lightweight — this is the first autofix housekeeping task in the
codebase (Decision B). STAGE-009-007's deferred startup stale-claim reaper
(TODO at ``orchestrator.py:596``) can adopt this shape when it lands.
"""

from __future__ import annotations

import asyncio
import datetime as _dt

from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.autofix.transcript_rotator import TranscriptRotator

# Daily rotation cadence: 03:00 UTC (Decision B).
_ROTATION_HOUR_UTC = 3
_ROTATION_MINUTE_UTC = 0


def _seconds_until_next_daily(now: _dt.datetime, *, hour: int, minute: int) -> float:
    """Return the number of seconds from ``now`` until the next UTC HH:MM.

    Exposed for unit testing; do not inline. ``now`` MUST be tz-aware UTC.
    """
    target_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_today <= now:
        target_today = target_today + _dt.timedelta(days=1)
    return (target_today - now).total_seconds()


async def run_transcript_rotation_loop(
    *,
    rotator: TranscriptRotator,
    log: BoundLogger,
) -> None:
    """Run one startup pass, then loop daily at 03:00 UTC.

    Never raises. On any exception from ``rotator.rotate()`` the loop logs
    and continues to the next daily wait. On ``asyncio.CancelledError`` the
    loop re-raises (cooperative cancellation on lifespan shutdown).
    """
    # 1. Startup pass — catches long-downtime accumulation.
    try:
        outcome = await rotator.rotate()
        log.info(
            "autofix.transcript_rotation.startup_pass_done",
            files_pruned=outcome.files_pruned,
            runs_marked=outcome.runs_marked,
            runbooks_scanned=outcome.runbooks_scanned,
            skipped_reason=outcome.skipped_reason,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("autofix.transcript_rotation.startup_pass_error", error=str(exc))

    # 2. Daily loop.
    while True:
        wait_seconds = _seconds_until_next_daily(
            _dt.datetime.now(_dt.UTC),
            hour=_ROTATION_HOUR_UTC,
            minute=_ROTATION_MINUTE_UTC,
        )
        try:
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            raise

        try:
            outcome = await rotator.rotate()
            log.info(
                "autofix.transcript_rotation.daily_pass_done",
                files_pruned=outcome.files_pruned,
                runs_marked=outcome.runs_marked,
                runbooks_scanned=outcome.runbooks_scanned,
                skipped_reason=outcome.skipped_reason,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("autofix.transcript_rotation.daily_pass_error", error=str(exc))


__all__ = ["_seconds_until_next_daily", "run_transcript_rotation_loop"]
