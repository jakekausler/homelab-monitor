"""Unit tests for _enrich_client_dns and _pihole_queries_expr (STAGE-006-027).

Tests every branch of the helper using a _FakeVl in-process client, avoiding
the overhead of a full HTTP stack. The endpoint integration tests in
test_api_integrations_unifi.py prove the wiring.
"""

# pyright: reportPrivateUsage=false, reportArgumentType=false

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from homelab_monitor.kernel.api.routers.integrations_unifi import (
    _DNS_TOP_DOMAINS_CAP,
    _enrich_client_dns,
    _pihole_queries_expr,
)
from homelab_monitor.kernel.db.repositories.unifi_clients_repository import UnifiIpSpan
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClientError,
    VlLogLine,
    VlQueryResult,
)

# ── constants ────────────────────────────────────────────────────────────────

_IP = "192.168.2.50"
_HOST_LAN_IP = "192.168.2.148"
_SINCE = "2024-05-04T00:00:00Z"
_NOW = "2024-05-05T00:00:00Z"
_BLOCKED_COUNT_2 = 2
_TOP_DOMAINS_GENERATE = 15


# ── fake VL client ────────────────────────────────────────────────────────────


class _FakeVl:
    """Minimal VL client double for unit tests.

    by_expr: dict mapping expr string -> list[str] of _msg payloads to return.
    If by_expr is an Exception instance, query() raises it.
    """

    def __init__(self, by_expr: dict[str, list[str]] | Exception) -> None:
        self._by_expr = by_expr

    async def query(self, *, expr: str, start: str, end: str) -> VlQueryResult:
        if isinstance(self._by_expr, Exception):
            raise self._by_expr
        msgs = self._by_expr.get(expr, [])
        lines = [VlLogLine(timestamp="", message=m, stream="", fields={}) for m in msgs]
        return VlQueryResult(lines=lines, truncated=False)


def _msg(client_ip: str, domain: str, status: str, time: float) -> str:
    """JSON-encoded pihole-queries record."""
    return json.dumps({"client_ip": client_ip, "domain": domain, "status": status, "time": time})


def _span(ip: str, first_seen: str = _SINCE, last_seen: str = _NOW) -> UnifiIpSpan:
    return UnifiIpSpan(ip=ip, first_seen=first_seen, last_seen=last_seen)


# ── _pihole_queries_expr ─────────────────────────────────────────────────────


def test_pihole_queries_expr_exact_string() -> None:
    """_pihole_queries_expr produces the expected LogsQL stream-selector + phrase-match."""
    expr = _pihole_queries_expr("1.2.3.4")
    assert expr == 'service:"pihole-queries" AND source_type:"pihole" AND "1.2.3.4"'


# ── _enrich_client_dns ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_no_windows_returns_none() -> None:
    """No spans, no current_ip, not host → windows empty → None."""
    result = await _enrich_client_dns(
        is_host=False,
        current_ip=None,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=_FakeVl({}),
    )
    assert result is None


@pytest.mark.asyncio
async def test_enrich_current_ip_fallback_adds_window() -> None:
    """With no spans but a current_ip, current_ip window is used → populated."""
    expr = _pihole_queries_expr(_IP)
    vl = _FakeVl({expr: [_msg(_IP, "example.com", "FORWARDED", 1714867200.0)]})

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is not None
    assert "example.com" in result.top_domains
    assert result.blocked_count == 0
    assert result.last_query_at is not None


