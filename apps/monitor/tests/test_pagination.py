"""Tests for the A1 cursor pagination module (STAGE-004-007).

Project test conventions discovered:
- Framework: pytest + pytest-asyncio, asyncio_mode="auto" (no decorator needed)
- Mocking: unittest.mock.AsyncMock for VictoriaLogsClient.query
- Assertions: plain assert; noqa: PLR2004 for magic-number comparisons
- 4-space indent, lines <=100 chars

Signatures:
  encode_cursor(cursor: LogCursor) -> str
  decode_cursor(raw: str) -> LogCursor  (raises InvalidCursorError on bad input)
  _iso_to_ns(ts: str) -> int
  _ns_to_iso(ns: int) -> str
  paginate_older(
      *, client: VictoriaLogsClient, expr: str, window_start: str, window_end: str,
      page_size: int, base_limits: VlQueryLimits, cursor: str | None,
  ) -> PaginatedLogs
  PaginatedLogs: {lines: list[VlLogLine], next_cursor: str|None, has_more: bool, truncated: bool}
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from homelab_monitor.kernel.config import VlQueryLimits
from homelab_monitor.kernel.logs.pagination import (
    InvalidCursorError,
    LogCursor,
    _iso_to_ns,  # pyright: ignore[reportPrivateUsage]
    _ns_to_iso,  # pyright: ignore[reportPrivateUsage]
    decode_cursor,
    encode_cursor,
    paginate_older,
)
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VlLogLine,
    VlQueryResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_LIMITS = VlQueryLimits(max_lines=100, max_bytes=1_000_000, timeout_seconds=5.0)

_WINDOW_START = "2026-05-01T00:00:00+00:00"
_WINDOW_END = "2026-05-02T00:00:00+00:00"


def _make_line(msg: str, ts: str) -> VlLogLine:
    """Construct a VlLogLine with the given message and timestamp."""
    return VlLogLine(timestamp=ts, message=msg, stream="test-stream", fields={})


def _make_client(lines: list[VlLogLine], *, truncated: bool = False) -> VictoriaLogsClient:
    """Return a VictoriaLogsClient whose query() always returns the given lines."""
    result = VlQueryResult(lines=lines, truncated=truncated)
    mock_page_client = MagicMock(spec=VictoriaLogsClient)
    mock_page_client.query = AsyncMock(return_value=result)

    mock_client = MagicMock(spec=VictoriaLogsClient)
    mock_client.with_limits = MagicMock(return_value=mock_page_client)
    return mock_client


def _make_truncating_client(corpus: list[VlLogLine]) -> VictoriaLogsClient:
    """VictoriaLogsClient mock replicating VL's mid-group `limit` truncation.

    Windowed fetch (start != end): return the max_lines LATEST in-window lines
    DESC, but if the cap splits a same-_time group, drop the rest of that group
    and keep exactly ONE arbitrary line of it (like confirmed real VL: limit=3
    -> 1 of 3). Exact-ns fetch (start == end): return the COMPLETE group at that
    ns (the [GROUP-COMPLETE] boundary query the fix relies on).
    """
    captured: dict[str, VlQueryLimits] = {}

    def _with_limits(limits: VlQueryLimits) -> VictoriaLogsClient:
        captured["limits"] = limits
        page = MagicMock(spec=VictoriaLogsClient)

        async def _query(*, expr: str, start: str, end: str) -> VlQueryResult:
            max_lines = captured["limits"].max_lines
            start_ns = _iso_to_ns(start)
            end_ns = _iso_to_ns(end)
            in_window = [ln for ln in corpus if start_ns <= _iso_to_ns(ln.timestamp) <= end_ns]
            desc = sorted(in_window, key=lambda ln: _iso_to_ns(ln.timestamp), reverse=True)
            if start == end:
                grp = [ln for ln in desc if _iso_to_ns(ln.timestamp) == start_ns]
                return VlQueryResult(lines=grp, truncated=len(desc) > max_lines)
            truncated = len(desc) > max_lines
            capped = desc[:max_lines]
            if truncated and capped:
                cut_ns = _iso_to_ns(capped[-1].timestamp)
                next_ns = _iso_to_ns(desc[max_lines].timestamp)
                if cut_ns == next_ns:
                    capped = [ln for ln in capped if _iso_to_ns(ln.timestamp) != cut_ns]
                    one = [ln for ln in desc if _iso_to_ns(ln.timestamp) == cut_ns][:1]
                    capped = capped + one
            return VlQueryResult(lines=capped, truncated=truncated)

        page.query = AsyncMock(side_effect=_query)
        return page

    client = MagicMock(spec=VictoriaLogsClient)
    client.with_limits = MagicMock(side_effect=_with_limits)
    return client


def _ns_for(iso: str) -> int:
    return _iso_to_ns(iso)


# ---------------------------------------------------------------------------
# encode_cursor / decode_cursor — round-trip
# ---------------------------------------------------------------------------


def test_encode_decode_round_trip() -> None:
    """encode then decode preserves t and n exactly."""
    cursor = LogCursor(t=1_700_000_000_000_000_000, n=3)
    raw = encode_cursor(cursor)
    decoded = decode_cursor(raw)
    assert decoded.t == cursor.t
    assert decoded.n == cursor.n


def test_encode_produces_base64url_string() -> None:
    """encode_cursor output is a valid base64url ASCII string."""
    cursor = LogCursor(t=1234567890, n=1)
    raw = encode_cursor(cursor)
    # Must be decodeable as base64url
    padding = (4 - len(raw) % 4) % 4
    payload = json.loads(base64.urlsafe_b64decode(raw + "=" * padding).decode())
    assert payload["t"] == 1234567890  # noqa: PLR2004
    assert payload["n"] == 1


# ---------------------------------------------------------------------------
# decode_cursor — error paths
# ---------------------------------------------------------------------------


def test_decode_cursor_malformed_base64_raises() -> None:
    """Completely invalid base64 input raises InvalidCursorError."""
    with pytest.raises(InvalidCursorError):
        decode_cursor("!!!not-base64!!!")


def test_decode_cursor_non_dict_json_raises() -> None:
    """A base64url-encoded JSON array (not object) raises InvalidCursorError."""
    payload = json.dumps([1, 2, 3]).encode("utf-8")
    raw = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(InvalidCursorError, match="not a JSON object"):
        decode_cursor(raw)


def test_decode_cursor_missing_t_raises() -> None:
    """Missing 't' field raises InvalidCursorError."""
    payload = json.dumps({"n": 1}).encode("utf-8")
    raw = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(InvalidCursorError, match="'t' must be an integer"):
        decode_cursor(raw)


def test_decode_cursor_missing_n_raises() -> None:
    """Missing 'n' field raises InvalidCursorError."""
    payload = json.dumps({"t": 1000}).encode("utf-8")
    raw = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(InvalidCursorError, match="'n' must be an integer"):
        decode_cursor(raw)


def test_decode_cursor_t_as_bool_raises() -> None:
    """t=True (bool subclass of int) raises InvalidCursorError."""
    payload = json.dumps({"t": True, "n": 1}).encode("utf-8")
    raw = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(InvalidCursorError, match="'t' must be an integer"):
        decode_cursor(raw)


def test_decode_cursor_n_as_bool_raises() -> None:
    """n=True (bool subclass of int) raises InvalidCursorError."""
    payload = json.dumps({"t": 1000, "n": True}).encode("utf-8")
    raw = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(InvalidCursorError, match="'n' must be an integer"):
        decode_cursor(raw)


def test_decode_cursor_n_less_than_1_raises() -> None:
    """n=0 raises InvalidCursorError (n must be >= 1)."""
    payload = json.dumps({"t": 1000, "n": 0}).encode("utf-8")
    raw = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(InvalidCursorError, match="'n' must be an integer"):
        decode_cursor(raw)


def test_decode_cursor_n_negative_raises() -> None:
    """n=-1 raises InvalidCursorError."""
    payload = json.dumps({"t": 1000, "n": -1}).encode("utf-8")
    raw = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_cursor(raw)


def test_decode_cursor_t_as_float_raises() -> None:
    """t=1.5 (float) raises InvalidCursorError."""
    payload = json.dumps({"t": 1.5, "n": 1}).encode("utf-8")
    raw = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(InvalidCursorError):
        decode_cursor(raw)


def test_decode_cursor_padding_tolerance() -> None:
    """decode_cursor handles missing base64 padding gracefully."""
    cursor = LogCursor(t=999, n=2)
    raw = encode_cursor(cursor)
    # Strip any padding and verify it still decodes
    raw_no_pad = raw.rstrip("=")
    decoded = decode_cursor(raw_no_pad)
    assert decoded.t == 999  # noqa: PLR2004
    assert decoded.n == 2  # noqa: PLR2004


# ---------------------------------------------------------------------------
# _iso_to_ns
# ---------------------------------------------------------------------------


def test_iso_to_ns_z_suffix() -> None:
    """'Z' suffix is treated as +00:00."""
    ns_z = _iso_to_ns("2026-05-01T00:00:00Z")
    ns_utc = _iso_to_ns("2026-05-01T00:00:00+00:00")
    assert ns_z == ns_utc


def test_iso_to_ns_positive_offset() -> None:
    """+05:30 offset is converted to UTC correctly."""
    # 2026-05-01T05:30:00+05:30 == 2026-05-01T00:00:00Z
    ns_offset = _iso_to_ns("2026-05-01T05:30:00+05:30")
    ns_utc = _iso_to_ns("2026-05-01T00:00:00Z")
    assert ns_offset == ns_utc


def test_iso_to_ns_negative_offset() -> None:
    """-05:00 offset is converted to UTC correctly."""
    # 2026-05-01T00:00:00-05:00 == 2026-05-01T05:00:00Z
    ns_offset = _iso_to_ns("2026-05-01T00:00:00-05:00")
    ns_utc = _iso_to_ns("2026-05-01T05:00:00Z")
    assert ns_offset == ns_utc


def test_iso_to_ns_full_9_digit_fractional() -> None:
    """9-digit fractional seconds preserved without truncation."""
    ns = _iso_to_ns("2026-05-01T00:00:01.123456789+00:00")
    # whole second part: epoch_s * 1e9; frac = 123456789
    ns_whole = _iso_to_ns("2026-05-01T00:00:01+00:00")
    assert ns == ns_whole + 123456789


def test_iso_to_ns_short_fractional_padded() -> None:
    """Short fractional '.5' is right-padded to 9 digits (= 500000000 ns)."""
    ns = _iso_to_ns("2026-05-01T00:00:01.5+00:00")
    ns_whole = _iso_to_ns("2026-05-01T00:00:01+00:00")
    assert ns == ns_whole + 500_000_000


def test_iso_to_ns_no_fractional() -> None:
    """Timestamp with no fractional seconds returns whole-second ns."""
    ns = _iso_to_ns("2026-05-01T00:00:01+00:00")
    ns_base = _iso_to_ns("2026-05-01T00:00:00+00:00")
    assert ns == ns_base + 1_000_000_000


def test_iso_to_ns_naive_timestamp() -> None:
    """Naive timestamp (no tz suffix, no Z) is treated as UTC (+00:00).

    Exercises the branch where the for-sign loop finds neither '+' nor '-'
    after index 11 (line 104→111 branch in pagination.py).
    """
    ns_naive = _iso_to_ns("2026-05-01T00:00:00")
    ns_utc = _iso_to_ns("2026-05-01T00:00:00+00:00")
    assert ns_naive == ns_utc


def test_iso_to_ns_non_numeric_fractional_returns_zero() -> None:
    """A '.' in the body with a NON-numeric fractional part leaves frac_ns=0
    and does not split body, so fromisoformat fails -> returns 0.

    Exercises the False path of `if frac_digits.isdigit():` (pagination.py
    line 115->119 branch).
    """
    # Naive (no tz sign after 'T') so the '.12x' fraction stays in `body`.
    assert _iso_to_ns("2026-05-01T00:00:01.12x") == 0


def test_iso_to_ns_round_trip_with_ns_to_iso() -> None:
    """_iso_to_ns(_ns_to_iso(x)) == x for a known nanosecond value."""
    # Pick a value with fractional ns component
    ns_in = 1_746_057_600_987_654_321
    assert _iso_to_ns(_ns_to_iso(ns_in)) == ns_in


# ---------------------------------------------------------------------------
# _ns_to_iso
# ---------------------------------------------------------------------------


def test_ns_to_iso_format() -> None:
    """Output matches YYYY-MM-DDTHH:MM:SS.fffffffff+00:00 format."""
    ns = 1_746_057_600_000_000_000
    result = _ns_to_iso(ns)
    # Check format: ends with +00:00, has 9 fractional digits
    assert result.endswith("+00:00")
    # Split on '.' to find fractional part (before +00:00)
    dot_idx = result.index(".")
    plus_idx = result.index("+", dot_idx)
    frac_part = result[dot_idx + 1 : plus_idx]
    assert len(frac_part) == 9  # noqa: PLR2004


def test_ns_to_iso_whole_second() -> None:
    """ns on a whole second has fractional .000000000."""
    ns = 1_746_057_600_000_000_000  # exact second, zero frac
    result = _ns_to_iso(ns)
    assert ".000000000" in result


def test_ns_to_iso_with_fractional() -> None:
    """ns with fractional part encodes it correctly."""
    base_ns = 1_746_057_600_000_000_000
    frac = 123_456_789
    result = _ns_to_iso(base_ns + frac)
    assert ".123456789" in result


def test_ns_to_iso_round_trip() -> None:
    """_ns_to_iso round-trips through _iso_to_ns."""
    for ns in [
        1_746_057_600_000_000_000,
        1_746_057_600_999_999_999,
        1_746_057_601_500_000_000,
    ]:
        assert _iso_to_ns(_ns_to_iso(ns)) == ns


# ---------------------------------------------------------------------------
# paginate_older — first page (no cursor)
# ---------------------------------------------------------------------------


async def test_paginate_first_page_fewer_than_page_size() -> None:
    """First page: VL returns fewer lines than page_size → no next cursor."""
    ts = "2026-05-01T12:00:00+00:00"
    lines = [_make_line(f"msg-{i}", ts) for i in range(3)]
    client = _make_client(lines)

    result = await paginate_older(
        client=client,
        expr="*",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        page_size=5,
        base_limits=_BASE_LIMITS,
        cursor=None,
    )

    assert len(result.lines) == 3  # noqa: PLR2004
    assert result.next_cursor is None
    assert result.has_more is False


async def test_paginate_first_page_exactly_page_size() -> None:
    """First page: VL returns exactly page_size lines → no next cursor (no extra line)."""
    ts = "2026-05-01T12:00:00+00:00"
    lines = [_make_line(f"msg-{i}", ts) for i in range(5)]
    client = _make_client(lines)

    result = await paginate_older(
        client=client,
        expr="*",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        page_size=5,
        base_limits=_BASE_LIMITS,
        cursor=None,
    )

    assert len(result.lines) == 5  # noqa: PLR2004
    assert result.next_cursor is None
    assert result.has_more is False


async def test_paginate_first_page_more_than_page_size() -> None:
    """First page: VL returns page_size+1 lines → has_more=True, next_cursor set."""
    # page_size=3; return 4 lines (3+1) at distinct timestamps so oldest is unambiguous
    lines = [
        _make_line("newest", "2026-05-01T12:00:03+00:00"),
        _make_line("middle1", "2026-05-01T12:00:02+00:00"),
        _make_line("middle2", "2026-05-01T12:00:01+00:00"),
        _make_line("oldest", "2026-05-01T12:00:00+00:00"),
    ]
    client = _make_client(lines)

    result = await paginate_older(
        client=client,
        expr="*",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        page_size=3,
        base_limits=_BASE_LIMITS,
        cursor=None,
    )

    assert result.has_more is True
    assert result.next_cursor is not None
    assert len(result.lines) == 3  # noqa: PLR2004


async def test_paginate_first_page_empty() -> None:
    """First page: VL returns zero lines → empty result, no cursor."""
    client = _make_client([])

    result = await paginate_older(
        client=client,
        expr="*",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        page_size=5,
        base_limits=_BASE_LIMITS,
        cursor=None,
    )

    assert result.lines == []
    assert result.next_cursor is None
    assert result.has_more is False


# ---------------------------------------------------------------------------
# paginate_older — cursor page, normal case
# ---------------------------------------------------------------------------


async def test_paginate_cursor_page_normal() -> None:
    """Cursor page: effective_end is set to cursor.t; skip_n lines are dropped."""
    # Encode a cursor pointing to t=some ns, n=1
    boundary_ts = "2026-05-01T12:00:02+00:00"
    boundary_ns = _ns_for(boundary_ts)
    cursor_str = encode_cursor(LogCursor(t=boundary_ns, n=1))

    # VL returns 2 lines: the boundary line (to be skipped) + 1 older line
    lines = [
        _make_line("boundary-line", boundary_ts),
        _make_line("older-line", "2026-05-01T12:00:01+00:00"),
    ]
    client = _make_client(lines)

    result = await paginate_older(
        client=client,
        expr="*",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        page_size=5,
        base_limits=_BASE_LIMITS,
        cursor=cursor_str,
    )

    # The boundary line is dropped; only the older line is kept
    assert len(result.lines) == 1
    assert result.lines[0].message == "older-line"
    assert result.has_more is False


# ---------------------------------------------------------------------------
# paginate_older — cursor at/below window floor
# ---------------------------------------------------------------------------


async def test_paginate_cursor_at_window_start_returns_empty() -> None:
    """Cursor t == window_start_ns → returns empty, no cursor."""
    window_start_ns = _ns_for(_WINDOW_START)
    cursor_str = encode_cursor(LogCursor(t=window_start_ns, n=1))
    client = _make_client([_make_line("irrelevant", _WINDOW_START)])

    result = await paginate_older(
        client=client,
        expr="*",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        page_size=5,
        base_limits=_BASE_LIMITS,
        cursor=cursor_str,
    )

    assert result.lines == []
    assert result.next_cursor is None
    assert result.has_more is False


async def test_paginate_cursor_below_window_start_returns_empty() -> None:
    """Cursor t < window_start_ns → returns empty immediately (floor guard)."""
    window_start_ns = _ns_for(_WINDOW_START)
    below_ns = window_start_ns - 1_000_000_000  # 1 second before window start
    cursor_str = encode_cursor(LogCursor(t=below_ns, n=1))
    client = _make_client([_make_line("irrelevant", _WINDOW_START)])

    result = await paginate_older(
        client=client,
        expr="*",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        page_size=5,
        base_limits=_BASE_LIMITS,
        cursor=cursor_str,
    )

    assert result.lines == []
    assert result.next_cursor is None
    assert result.has_more is False


# ---------------------------------------------------------------------------
# paginate_older — malformed cursor propagates InvalidCursorError
# ---------------------------------------------------------------------------


async def test_paginate_malformed_cursor_raises_invalid_cursor_error() -> None:
    """Invalid cursor string propagates InvalidCursorError from decode_cursor."""
    client = _make_client([])

    with pytest.raises(InvalidCursorError):
        await paginate_older(
            client=client,
            expr="*",
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            page_size=5,
            base_limits=_BASE_LIMITS,
            cursor="!!!invalid!!!",
        )


# ---------------------------------------------------------------------------
# paginate_older — boundary collision (CRITICAL correctness test)
#
# Plant 4 lines at the EXACT same ns straddling a page boundary (page_size=3).
# Page 1 must contain the 3 newest; page 2 must contain the 1 remaining.
# Together they must cover ALL 4 lines with NO duplicates and NO gaps.
# ---------------------------------------------------------------------------


async def test_paginate_mid_group_truncation_no_data_loss() -> None:
    """REGRESSION (STAGE-004-007 Refinement): VL truncates MID same-ns group.

    7 lines, 3 (L3/L4/L5) at identical ns. With page_size=3 the naive
    paginator fetched only ONE collision line, set has_more=False, and
    DROPPED L1,L2 + two collision lines. The fix completes the boundary
    group so paging covers ALL 7 with NO gap/dup; a page never splits the
    same-ns group.
    """
    collision_ts = "2026-05-01T13:00:02.000000001+00:00"
    corpus = [
        _make_line("L7", "2026-05-01T13:00:07+00:00"),
        _make_line("L6", "2026-05-01T13:00:05+00:00"),
        _make_line("L5", collision_ts),
        _make_line("L4", collision_ts),
        _make_line("L3", collision_ts),
        _make_line("L2", "2026-05-01T13:00:01+00:00"),
        _make_line("L1", "2026-05-01T13:00:00+00:00"),
    ]
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        pages += 1
        assert pages <= 10, "pagination failed to terminate"  # noqa: PLR2004
        page = await paginate_older(
            client=_make_truncating_client(corpus),
            expr="*",
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            page_size=3,
            base_limits=_BASE_LIMITS,
            cursor=cursor,
        )
        seen.extend(ln.message for ln in page.lines)
        n_collision = sum(1 for ln in page.lines if ln.message in {"L3", "L4", "L5"})
        assert n_collision in (0, 3), (
            f"page split the same-ns group: {[ln.message for ln in page.lines]}"
        )
        if not page.has_more:
            assert page.next_cursor is None
            break
        assert page.next_cursor is not None
        cursor = page.next_cursor
    assert sorted(seen) == ["L1", "L2", "L3", "L4", "L5", "L6", "L7"], seen
    assert len(seen) == len(set(seen)), f"duplicates: {seen}"


async def test_paginate_boundary_collision_no_gap_no_dup() -> None:
    """CRITICAL: lines sharing the same _time at a page boundary produce no gap/dup.

    Setup: 4 lines all at the same nanosecond timestamp, page_size=3.
    Page 1: VL returns all 4. Keep 3 (remaining > page_size). Cursor points
            to the shared ns with n=3.
    Page 2: VL re-includes the 3 boundary lines + the 1 older line, but
            we fetch page_size+skip_n = 3+3 = 6 lines; skip 3 boundary;
            keep up to 3 → keeps the 1 older line.
    Union of kept lines from both pages == all 4 original lines, no overlap.
    """
    shared_ts = "2026-05-01T12:00:05+00:00"
    shared_ns = _ns_for(shared_ts)
    older_ts = "2026-05-01T12:00:04+00:00"

    # Page 1 call: VL returns the 4 lines at shared_ts (page_size+1 = 4 lines)
    page1_lines = [
        _make_line("collision-A", shared_ts),
        _make_line("collision-B", shared_ts),
        _make_line("collision-C", shared_ts),
        _make_line("collision-D", shared_ts),
    ]

    # Simulate page 1
    # truncated=True models real VL: the fetch cap landed at-or-inside the
    # same-ns group with MORE lines below, so VL flagged truncated. This is the
    # authoritative "older lines exist" signal that drives has_more
    # (fetch_was_capped alone is NOT sufficient -- STAGE-004-007).
    client1 = _make_client(page1_lines, truncated=True)
    page1 = await paginate_older(
        client=client1,
        expr="*",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        page_size=3,
        base_limits=_BASE_LIMITS,
        cursor=None,
    )

    assert page1.has_more is True
    assert page1.next_cursor is not None
    # Under [GROUP-COMPLETE]: page_size=3, but fetch returns 4 at shared_ts.
    # The boundary group completion re-fetches and includes all 4 at shared_ts.
    assert len(page1.lines) == 4  # noqa: PLR2004

    # Decode the produced cursor: t must equal shared_ns, n must equal 4
    # (the full group size, not the pre-fix n=3).
    decoded_cursor = decode_cursor(page1.next_cursor)
    assert decoded_cursor.t == shared_ns
    assert decoded_cursor.n == 4  # noqa: PLR2004

    # Page 2 call: VL re-includes the 4 boundary lines at shared_ts plus 1 older
    # (fetch_limit = page_size + skip_n = 3 + 4 = 7; VL returns 5)
    page2_lines = [
        _make_line("collision-A", shared_ts),
        _make_line("collision-B", shared_ts),
        _make_line("collision-C", shared_ts),
        _make_line("collision-D", shared_ts),
        _make_line("older-line", older_ts),
    ]
    client2 = _make_client(page2_lines)
    page2 = await paginate_older(
        client=client2,
        expr="*",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        page_size=3,
        base_limits=_BASE_LIMITS,
        cursor=page1.next_cursor,
    )

    # After skipping 4 boundary lines, only 1 older line remains — less than page_size
    assert page2.has_more is False
    assert len(page2.lines) == 1
    assert page2.lines[0].message == "older-line"

    # Union check: page1 kept 4 collision lines + page2 kept 1 older = 5 total.
    # (All 4 collision lines stayed together in page1; page2 has only the older.)
    all_kept = [ln.message for ln in page1.lines] + [ln.message for ln in page2.lines]
    assert len(all_kept) == 5  # noqa: PLR2004
    # No message appears twice (gap-free, dup-free).
    assert len(set(all_kept)) == len(all_kept), f"Duplicate found: {all_kept}"


# ---------------------------------------------------------------------------
# paginate_older — burst accumulation (n accumulates across pages at same ns)
# ---------------------------------------------------------------------------


async def test_paginate_burst_n_accumulates() -> None:
    """Burst at one ns: under [GROUP-COMPLETE], the entire burst consumes one page.

    Setup: 5 lines all at the same ns, page_size=2.
    Pre-fix: Page 1 would return 2, page 2 would return 2, page 3 would return 1.
    Post-fix: When page 1's fetch (3 lines) is capped and the oldest line (line 2)
              is at the boundary, [GROUP-COMPLETE] re-fetches all 5 lines at that ns
              and includes them all in page 1 (even though page_size=2).
    Page 1: fetches 3; boundary-group-completion re-fetches all 5; returns all 5;
            cursor n=5, has_more=False (no older lines).
    """
    burst_ts = "2026-05-01T12:00:10+00:00"
    _ns_for(burst_ts)

    all_burst = [_make_line(f"burst-{i}", burst_ts) for i in range(5)]

    # Page 1: VL returns 3 (page_size+1=3) burst lines, but [GROUP-COMPLETE]
    # re-fetches and includes all 5 at the boundary ns.
    client1 = _make_client(all_burst)
    page1 = await paginate_older(
        client=client1,
        expr="*",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        page_size=2,
        base_limits=_BASE_LIMITS,
        cursor=None,
    )
    # Under [GROUP-COMPLETE], page 1 gets the full group of 5 lines (not 2).
    assert page1.has_more is False
    assert page1.next_cursor is None
    assert len(page1.lines) == 5  # noqa: PLR2004
    # All lines belong to the same ns burst and are returned together.
    for ln in page1.lines:
        assert ln.message.startswith("burst-")


async def test_paginate_cursor_page_accumulates_n_at_same_ns() -> None:
    """Cursor page whose new oldest ns == cursor.t accumulates n.

    Exercises the burst-accumulation branch ``new_n = accumulate_n +
    n_at_oldest``: a cursor already at a ns + a fetch whose oldest kept line
    re-lands on that exact ns with a strictly-older line still present, so n
    ACCUMULATES (prev n + this page's count at that ns).
    """
    same_ns_ts = "2026-05-01T12:00:05+00:00"
    same_ns = _ns_for(same_ns_ts)
    older_ts = "2026-05-01T12:00:04+00:00"
    cursor_str = encode_cursor(LogCursor(t=same_ns, n=1))
    lines = [
        _make_line("boundary", same_ns_ts),
        _make_line("same-1", same_ns_ts),
        _make_line("same-2", same_ns_ts),
        _make_line("older", older_ts),
    ]
    client = _make_client(lines)
    result = await paginate_older(
        client=client,
        expr="*",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        page_size=2,
        base_limits=_BASE_LIMITS,
        cursor=cursor_str,
    )
    assert result.has_more is True
    assert result.next_cursor is not None
    decoded = decode_cursor(result.next_cursor)
    assert decoded.t == same_ns
    assert decoded.n == 3  # noqa: PLR2004
