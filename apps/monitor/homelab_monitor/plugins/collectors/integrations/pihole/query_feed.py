"""STAGE-006-025: Pi-hole query-feed log shipper (Tier-3).

Pages Pi-hole's ``/api/queries`` REST endpoint and ships each DNS query as a
structured JSON line into VictoriaLogs on stream ``pihole-queries``
(``service="pihole-queries"``, ``source_type="pihole"``). Default-OFF: the
collector no-ops unless ``PiholeConfig.stream_query_feed_enabled`` is True.

PII WARNING: every shipped line is a DNS query (domain + client). The stream is
gated behind the flag and a per-UTC-day byte cap (default 500 MiB, the standard
per-stream budget). VictoriaLogs retention is GLOBAL (set elsewhere); this stream
inherits that retention — it has no independent retention knob. On cap-hit the
shipper STOPS ingesting for the day but STILL advances the dedup cursor to the
max id seen (drop, don't backlog), so it never floods VL the next day.

Dedup: a durable high-water query id is persisted in ``app_settings`` under
``pihole.query_feed.last_id``. First run (no stored key) records the current max
id as a baseline and ships NOTHING.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar, cast

from homelab_monitor.kernel.config import PiholeConfig, load_pihole_config
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.pihole.client import (
    PiholeError,
    PiholeResponse,
    PiholeRestClient,
)
from homelab_monitor.kernel.pihole.clients import classify_one
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult

# Stream identity — defined ONCE here, imported by the overview handler if needed.
PIHOLE_QUERY_FEED_STREAM = "pihole-queries"
PIHOLE_QUERY_FEED_SOURCE_TYPE = "pihole"

# Durable cursor key in app_settings.
QUERY_FEED_LAST_ID_KEY = "pihole.query_feed.last_id"

# Per-tick safety: how many /api/queries pages to walk in one run().
_PAGE_CAP = 5
# Page size requested per /api/queries call.
_PAGE_LENGTH = 1000

# Self-observability metric emitted on cap-hit (in addition to the framework's
# homelab_collector_run_* metrics, which the scheduler records automatically).
M_QUERY_FEED_CAP_HIT = "homelab_pihole_query_feed_cap_hit_total"
M_QUERY_FEED_SHIPPED = "homelab_pihole_query_feed_shipped_total"


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """A single defensively-parsed /api/queries record.

    Built from ``PiholeResponse.payload`` (typed ``object``) via :func:`parse_query`,
    which returns ``None`` for malformed records (skipped, not raised).
    """

    query_id: int
    time_epoch: float
    domain: str
    client_ip: str
    client_name: str
    status: str
    query_type: str
    reply_type: str
    reply_time: float | None
    dnssec: str
    ede_code: int | None
    ede_text: str
    upstream: str
    cname: str
    list_id: int | None


def _as_str(value: object) -> str:
    """Coerce a JSON scalar to str; non-str/None -> empty string."""
    return value if isinstance(value, str) else ""


def _as_opt_int(value: object) -> int | None:
    """Return int when value is an int (not bool), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _as_opt_float(value: object) -> float | None:
    """Return float when value is an int/float (not bool), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_dict(value: object) -> dict[str, object] | None:
    """Return value as dict[str, object] when it is a dict, else None."""
    return value if isinstance(value, dict) else None  # type: ignore[return-value]


def parse_query(record: object) -> ParsedQuery | None:
    """Defensively parse one /api/queries record. Return None when malformed.

    Malformed = not a dict, or missing/invalid ``id`` or ``time`` (the two fields
    required to dedup + timestamp). All other fields degrade to safe defaults.
    """
    rec = _as_dict(record)
    if rec is None:
        return None
    qid = _as_opt_int(rec.get("id"))
    if qid is None:
        return None
    t = _as_opt_float(rec.get("time"))
    if t is None:
        return None

    reply_d = _as_dict(rec.get("reply"))
    reply_type = ""
    reply_time: float | None = None
    if reply_d is not None:
        reply_type = _as_str(reply_d.get("type"))
        reply_time = _as_opt_float(reply_d.get("time"))

    client_d = _as_dict(rec.get("client"))
    client_ip = ""
    client_name = ""
    if client_d is not None:
        client_ip = _as_str(client_d.get("ip"))
        client_name = _as_str(client_d.get("name"))

    ede_d = _as_dict(rec.get("ede"))
    ede_code: int | None = None
    ede_text = ""
    if ede_d is not None:
        ede_code = _as_opt_int(ede_d.get("code"))
        ede_text = _as_str(ede_d.get("text"))

    return ParsedQuery(
        query_id=qid,
        time_epoch=t,
        domain=_as_str(rec.get("domain")),
        client_ip=client_ip,
        client_name=client_name,
        status=_as_str(rec.get("status")),
        query_type=_as_str(rec.get("type")),
        reply_type=reply_type,
        reply_time=reply_time,
        dnssec=_as_str(rec.get("dnssec")),
        ede_code=ede_code,
        ede_text=ede_text,
        upstream=_as_str(rec.get("upstream")),
        cname=_as_str(rec.get("cname")),
        list_id=_as_opt_int(rec.get("list_id")),
    )


def _epoch_to_iso(epoch: float) -> str:
    """Epoch seconds (float) -> ISO-8601 UTC string with 'Z'."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def build_line(parsed: ParsedQuery, *, host_lan_ip: str) -> tuple[str, str]:
    """Build the (json_line, iso_ts) for one parsed query.

    Runs the client through :func:`classify_one` (Tier-2 attribution precedent,
    STAGE-006-012): loopback clients attribute to ``host_lan_ip``. Returns the
    json.dumps'd line and the ISO-8601 UTC timestamp derived from the query time.
    """
    kind, attributed = classify_one(
        parsed.client_ip,
        parsed.client_name,
        host_lan_ip=host_lan_ip,
    )
    fields: dict[str, object] = {
        "query_id": parsed.query_id,
        "time": parsed.time_epoch,
        "domain": parsed.domain,
        "client_ip": parsed.client_ip,
        "client_name": parsed.client_name,
        "client_kind": kind,
        "status": parsed.status,
        "query_type": parsed.query_type,
        "reply_type": parsed.reply_type,
        "dnssec": parsed.dnssec,
        "upstream": parsed.upstream,
        "cname": parsed.cname,
    }
    if attributed is not None:
        fields["attributed_host"] = attributed
    if parsed.reply_time is not None:
        fields["reply_time"] = parsed.reply_time
    if parsed.ede_code is not None:
        fields["ede_code"] = parsed.ede_code
    if parsed.ede_text:
        fields["ede_text"] = parsed.ede_text
    if parsed.list_id is not None:
        fields["list_id"] = parsed.list_id
    return json.dumps(fields, sort_keys=True), _epoch_to_iso(parsed.time_epoch)


