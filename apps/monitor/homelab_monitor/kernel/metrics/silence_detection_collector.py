"""SilenceDetectionCollector (STAGE-004-038).

Anomaly Type D — "signature went silent". A pure DB reader: each tick it scans
log_signatures cross-checked against log_signature_silence_allowlist and emits
homelab_log_signature_silent{service_key, template_hash}=1 for every signature that
is (silent within the alertable window: silent_min..silent_max since last_seen_at)
AND (not suppressed) AND (NOT covered by an active expected-silence allowlist entry).
A DUMB vmalert-metrics rule (deploy/vmalert/metrics/signature_silent.yaml) fires on
homelab_log_signature_silent == 1.

Self-resolution: replace_family every tick. A signature that recovers (last_seen_at
refreshes), ages past silent_max, gets suppressed, or enters an allow-window simply
stops being emitted -> series disappears -> alert resolves (035 parity). The
allowlist decision is FOLDED into this gauge (D-FOLD-ALLOWLIST-INTO-SILENT-GAUGE):
no separate silence_allowed series.

Self-metric: homelab_collector_run_silence_detection{phase, result}.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final

from sqlalchemy import Row, text

from homelab_monitor.kernel.config import SilenceDetectionConfig
from homelab_monitor.kernel.logs.silence_schedule import is_silence_allowed
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult, RunKind, TrustLevel

_METRIC_SILENT: Final[str] = "homelab_log_signature_silent"
_SELF_METRIC: Final[str] = "homelab_collector_run_silence_detection"
_DEFAULT_INTERVAL_SECONDS: Final[int] = 60
_DEFAULT_TIMEOUT_SECONDS: Final[int] = 20


def _now_ms() -> int:
    """Current unix time in milliseconds (matches log_signatures.last_seen_at units)."""
    return int(time.time() * 1000)


class _AllowEntry:
    """Lightweight typed view over one allowlist row (avoids Row attr-access noise)."""

    __slots__ = ("expires_at", "schedule_kind", "schedule_value", "service_key", "template_hash")

    def __init__(
        self,
        *,
        template_hash: str | None,
        service_key: str,
        schedule_kind: str,
        schedule_value: str,
        expires_at: str | None,
    ) -> None:
        self.template_hash = template_hash
        self.service_key = service_key
        self.schedule_kind = schedule_kind
        self.schedule_value = schedule_value
        self.expires_at = expires_at


class SilenceDetectionCollector(BaseCollector):
    """Emit homelab_log_signature_silent for alertable-silent, unsuppressed, un-allowed sigs."""

    name: ClassVar[str] = "silence_detection"
    interval: ClassVar[timedelta] = timedelta(seconds=_DEFAULT_INTERVAL_SECONDS)
    timeout: ClassVar[timedelta] = timedelta(seconds=_DEFAULT_TIMEOUT_SECONDS)
    concurrency_group: ClassVar[str] = "silence_detection"
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    trust_level: ClassVar[TrustLevel] = TrustLevel.BUILTIN

    def __init__(self, *, config: SilenceDetectionConfig | None = None) -> None:
        self._config: SilenceDetectionConfig | None = config

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        start = time.monotonic()

        if self._config is None:
            ctx.log.error("silence_detection_collector.dependencies_unwired")
            self._emit_self_metric(ctx, phase="tick", result="dependencies_unwired")
            return CollectorResult(
                ok=False,
                metrics_emitted=1,
                errors=["dependencies_unwired"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        now_ms = _now_ms()
        now_dt = datetime.now(UTC)
        min_ms = self._config.silent_min_seconds * 1000
        max_ms = self._config.silent_max_seconds * 1000

        try:
            sig_rows = await ctx.db.fetch_all(
                text("SELECT service_key, template_hash, status, last_seen_at FROM log_signatures")
            )
            allow_rows = await ctx.db.fetch_all(
                text(
                    "SELECT template_hash, service_key, schedule_kind, schedule_value, expires_at "
                    "FROM log_signature_silence_allowlist"
                )
            )
        except Exception as exc:
            ctx.log.warning("silence_detection_collector.query_failed", error=str(exc))
            self._emit_self_metric(ctx, phase="tick", result="error")
            return CollectorResult(
                ok=False,
                metrics_emitted=1,
                errors=[f"query_failed: {exc}"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        allow_entries = self._build_allow_entries(allow_rows, now_dt)

        entries: list[tuple[float, dict[str, str]]] = []
        for row in sig_rows:
            status = str(row.status)  # pyright: ignore[reportAttributeAccessIssue]
            if status == "suppressed":
                continue
            last_seen_at = int(row.last_seen_at)  # pyright: ignore[reportAttributeAccessIssue]
            silent_for_ms = now_ms - last_seen_at
            if not (min_ms <= silent_for_ms <= max_ms):
                continue
            service_key = str(row.service_key)  # pyright: ignore[reportAttributeAccessIssue]
            template_hash = str(row.template_hash)  # pyright: ignore[reportAttributeAccessIssue]
            if self._allow_covered(allow_entries, service_key, template_hash, now_dt):
                continue
            entries.append((1.0, {"service_key": service_key, "template_hash": template_hash}))

        replacer = getattr(ctx.vm, "replace_family", None)
        if callable(replacer):
            replacer(_METRIC_SILENT, entries)

        self._emit_self_metric(ctx, phase="tick", result="ok")
        return CollectorResult(
            ok=True,
            metrics_emitted=len(entries) + 1,
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    def _build_allow_entries(
        self,
        allow_rows: Sequence[Row[Any]],
        now_dt: datetime,
    ) -> list[_AllowEntry]:
        """Convert non-expired allowlist rows into _AllowEntry views."""
        out: list[_AllowEntry] = []
        for r in allow_rows:
            raw_exp = r.expires_at  # pyright: ignore[reportAttributeAccessIssue]
            expires_at = None if raw_exp is None else str(raw_exp)
            if expires_at is not None and self._is_expired(expires_at, now_dt):
                continue
            raw_hash = r.template_hash  # pyright: ignore[reportAttributeAccessIssue]
            out.append(
                _AllowEntry(
                    template_hash=(None if raw_hash is None else str(raw_hash)),
                    service_key=str(r.service_key),  # pyright: ignore[reportAttributeAccessIssue]
                    schedule_kind=str(r.schedule_kind),  # pyright: ignore[reportAttributeAccessIssue]
                    schedule_value=str(r.schedule_value),  # pyright: ignore[reportAttributeAccessIssue]
                    expires_at=expires_at,
                )
            )
        return out

    @staticmethod
    def _is_expired(expires_at: str, now_dt: datetime) -> bool:
        """True when expires_at parses and is strictly before now. Unparseable -> not expired."""
        try:
            exp = datetime.fromisoformat(expires_at)
        except ValueError:
            return (
                False  # defensive: keep a malformed-expiry entry rather than silently expiring it
            )
        exp = exp.replace(tzinfo=UTC) if exp.tzinfo is None else exp.astimezone(UTC)
        return exp < now_dt

    def _allow_covered(
        self,
        allow_entries: list[_AllowEntry],
        service_key: str,
        template_hash: str,
        now_dt: datetime,
    ) -> bool:
        """True if ANY active entry (hash-specific FIRST, then service-wide) currently allows."""
        assert self._config is not None  # narrowed in run() before this is reachable
        grace = self._config.cron_grace_seconds
        # Hash-specific entries first, then service-wide (template_hash is None).
        ordered = [
            e
            for e in allow_entries
            if e.service_key == service_key and e.template_hash == template_hash
        ] + [e for e in allow_entries if e.service_key == service_key and e.template_hash is None]
        for e in ordered:
            try:
                if is_silence_allowed(
                    e.schedule_kind, e.schedule_value, now_dt, cron_grace_seconds=grace
                ):
                    return True
            except ValueError:
                # Malformed entry -> treat as non-matching; do not crash the tick.
                continue
        return False

    @staticmethod
    def _emit_self_metric(ctx: CollectorContext, *, phase: str, result: str) -> None:
        ctx.vm.write_gauge(_SELF_METRIC, 1.0, {"phase": phase, "result": result})


__all__ = ["SilenceDetectionCollector"]