@pytest.mark.asyncio
async def test_enrich_host_lan_ip_window_added() -> None:
    """is_host=True + host_lan_ip → host-LAN-IP window is added → populated."""
    expr = _pihole_queries_expr(_HOST_LAN_IP)
    vl = _FakeVl({expr: [_msg(_HOST_LAN_IP, "host.domain", "FORWARDED", 1714867200.0)]})

    result = await _enrich_client_dns(
        is_host=True,
        current_ip=None,
        ip_spans=[],
        host_lan_ip=_HOST_LAN_IP,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is not None
    assert "host.domain" in result.top_domains


@pytest.mark.asyncio
async def test_enrich_spans_create_windows() -> None:
    """IP spans are used as (first_seen, last_seen) windows."""
    span_first = "2024-05-04T10:00:00Z"
    span_last = "2024-05-04T12:00:00Z"
    span = _span(_IP, first_seen=span_first, last_seen=span_last)
    expr = _pihole_queries_expr(_IP)
    vl = _FakeVl({expr: [_msg(_IP, "span.domain", "FORWARDED", 1714867200.0)]})

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=None,
        ip_spans=[span],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is not None
    assert "span.domain" in result.top_domains


@pytest.mark.asyncio
async def test_enrich_blocked_status_counted() -> None:
    """Records with blocked status are counted; non-blocked are not."""
    expr = _pihole_queries_expr(_IP)
    vl = _FakeVl(
        {
            expr: [
                _msg(_IP, "ads.example", "GRAVITY", 1714867200.0),
                _msg(_IP, "ads.example", "GRAVITY", 1714867201.0),
                _msg(_IP, "safe.example", "FORWARDED", 1714867202.0),
            ]
        }
    )

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is not None
    assert result.blocked_count == _BLOCKED_COUNT_2
    assert result.top_domains[0] == "ads.example"  # higher frequency first
    assert "safe.example" in result.top_domains


@pytest.mark.asyncio
async def test_enrich_regex_status_counted() -> None:
    """A REGEX-status record (FTL v6 regex-denylist hit) counts as blocked."""
    expr = _pihole_queries_expr(_IP)
    vl = _FakeVl(
        {
            expr: [
                _msg(_IP, "tracker.example", "REGEX", 1714867200.0),
                _msg(_IP, "safe.example", "FORWARDED", 1714867201.0),
            ]
        }
    )

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is not None
    assert result.blocked_count == 1


@pytest.mark.asyncio
async def test_enrich_time_max_selection() -> None:
    """last_query_at reflects the max epoch across all records."""
    expr = _pihole_queries_expr(_IP)
    epoch_max = 1714867300.0
    vl = _FakeVl(
        {
            expr: [
                _msg(_IP, "a.com", "FORWARDED", 1714867200.0),
                _msg(_IP, "b.com", "FORWARDED", epoch_max),
                _msg(_IP, "c.com", "FORWARDED", 1714867250.0),
            ]
        }
    )

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is not None
    assert result.last_query_at is not None
    # The ISO timestamp must end with Z.
    assert result.last_query_at.endswith("Z")
    # The timestamp should correspond to epoch_max.
    expected_iso = datetime.fromtimestamp(epoch_max, tz=UTC).isoformat().replace("+00:00", "Z")
    assert result.last_query_at == expected_iso


@pytest.mark.asyncio
async def test_enrich_vl_error_returns_none() -> None:
    """VictoriaLogsClientError on the only IP → saw_any stays False → None."""
    vl = _FakeVl(VictoriaLogsClientError("vl down"))

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is None


@pytest.mark.asyncio
async def test_enrich_vl_error_on_one_ip_continues_to_others() -> None:
    """VL error on one IP skips it; other IPs still produce results."""
    ip_ok = "192.168.2.51"
    ip_err = "192.168.2.52"

    class _SelectiveErrorVl:
        async def query(self, *, expr: str, start: str, end: str) -> VlQueryResult:
            if ip_err in expr:
                raise VictoriaLogsClientError("selective error")
            msgs = [_msg(ip_ok, "ok.domain", "FORWARDED", 1714867200.0)]
            return VlQueryResult(
                lines=[VlLogLine(timestamp="", message=m, stream="", fields={}) for m in msgs],
                truncated=False,
            )

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=ip_ok,
        ip_spans=[_span(ip_err)],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=_SelectiveErrorVl(),
    )
    assert result is not None
    assert "ok.domain" in result.top_domains


@pytest.mark.asyncio
async def test_enrich_malformed_msg_skipped() -> None:
    """Non-JSON _msg is skipped without raising; valid records still counted."""
    expr = _pihole_queries_expr(_IP)
    vl = _FakeVl(
        {
            expr: [
                "not json at all",
                _msg(_IP, "good.com", "FORWARDED", 1714867200.0),
            ]
        }
    )

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is not None
    assert "good.com" in result.top_domains


@pytest.mark.asyncio
async def test_enrich_non_dict_json_skipped() -> None:
    """JSON that parses to a non-dict (e.g. list) is skipped via isinstance check."""
    expr = _pihole_queries_expr(_IP)
    vl = _FakeVl(
        {
            expr: [
                json.dumps(["not", "a", "dict"]),  # list → not isinstance(record, dict) → skip
                _msg(_IP, "valid.com", "FORWARDED", 1714867200.0),
            ]
        }
    )

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is not None
    assert "valid.com" in result.top_domains


