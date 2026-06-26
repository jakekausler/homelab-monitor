"""Tests for :class:`PiholeQueryFeedCollector` (STAGE-006-025)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

import structlog

from homelab_monitor.kernel.config import PiholeConfig
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.pihole.client import PiholeError, PiholeResponse
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, MemoryRetainingMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole.query_feed import (
    _PAGE_CAP,  # pyright: ignore[reportPrivateUsage]
    M_QUERY_FEED_CAP_HIT,
    PIHOLE_QUERY_FEED_SOURCE_TYPE,
    PIHOLE_QUERY_FEED_STREAM,
    QUERY_FEED_LAST_ID_KEY,
    ParsedQuery,
    PiholeQueryFeedCollector,
    _extract_records,  # pyright: ignore[reportPrivateUsage]
    build_line,
    parse_query,
)

# Test constants for magic values (PLR2004)
_RECORD_ID_10 = 10
_RECORD_ID_9 = 9
_RECORD_ID_8 = 8
_RECORD_ID_7 = 7
_RECORD_ID_6 = 6
_RECORD_ID_5 = 5
_RECORD_ID_1 = 1
_TIME_EPOCH_1_0 = 1.0
_TIME_EPOCH_1_5 = 1.5
_TIME_EPOCH_BASELINE = 1609459200.0
_TIME_EPOCH_PLUS_ONE = 1609459201.0
_REPLY_TIME_VALUE = 0.01
_CAP_TINY = 10
_EXPECTED_METRICS_ZERO = 0
_EXPECTED_METRICS_ONE = 1
_EXPECTED_METRICS_TWO = 2
_EXPECTED_METRICS_THREE = 3
_EXPECTED_LOGS_ZERO = 0
_EXPECTED_LOGS_ONE = 1
_EXPECTED_LOGS_TWO = 2
_EXPECTED_LOGS_THREE = 3
_EXPECTED_RECORDS_ONE = 1
_EXPECTED_RECORDS_LESS_THAN_TWO = 2
_EXPECTED_CALL_COUNT_ONE = 1
_EXPECTED_CALL_COUNT_ZERO = 0


async def make_context(repo: SqliteRepository) -> CollectorContext:
    """Minimal CollectorContext for PiholeQueryFeedCollector."""
    return CollectorContext(
        config=CollectorConfig(name="pihole_query_feed"),
        db=repo,
        vm=MemoryRetainingMetricsWriter(),
        vl=InMemoryLogsWriter(),
        http=AsyncMock(),
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_query_feed"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


async def test_flag_off_noops(repo: SqliteRepository) -> None:
    """Branch 1: flag OFF -> no-op."""
    ctx = await make_context(repo)
    vl = cast(InMemoryLogsWriter, ctx.vl)
    config = PiholeConfig(stream_query_feed_enabled=False)
    collector = PiholeQueryFeedCollector(client=AsyncMock(), config=config)

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.metrics_emitted == _EXPECTED_METRICS_ZERO
    assert result.errors == []
    assert len(vl.recorded) == _EXPECTED_LOGS_ZERO


async def test_client_unconfigured(repo: SqliteRepository) -> None:
    """Branch 2: client unconfigured -> error."""
    ctx = await make_context(repo)
    config = PiholeConfig(stream_query_feed_enabled=True)
    collector = PiholeQueryFeedCollector(client=None, config=config)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["client_unconfigured"]


async def test_first_run_baseline_ships_nothing(repo: SqliteRepository) -> None:
    """Branch 3: first run (no stored cursor) -> baseline, ship NOTHING."""
    ctx = await make_context(repo)
    vl = cast(InMemoryLogsWriter, ctx.vl)
    config = PiholeConfig(stream_query_feed_enabled=True)

    client = AsyncMock()
    records = [
        {
            "id": _RECORD_ID_10,
            "time": _TIME_EPOCH_1_0,
            "domain": "a.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
        {
            "id": _RECORD_ID_9,
            "time": _TIME_EPOCH_1_0,
            "domain": "b.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
        {
            "id": _RECORD_ID_8,
            "time": _TIME_EPOCH_1_0,
            "domain": "c.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
    ]
    client.queries.return_value = PiholeResponse(
        payload={"queries": records, "cursor": None}, took_seconds=0.1, endpoint="queries"
    )
    collector = PiholeQueryFeedCollector(client=client, config=config)

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.metrics_emitted == _EXPECTED_METRICS_ZERO
    assert len(vl.recorded) == _EXPECTED_LOGS_ZERO

    # Verify cursor was stored
    settings = AppSettingsRepository(repo)
    stored = await settings.get(QUERY_FEED_LAST_ID_KEY)
    assert stored == "10"


async def test_subsequent_run_ships_records(repo: SqliteRepository) -> None:
    """Branch 4: subsequent run ships id > last_id with correct fields."""
    ctx = await make_context(repo)
    vl = cast(InMemoryLogsWriter, ctx.vl)
    config = PiholeConfig(stream_query_feed_enabled=True, host_lan_ip="192.168.2.148")

    # Seed the cursor
    settings = AppSettingsRepository(repo)
    await settings.set(QUERY_FEED_LAST_ID_KEY, "8")

    client = AsyncMock()
    records = [
        {
            "id": _RECORD_ID_10,
            "time": _TIME_EPOCH_BASELINE,  # 2021-01-01 00:00:00 UTC
            "domain": "example.com",
            "client": {"ip": "192.168.2.5", "name": "client-pc"},
            "status": "ALLOWED",
            "type": "A",
            "dnssec": "",
            "upstream": "8.8.8.8",
            "cname": "",
        },
        {
            "id": _RECORD_ID_9,
            "time": _TIME_EPOCH_PLUS_ONE,
            "domain": "test.com",
            "client": {"ip": "192.168.2.6", "name": ""},
            "status": "BLOCKED",
            "type": "A",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
    ]
    client.queries.return_value = PiholeResponse(
        payload={"queries": records, "cursor": None}, took_seconds=0.1, endpoint="queries"
    )
    collector = PiholeQueryFeedCollector(client=client, config=config)

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.metrics_emitted == _EXPECTED_METRICS_TWO
    assert len(vl.recorded) == _EXPECTED_LOGS_TWO

    # Verify records shipped in ascending ID order (9 first, then 10)
    logs = vl.recorded
    line1 = json.loads(logs[0].line)
    line2 = json.loads(logs[1].line)
    assert line1["query_id"] == _RECORD_ID_9
    assert line2["query_id"] == _RECORD_ID_10

    # Verify all ingests used the correct stream/service/source_type
    assert logs[0].stream == PIHOLE_QUERY_FEED_STREAM
    assert logs[0].service == PIHOLE_QUERY_FEED_STREAM
    assert logs[0].source_type == PIHOLE_QUERY_FEED_SOURCE_TYPE

    # Verify cursor advanced
    stored = await settings.get(QUERY_FEED_LAST_ID_KEY)
    assert stored == "10"

    # STAGE-006-028: client_ip is recorded as an indexed field on each ingest.
    assert logs[0].client_ip == "192.168.2.6"  # id 9 (client-pc absent)
    assert logs[1].client_ip == "192.168.2.5"  # id 10


async def test_cap_hit_drops_and_advances_cursor(repo: SqliteRepository) -> None:
    """Branch 5: cap-hit drops records but still advances cursor."""
    ctx = await make_context(repo)
    vl = cast(InMemoryLogsWriter, ctx.vl)
    vm = cast(MemoryRetainingMetricsWriter, ctx.vm)
    config = PiholeConfig(
        stream_query_feed_enabled=True,
        query_feed_max_bytes_per_day=_CAP_TINY,  # tiny cap
        host_lan_ip="",
    )

    # Seed the cursor
    settings = AppSettingsRepository(repo)
    await settings.set(QUERY_FEED_LAST_ID_KEY, "5")

    client = AsyncMock()
    records = [
        {
            "id": _RECORD_ID_7,
            "time": _TIME_EPOCH_1_0,
            "domain": "a.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
        {
            "id": _RECORD_ID_6,
            "time": _TIME_EPOCH_1_0,
            "domain": "b.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
    ]
    client.queries.return_value = PiholeResponse(
        payload={"queries": records, "cursor": None}, took_seconds=0.1, endpoint="queries"
    )
    collector = PiholeQueryFeedCollector(client=client, config=config)

    await collector.run(ctx)

    # Some were dropped due to cap-hit
    assert len(vl.recorded) < _EXPECTED_RECORDS_LESS_THAN_TWO

    # Cursor still advanced to max_seen
    stored = await settings.get(QUERY_FEED_LAST_ID_KEY)
    assert stored == "7"

    # Cap-hit metric emitted
    cap_hit_entries = [e for e in vm.recorded if e.name == M_QUERY_FEED_CAP_HIT]
    assert len(cap_hit_entries) > 0


async def test_malformed_record_skipped(repo: SqliteRepository) -> None:
    """Branch 6: malformed records are skipped, not fatal."""
    ctx = await make_context(repo)
    vl = cast(InMemoryLogsWriter, ctx.vl)
    config = PiholeConfig(stream_query_feed_enabled=True)

    # Seed the cursor
    settings = AppSettingsRepository(repo)
    await settings.set(QUERY_FEED_LAST_ID_KEY, "5")

    client = AsyncMock()
    records = [
        "garbage",  # not a dict
        {"id": _RECORD_ID_9, "domain": "missing-time.com"},  # missing time
        {"time": _TIME_EPOCH_1_0, "domain": "missing-id.com"},  # missing id
        {
            "id": _RECORD_ID_8,
            "time": _TIME_EPOCH_1_0,
            "domain": "valid.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
    ]
    client.queries.return_value = PiholeResponse(
        payload={"queries": records, "cursor": None}, took_seconds=0.1, endpoint="queries"
    )
    collector = PiholeQueryFeedCollector(client=client, config=config)

    result = await collector.run(ctx)

    # Only the one valid record shipped
    assert result.ok is True
    assert result.metrics_emitted == _EXPECTED_METRICS_ONE
    assert len(vl.recorded) == _EXPECTED_LOGS_ONE


async def test_classify_one_attribution_applied(repo: SqliteRepository) -> None:
    """Branch 7: classify_one attribution applied for loopback clients."""
    ctx = await make_context(repo)
    vl = cast(InMemoryLogsWriter, ctx.vl)
    config = PiholeConfig(stream_query_feed_enabled=True, host_lan_ip="192.168.2.148")

    # Seed the cursor
    settings = AppSettingsRepository(repo)
    await settings.set(QUERY_FEED_LAST_ID_KEY, "5")

    client = AsyncMock()
    records = [
        {
            "id": _RECORD_ID_7,
            "time": _TIME_EPOCH_1_0,
            "domain": "resolver-check.com",
            "client": {"ip": "127.0.0.1", "name": "pi.hole"},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
        {
            "id": _RECORD_ID_6,
            "time": _TIME_EPOCH_1_0,
            "domain": "local-check.com",
            "client": {"ip": "127.0.0.1", "name": "something"},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
        {
            "id": _RECORD_ID_8,
            "time": _TIME_EPOCH_1_0,
            "domain": "lan-check.com",
            "client": {"ip": "192.168.2.5", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
    ]
    client.queries.return_value = PiholeResponse(
        payload={"queries": records, "cursor": None}, took_seconds=0.1, endpoint="queries"
    )
    collector = PiholeQueryFeedCollector(client=client, config=config)

    result = await collector.run(ctx)

    assert result.metrics_emitted == _EXPECTED_METRICS_THREE
    logs = vl.recorded
    lines = [json.loads(log.line) for log in logs]

    # Records ship in ascending ID order (6, 7, 8)
    # ID 6: Local (127.0.0.1, not pi.hole) -> local kind + attributed_host
    assert lines[0]["query_id"] == _RECORD_ID_6
    assert lines[0]["client_kind"] == "local"
    assert lines[0]["attributed_host"] == "192.168.2.148"

    # ID 7: Resolver self (pi.hole) -> resolver_self kind + attributed_host
    assert lines[1]["query_id"] == _RECORD_ID_7
    assert lines[1]["client_kind"] == "resolver_self"
    assert lines[1]["attributed_host"] == "192.168.2.148"

    # ID 8: LAN (192.168.2.5) -> lan kind, NO attributed_host
    assert lines[2]["query_id"] == _RECORD_ID_8
    assert lines[2]["client_kind"] == "lan"
    assert "attributed_host" not in lines[2]


async def test_page_error_returned(repo: SqliteRepository) -> None:
    """Branch 8: page error returned when queries() fails."""
    ctx = await make_context(repo)
    config = PiholeConfig(stream_query_feed_enabled=True)

    # Seed the cursor
    settings = AppSettingsRepository(repo)
    await settings.set(QUERY_FEED_LAST_ID_KEY, "5")

    client = AsyncMock()
    client.queries.return_value = PiholeError(reason="unreachable", message="connection failed")
    collector = PiholeQueryFeedCollector(client=client, config=config)

    result = await collector.run(ctx)

    assert result.ok is False
    assert "unreachable" in result.errors[0]
    assert "connection failed" in result.errors[0]


async def test_paging_stops_at_page_cap(repo: SqliteRepository) -> None:
    """Branch 9: paging stops at _PAGE_CAP pages."""
    ctx = await make_context(repo)
    config = PiholeConfig(stream_query_feed_enabled=True)

    # Seed the cursor
    settings = AppSettingsRepository(repo)
    await settings.set(QUERY_FEED_LAST_ID_KEY, "5")

    client = AsyncMock()
    # Return a full page each time (would exceed cap without the limit)
    full_page = [
        {
            "id": 10 + i,
            "time": float(i),
            "domain": f"page{i}.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        }
        for i in range(1000)
    ]
    client.queries.return_value = PiholeResponse(
        payload={"queries": full_page, "cursor": 999},
        took_seconds=0.1,
        endpoint="queries",
    )
    collector = PiholeQueryFeedCollector(client=client, config=config)

    await collector.run(ctx)

    # Should stop after _PAGE_CAP pages
    assert client.queries.call_count <= _PAGE_CAP


async def test_reset_cap_if_new_day(repo: SqliteRepository) -> None:
    """Branch 10: _reset_cap_if_new_day resets on day change."""
    config = PiholeConfig(stream_query_feed_enabled=True)

    client = AsyncMock()
    client.queries.return_value = PiholeResponse(
        payload={"queries": [], "cursor": None}, took_seconds=0.1, endpoint="queries"
    )
    collector = PiholeQueryFeedCollector(client=client, config=config)

    # Call twice on the same day
    now_same = datetime.now(tz=UTC)
    collector._reset_cap_if_new_day(now_same)  # pyright: ignore[reportPrivateUsage]
    collector._cap_bytes_used = 100  # pyright: ignore[reportPrivateUsage]
    collector._reset_cap_if_new_day(now_same)  # pyright: ignore[reportPrivateUsage]
    assert collector._cap_bytes_used == 100  # noqa: PLR2004  # pyright: ignore[reportPrivateUsage]

    # Call again after day change (simulate by setting cap_day to yesterday)
    collector._cap_day = "2020-01-01"  # pyright: ignore[reportPrivateUsage]
    collector._reset_cap_if_new_day(now_same)  # pyright: ignore[reportPrivateUsage]
    assert collector._cap_bytes_used == _EXPECTED_METRICS_ZERO  # pyright: ignore[reportPrivateUsage]  # reset


def test_parse_query_valid_record() -> None:
    """parse_query with valid record returns ParsedQuery."""
    record = {
        "id": _RECORD_ID_1,
        "time": _TIME_EPOCH_1_5,
        "domain": "example.com",
        "client": {"ip": "192.168.1.1", "name": "client"},
        "status": "ALLOWED",
        "type": "A",
        "dnssec": "",
        "upstream": "8.8.8.8",
        "cname": "",
    }
    parsed = parse_query(record)
    assert parsed is not None
    assert parsed.query_id == _RECORD_ID_1
    assert parsed.time_epoch == _TIME_EPOCH_1_5
    assert parsed.domain == "example.com"


def test_parse_query_missing_id() -> None:
    """parse_query with missing id returns None."""
    record = {"time": _TIME_EPOCH_1_5, "domain": "example.com"}
    parsed = parse_query(record)
    assert parsed is None


def test_parse_query_missing_time() -> None:
    """parse_query with missing time returns None."""
    record = {"id": _RECORD_ID_1, "domain": "example.com"}
    parsed = parse_query(record)
    assert parsed is None


def test_parse_query_id_is_bool() -> None:
    """parse_query with id=bool -> _as_opt_int returns None -> malformed."""
    record = {"id": True, "time": _TIME_EPOCH_1_5, "domain": "example.com"}
    parsed = parse_query(record)
    assert parsed is None


def test_parse_query_defensive_coercion() -> None:
    """parse_query coerces non-string fields to safe defaults."""
    record: dict[str, object] = {
        "id": _RECORD_ID_1,
        "time": _TIME_EPOCH_1_5,
        "domain": 123,  # not a string -> coerced to ""
        "client": {"ip": False, "name": None},  # not strings -> coerced to ""
        "status": 456,  # not a string -> ""
        "type": [],  # not a string -> ""
        "dnssec": {},  # not a string -> ""
        "upstream": "",
        "cname": "",
        "reply": {
            "type": True,
            "time": True,
        },  # bool for time -> None (via _as_opt_float bool guard)
        "ede": {"code": False, "text": 789},  # bool->None, int->None for float coercion
    }
    parsed = parse_query(record)
    assert parsed is not None
    assert parsed.query_id == _RECORD_ID_1
    assert parsed.domain == ""
    assert parsed.client_ip == ""
    assert parsed.client_name == ""
    assert parsed.status == ""
    assert parsed.query_type == ""
    assert parsed.dnssec == ""
    assert parsed.reply_type == ""
    assert parsed.reply_time is None
    assert parsed.ede_code is None
    assert parsed.ede_text == ""


def test_build_line_includes_optional_fields() -> None:
    """build_line includes optional fields when present."""
    parsed = ParsedQuery(
        query_id=_RECORD_ID_1,
        time_epoch=_TIME_EPOCH_1_0,
        domain="example.com",
        client_ip="192.168.2.5",
        client_name="",
        status="ALLOWED",
        query_type="A",
        reply_type="NODATA",
        reply_time=_REPLY_TIME_VALUE,
        dnssec="",
        ede_code=_RECORD_ID_1,
        ede_text="test",
        upstream="8.8.8.8",
        cname="",
        list_id=_RECORD_ID_5,
    )
    line, _ts = build_line(parsed, host_lan_ip="")
    obj = json.loads(line)
    assert obj["reply_time"] == _REPLY_TIME_VALUE
    assert obj["ede_code"] == _RECORD_ID_1
    assert obj["ede_text"] == "test"
    assert obj["list_id"] == _RECORD_ID_5


def test_build_line_omits_optional_fields_when_absent() -> None:
    """build_line omits optional fields when None/empty."""
    parsed = ParsedQuery(
        query_id=_RECORD_ID_1,
        time_epoch=_TIME_EPOCH_1_0,
        domain="example.com",
        client_ip="192.168.2.5",
        client_name="",
        status="ALLOWED",
        query_type="A",
        reply_type="",
        reply_time=None,
        dnssec="",
        ede_code=None,
        ede_text="",
        upstream="8.8.8.8",
        cname="",
        list_id=None,
    )
    line, _ts = build_line(parsed, host_lan_ip="")
    obj = json.loads(line)
    assert "reply_time" not in obj
    assert "ede_code" not in obj
    assert "ede_text" not in obj
    assert "list_id" not in obj


async def test_paging_stops_short_page(repo: SqliteRepository) -> None:
    """Branch 9: paging stops when a short page is received."""
    ctx = await make_context(repo)
    config = PiholeConfig(stream_query_feed_enabled=True)

    # Seed the cursor
    settings = AppSettingsRepository(repo)
    await settings.set(QUERY_FEED_LAST_ID_KEY, "5")

    client = AsyncMock()
    # Return a short page (< _PAGE_LENGTH)
    short_page = [
        {
            "id": 10 + i,
            "time": float(i),
            "domain": f"short{i}.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        }
        for i in range(100)  # < 1000
    ]
    client.queries.return_value = PiholeResponse(
        payload={"queries": short_page, "cursor": None},
        took_seconds=0.1,
        endpoint="queries",
    )
    collector = PiholeQueryFeedCollector(client=client, config=config)

    await collector.run(ctx)

    # Should stop after short page (no cursor)
    assert client.queries.call_count == _EXPECTED_CALL_COUNT_ONE


async def test_extract_records_payload_not_dict(repo: SqliteRepository) -> None:
    """_extract_records: payload not a dict -> ([], None)."""
    resp = PiholeResponse(payload="not a dict", took_seconds=0.1, endpoint="queries")
    records, cursor = _extract_records(resp)
    assert records == []
    assert cursor is None


async def test_extract_records_queries_not_list(repo: SqliteRepository) -> None:
    """_extract_records: queries not a list -> ([], None)."""
    resp = PiholeResponse(payload={"queries": "not a list"}, took_seconds=0.1, endpoint="queries")
    records, cursor = _extract_records(resp)
    assert records == []
    assert cursor is None


async def test_extract_records_cursor_bool(repo: SqliteRepository) -> None:
    """_extract_records: cursor is bool -> treated as None (not int)."""
    resp = PiholeResponse(
        payload={"queries": [], "cursor": True},
        took_seconds=0.1,
        endpoint="queries",
    )
    records, cursor = _extract_records(resp)
    assert records == []
    assert cursor is None


async def test_paging_stops_on_empty_records(repo: SqliteRepository) -> None:
    """Branch 9: paging stops when _extract_records returns empty list."""
    ctx = await make_context(repo)
    config = PiholeConfig(stream_query_feed_enabled=True)

    # Seed the cursor
    settings = AppSettingsRepository(repo)
    await settings.set(QUERY_FEED_LAST_ID_KEY, "5")

    client = AsyncMock()
    # Return response with no queries key (malformed)
    client.queries.return_value = PiholeResponse(
        payload={},  # no queries key -> _extract_records returns ([], None)
        took_seconds=0.1,
        endpoint="queries",
    )
    collector = PiholeQueryFeedCollector(client=client, config=config)

    await collector.run(ctx)

    # Should stop after empty records
    assert client.queries.call_count == _EXPECTED_CALL_COUNT_ONE


async def test_stops_when_hitting_dedup_boundary(repo: SqliteRepository) -> None:
    """Branch 9: paging stops when a record <= last_id is encountered."""
    ctx = await make_context(repo)
    config = PiholeConfig(stream_query_feed_enabled=True)

    # Seed the cursor to 8
    settings = AppSettingsRepository(repo)
    await settings.set(QUERY_FEED_LAST_ID_KEY, "8")

    client = AsyncMock()
    # Return records in id-DESC order: 10, 9, 8 (stop at <= last_id)
    records = [
        {
            "id": _RECORD_ID_10,
            "time": _TIME_EPOCH_1_0,
            "domain": "new1.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
        {
            "id": _RECORD_ID_9,
            "time": _TIME_EPOCH_1_0,
            "domain": "new2.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
        {
            "id": _RECORD_ID_8,  # <= last_id -> should trigger stop
            "time": _TIME_EPOCH_1_0,
            "domain": "old.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
    ]
    # Return with a cursor (would cause more pages if not stopped)
    client.queries.return_value = PiholeResponse(
        payload={"queries": records, "cursor": 100},
        took_seconds=0.1,
        endpoint="queries",
    )
    collector = PiholeQueryFeedCollector(client=client, config=config)

    result = await collector.run(ctx)

    # Should stop after hitting record with id <= last_id
    assert client.queries.call_count == _EXPECTED_CALL_COUNT_ONE
    # Only 2 records shipped (9 and 10, not 8)
    assert result.metrics_emitted == _EXPECTED_METRICS_TWO


async def test_corrupt_cursor_falls_back_to_baseline(repo: SqliteRepository) -> None:
    """Corrupt cursor (non-numeric) falls back to first-run baseline and ships nothing."""
    ctx = await make_context(repo)
    vl = cast(InMemoryLogsWriter, ctx.vl)
    config = PiholeConfig(stream_query_feed_enabled=True)

    # Seed the cursor with a non-numeric value to trigger the ValueError branch
    settings = AppSettingsRepository(repo)
    await settings.set(QUERY_FEED_LAST_ID_KEY, "not-a-number")

    client = AsyncMock()
    records = [
        {
            "id": _RECORD_ID_10,
            "time": _TIME_EPOCH_1_0,
            "domain": "a.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
        {
            "id": _RECORD_ID_9,
            "time": _TIME_EPOCH_1_0,
            "domain": "b.com",
            "client": {"ip": "", "name": ""},
            "status": "",
            "type": "",
            "dnssec": "",
            "upstream": "",
            "cname": "",
        },
    ]
    client.queries.return_value = PiholeResponse(
        payload={"queries": records, "cursor": None}, took_seconds=0.1, endpoint="queries"
    )
    collector = PiholeQueryFeedCollector(client=client, config=config)

    result = await collector.run(ctx)

    # Corrupt cursor -> last_id=None -> baseline: ship nothing, record max id
    assert result.ok is True
    assert result.metrics_emitted == _EXPECTED_METRICS_ZERO
    assert len(vl.recorded) == _EXPECTED_LOGS_ZERO

    # Verify cursor was stored with max seen id
    stored = await settings.get(QUERY_FEED_LAST_ID_KEY)
    assert stored == "10"


async def test_parse_query_nested_dicts_missing(repo: SqliteRepository) -> None:
    """parse_query handles missing nested dicts gracefully."""
    record = {
        "id": _RECORD_ID_1,
        "time": _TIME_EPOCH_1_0,
        "domain": "test.com",
        # client, reply, ede dicts all absent
    }
    parsed = parse_query(record)
    assert parsed is not None
    assert parsed.client_ip == ""
    assert parsed.reply_type == ""
    assert parsed.ede_code is None