class PiholeQueryFeedCollector(BaseCollector):
    """Ship Pi-hole /api/queries records to VictoriaLogs (default-OFF)."""

    name: ClassVar[str] = "pihole_query_feed"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "pihole"

    def __init__(
        self,
        *,
        client: PiholeRestClient | None = None,
        config: PiholeConfig | None = None,
    ) -> None:
        self._client: PiholeRestClient | None = client
        self._cfg: PiholeConfig = config or load_pihole_config()
        # Daily byte-cap state (process-local; resets on UTC day change).
        self._cap_day: str = ""
        self._cap_bytes_used: int = 0

    def _reset_cap_if_new_day(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if day != self._cap_day:
            self._cap_day = day
            self._cap_bytes_used = 0

    async def run(self, ctx: CollectorContext) -> CollectorResult:  # noqa: PLR0912, PLR0915
        start = time.monotonic()

        # Branch 1: flag OFF -> no-op (FIRST thing in run()).
        if not self._cfg.stream_query_feed_enabled:
            return CollectorResult(
                ok=True,
                metrics_emitted=0,
                errors=[],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Branch 2: client unconfigured.
        if self._client is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["client_unconfigured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        self._reset_cap_if_new_day(datetime.now(tz=UTC))
        settings = AppSettingsRepository(ctx.db)

        raw_last = await settings.get(QUERY_FEED_LAST_ID_KEY)
        try:
            last_id = int(raw_last) if raw_last is not None else None
        except ValueError:
            last_id = None  # corrupt cursor -> fall back to first-run baseline

        # Page /api/queries, collecting records with id > last_id.
        collected: list[ParsedQuery] = []
        max_seen = last_id if last_id is not None else 0
        cursor: int | None = None
        page_error: str | None = None

        for _page in range(_PAGE_CAP):
            params: dict[str, str] = {"length": str(_PAGE_LENGTH)}
            if cursor is not None:
                params["cursor"] = str(cursor)
            resp = await self._client.queries(params)
            if isinstance(resp, PiholeError):
                page_error = f"{resp.reason}: {resp.message}"
                break
            records, next_cursor = _extract_records(resp)
            if not records:
                break
            stop = False
            for rec in records:
                parsed = parse_query(rec)
                if parsed is None:
                    continue  # malformed -> skip
                max_seen = max(max_seen, parsed.query_id)
                if last_id is not None and parsed.query_id > last_id:
                    collected.append(parsed)
                elif last_id is not None and parsed.query_id <= last_id:
                    # Records are id-DESC; once we hit last_id we're done.
                    stop = True
            if stop or len(records) < _PAGE_LENGTH or next_cursor is None:
                break
            cursor = next_cursor

        # Branch 3: first run (no stored cursor) -> baseline, ship NOTHING.
        if last_id is None:
            await settings.set(QUERY_FEED_LAST_ID_KEY, str(max_seen))
            return CollectorResult(
                ok=page_error is None,
                metrics_emitted=0,
                errors=[page_error] if page_error else [],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Ship oldest-first (ascending id) for a natural feed ordering.
        collected.sort(key=lambda p: p.query_id)
        shipped = 0
        cap = self._cfg.query_feed_max_bytes_per_day
        cap_hit = False
        for parsed in collected:
            line, iso_ts = build_line(parsed, host_lan_ip=self._cfg.host_lan_ip)
            line_bytes = len(line.encode("utf-8"))
            # Branch 4: cap-hit -> stop ingesting, but still advance cursor.
            if self._cap_bytes_used + line_bytes > cap:
                cap_hit = True
                break
            # STAGE-006-028: client_ip is now an INDEXED VL field going forward.
            # Old records (pre-028) carry client_ip only inside the _msg JSON and age
            # out in ~30d; exact `client_ip:"X"` LogsQL filtering covers only records
            # shipped after this stage. _enrich_client_dns KEEPS its phrase-match
            # (decoupled). TODO(STAGE-006-029-or-later): swap phrase-match for the
            # indexed filter once all in-window records are indexed.
            ctx.vl.ingest(
                stream=PIHOLE_QUERY_FEED_STREAM,
                line=line,
                ts=iso_ts,
                service=PIHOLE_QUERY_FEED_STREAM,
                source_type=PIHOLE_QUERY_FEED_SOURCE_TYPE,
                client_ip=parsed.client_ip,
            )
            self._cap_bytes_used += line_bytes
            shipped += 1

        if cap_hit:
            ctx.vm.write_counter(M_QUERY_FEED_CAP_HIT, 1.0, {})
            ctx.log.warning(
                "pihole_query_feed.cap_hit",
                day=self._cap_day,
                bytes_used=self._cap_bytes_used,
                cap=cap,
                dropped_after=shipped,
            )

        # Always advance the high-water id to max_seen (drop, don't backlog).
        if max_seen > last_id:
            await settings.set(QUERY_FEED_LAST_ID_KEY, str(max_seen))

        if shipped:
            ctx.vm.write_counter(M_QUERY_FEED_SHIPPED, float(shipped), {})

        return CollectorResult(
            ok=page_error is None,
            metrics_emitted=shipped,
            errors=[page_error] if page_error else [],
            events=[],
            duration_seconds=time.monotonic() - start,
        )


def _extract_records(resp: PiholeResponse) -> tuple[list[object], int | None]:
    """Pull (records, cursor) from a /api/queries PiholeResponse.

    ``payload`` is typed ``object``; narrow defensively. Returns ([], None) when
    the shape is unexpected.
    """
    payload = _as_dict(resp.payload)
    if payload is None:
        return ([], None)
    raw_queries = payload.get("queries")
    if isinstance(raw_queries, list):
        records: list[object] = list(cast("list[object]", raw_queries))
    else:
        records = []
    raw_cursor = payload.get("cursor")
    # bool is subclass of int; explicitly exclude bool
    cursor = (
        raw_cursor if isinstance(raw_cursor, int) and not isinstance(raw_cursor, bool) else None
    )
    return (records, cursor)