@pytest.mark.asyncio
async def test_enrich_client_ip_mismatch_dropped() -> None:
    """A record whose client_ip != queried IP is excluded (phrase-match false positive)."""
    expr = _pihole_queries_expr(_IP)
    # The record's client_ip is a different IP that happened to match the phrase.
    other_ip = "192.168.2.99"
    vl = _FakeVl(
        {
            expr: [
                # Mismatch: client_ip is other_ip, not _IP.
                json.dumps(
                    {
                        "client_ip": other_ip,
                        "domain": "false.pos",
                        "status": "FORWARDED",
                        "time": 1714867200.0,
                    }
                ),
            ]
        }
    )

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    # The only record was a false positive → saw_any stays False → None.
    assert result is None


@pytest.mark.asyncio
async def test_enrich_saw_any_false_when_all_records_filtered() -> None:
    """If VL returns records but all are filtered (mismatch), return None."""
    expr = _pihole_queries_expr(_IP)
    # ALL records have wrong client_ip.
    vl = _FakeVl(
        {
            expr: [
                json.dumps(
                    {
                        "client_ip": "10.0.0.1",
                        "domain": "x.com",
                        "status": "FORWARDED",
                        "time": 1714867200.0,
                    }
                ),
                json.dumps(
                    {
                        "client_ip": "10.0.0.2",
                        "domain": "y.com",
                        "status": "GRAVITY",
                        "time": 1714867201.0,
                    }
                ),
            ]
        }
    )

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is None


@pytest.mark.asyncio
async def test_enrich_top_domains_cap() -> None:
    """top_domains is capped at _DNS_TOP_DOMAINS_CAP (10) even with >10 distinct domains."""
    expr = _pihole_queries_expr(_IP)
    # Generate 15 distinct domains, each with count = 1.
    msgs = [
        _msg(_IP, f"domain{i:02d}.com", "FORWARDED", float(1714867200 + i))
        for i in range(_TOP_DOMAINS_GENERATE)
    ]
    vl = _FakeVl({expr: msgs})

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is not None
    assert len(result.top_domains) == _DNS_TOP_DOMAINS_CAP


@pytest.mark.asyncio
async def test_enrich_empty_vl_response_returns_none() -> None:
    """VL returns no lines → saw_any False → None."""
    expr = _pihole_queries_expr(_IP)
    vl = _FakeVl({expr: []})

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is None


@pytest.mark.asyncio
async def test_enrich_setdefault_does_not_override_span_window() -> None:
    """current_ip setdefault does NOT override a span-provided window for the same IP."""
    span_first = "2024-05-04T06:00:00Z"
    span_last = "2024-05-04T18:00:00Z"
    span = _span(_IP, first_seen=span_first, last_seen=span_last)

    captured_calls: list[tuple[str, str]] = []

    class _CapturingVl:
        async def query(self, *, expr: str, start: str, end: str) -> VlQueryResult:
            captured_calls.append((start, end))
            msgs = [_msg(_IP, "x.com", "FORWARDED", 1714867200.0)]
            return VlQueryResult(
                lines=[VlLogLine(timestamp="", message=m, stream="", fields={}) for m in msgs],
                truncated=False,
            )

    await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[span],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=_CapturingVl(),
    )

    # Only ONE call should be made (setdefault didn't add a second window).
    assert len(captured_calls) == 1
    # The span's (first_seen, last_seen) were used, NOT (since, now).
    assert captured_calls[0] == (span_first, span_last)


@pytest.mark.asyncio
async def test_enrich_record_with_no_domain_field() -> None:
    """A record with domain=None is counted for blocked/time but not in domain_counts."""
    expr = _pihole_queries_expr(_IP)
    # Record has no domain (domain is None → isinstance check fails → not added to domain_counts).
    vl = _FakeVl(
        {
            expr: [
                json.dumps(
                    {"client_ip": _IP, "domain": None, "status": "GRAVITY", "time": 1714867200.0}
                ),
            ]
        }
    )

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is not None
    assert result.top_domains == []  # no domain to record
    assert result.blocked_count == 1  # status was blocked
    assert result.last_query_at is not None


@pytest.mark.asyncio
async def test_enrich_record_with_no_time_field() -> None:
    """A record with no time field: last_epoch stays None → last_query_at is None."""
    expr = _pihole_queries_expr(_IP)
    # Record is valid for domain + blocked but has no 'time'.
    vl = _FakeVl(
        {
            expr: [
                json.dumps({"client_ip": _IP, "domain": "x.com", "status": "FORWARDED"}),
            ]
        }
    )

    result = await _enrich_client_dns(
        is_host=False,
        current_ip=_IP,
        ip_spans=[],
        host_lan_ip=None,
        since=_SINCE,
        now=_NOW,
        vl_client=vl,
    )
    assert result is not None
    assert "x.com" in result.top_domains
    # last_epoch never set (no 'time' field) → last_query_at is None.
    assert result.last_query_at is None
