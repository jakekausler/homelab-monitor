"""AlertDispatcher: fan out alert events to all configured channels.

Per locked design decision: channels run sequentially in the order configured;
per-channel failures are caught + logged + counted but never raised. The
return value is a list of ``DeliveryResult``s in channel order so callers can
audit / surface per-channel status if needed.
"""

from __future__ import annotations

from collections import defaultdict

from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.dispatch.types import AlertEvent, Channel, DeliveryResult


class AlertDispatcher:
    """Fan-out dispatcher for alert events.

    Holds a list of channels and a per-kind failure counter. Failures are
    isolated: one bad channel never short-circuits the others.
    """

    def __init__(self, channels: list[Channel], log: BoundLogger) -> None:
        self._channels = channels
        self._log = log
        self._delivery_failures: dict[str, int] = defaultdict(int)

    async def dispatch(self, event: AlertEvent) -> list[DeliveryResult]:
        """Deliver ``event`` to every channel that accepts it; return per-channel results.

        NEVER raises: per-channel exceptions are caught, logged at WARNING
        with channel kind + error, and surfaced as ``DeliveryResult(ok=False,
        error=str(exc))``. The kind-keyed failure counter is incremented for
        each failure (used by the eventual metrics surface).

        Channels that return ``False`` from ``accepts(event)`` are skipped;
        no ``DeliveryResult`` is appended for them.

        # TODO: STOPGAP accepts() gate — retire when EPIC-012 STAGE-012-005 lands
        """
        results: list[DeliveryResult] = []
        for channel in self._channels:
            # TODO: STOPGAP — retire when EPIC-012 STAGE-012-005 lands (full routing engine)
            if not channel.accepts(event):
                self._log.debug(
                    "alert_dispatcher.channel_skipped",
                    channel_kind=channel.kind,
                )
                continue
            try:
                await channel.deliver(event)
                results.append(DeliveryResult(channel_kind=channel.kind, ok=True))
            except Exception as exc:  # -- per-channel isolation
                self._delivery_failures[channel.kind] += 1
                self._log.warning(
                    "alert_dispatcher.channel_failure",
                    channel_kind=channel.kind,
                    error=str(exc),
                )
                results.append(
                    DeliveryResult(
                        channel_kind=channel.kind,
                        ok=False,
                        error=str(exc),
                    )
                )
        return results

    @property
    def delivery_failures(self) -> dict[str, int]:
        """Read-only view of per-channel failure counters (for metrics/tests)."""
        # Cast defaultdict -> dict so callers can't accidentally seed new kinds.
        return dict(self._delivery_failures)
