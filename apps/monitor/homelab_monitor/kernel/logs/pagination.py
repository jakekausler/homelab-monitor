"""Shared A1 cursor pagination for log endpoints (STAGE-004-007).

A1 cursor = base64url(JSON {t: <oldest returned line's _time in ns>,
                            n: <count of returned lines at exactly that ns>}).
Opaque to the UI (D-CURSOR-OPAQUE). Backward-only (D-CURSOR-BACKWARD-ONLY-V1).

"Load older" algorithm: decode (t, n) -> re-query the SAME expr with an
INCLUSIVE upper bound end=t (so lines at _time==t are re-included) over the
existing limit=N-latest path with limit = page_size + n -> drop the n boundary
lines already shown -> keep the next page_size -> recompute the cursor.
Gap-free AND dup-free under _time collisions. See STAGE-004-007.md.

NO VL `sort offset limit` (OOMs even on tiny sets; VL #129/#8127).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.config import VlQueryLimits
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
    VlLogLine,
)

# Upper bound on lines re-fetched to COMPLETE a same-ns boundary group (see
# [GROUP-COMPLETE] in paginate_older). Large enough to absorb realistic same-ns
# bursts in one query; a group exceeding this surfaces truncated=True.
# LIMITATION (pathological): a single nanosecond with >10k lines is truncated
# here — those beyond 10k are not included in the boundary group and the cursor
# n undercounts them, so they may be re-returned on the next page (a dup, not a
# drop). >10k lines sharing one nanosecond does not occur with real log sources.
_BOUNDARY_GROUP_MAX_LINES = 10_000


class InvalidCursorError(ValueError):
    """Raised when a log cursor cannot be decoded or parsed."""


class LogCursor(BaseModel):
    """Opaque A1 cursor. base64url(JSON) over the wire.

    t: oldest returned line's _time in nanoseconds (int).
    n: count of returned lines whose _time == t at the boundary (>=1).
    """

    model_config = ConfigDict(extra="forbid")

    t: int
    n: int


def encode_cursor(cursor: LogCursor) -> str:
    """Encode a LogCursor as an opaque base64url-encoded JSON string."""
    payload: dict[str, int] = {"t": cursor.t, "n": cursor.n}
    json_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return base64.urlsafe_b64encode(json_str.encode("utf-8")).decode("ascii")


def decode_cursor(raw: str) -> LogCursor:
    """Decode an opaque base64url-encoded JSON cursor into a LogCursor.

    Raises InvalidCursorError if malformed. Mirrors
    cron.run_repository._decode_runs_cursor padding-tolerant decode.
    """
    try:
        padding = (4 - len(raw) % 4) % 4
        json_bytes = base64.urlsafe_b64decode(raw + "=" * padding)
        payload: Any = json.loads(json_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise InvalidCursorError("cursor payload is not a JSON object")
        payload_dict = cast(dict[str, Any], payload)
        t = payload_dict.get("t")
        n = payload_dict.get("n")
        # bool is an int subclass — reject it explicitly.
        if not isinstance(t, int) or isinstance(t, bool):
            raise InvalidCursorError("cursor 't' must be an integer")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise InvalidCursorError("cursor 'n' must be an integer >= 1")
        return LogCursor(t=t, n=n)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        if isinstance(exc, InvalidCursorError):
            raise
        raise InvalidCursorError(f"invalid cursor format: {exc}") from exc


def _iso_to_ns(ts: str) -> int:
    """Parse an RFC3339/ISO-8601 timestamp string to integer nanoseconds since
    epoch (UTC).

    Uses datetime.fromisoformat for the whole-second + timezone portion (Python
    3.11+ parses a trailing 'Z' and numeric offsets natively). fromisoformat
    truncates fractional seconds to microseconds, so the sub-second fraction is
    parsed separately to preserve up to 9 (nanosecond) digits.

    Unparseable timestamps (e.g. a malformed VL ``_time``) sort to epoch 0
    rather than raising, so a single bad line cannot fail an entire page.
    """
    s = ts.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"

    # Split the sub-second fraction from the rest so we can keep ns precision.
    # Locate the tz suffix by scanning for '+' or '-' AFTER the 'T' separator.
    frac_ns = 0
    t_idx = s.find("T")
    tz_idx = -1
    if t_idx != -1:
        for sign in ("+", "-"):
            idx = s.find(sign, t_idx)
            if idx != -1:
                tz_idx = idx if tz_idx == -1 else min(tz_idx, idx)
    tz = s[tz_idx:] if tz_idx != -1 else ""
    body = s[:tz_idx] if tz_idx != -1 else s
    if "." in body:
        whole, frac = body.split(".", 1)
        frac_digits = (frac + "000000000")[:9]
        if frac_digits.isdigit():
            frac_ns = int(frac_digits)
            body = whole

    try:
        dt = datetime.fromisoformat(body + (tz or "+00:00"))
    except ValueError:
        return 0
    epoch_s = int(dt.timestamp())
    return epoch_s * 1_000_000_000 + frac_ns


@dataclass(slots=True, frozen=True)
class PaginatedLogs:
    """Result of one A1 page: the kept lines + the cursor for the NEXT older page."""

    lines: list[VlLogLine]
    next_cursor: str | None
    has_more: bool
    truncated: bool


async def paginate_older(  # noqa: PLR0913
    *,
    client: VictoriaLogsClient,
    expr: str,
    window_start: str,
    window_end: str,
    page_size: int,
    base_limits: VlQueryLimits,
    cursor: str | None,
) -> PaginatedLogs:
    """Run ONE A1 page (latest page_size on first call; page_size strictly-older
    lines on a cursor call).

    window_start / window_end are ISO strings (the endpoint's full window).
    On the first page (cursor is None) the query window is [window_start,
    window_end]. On a cursor page the upper bound is tightened to the cursor's
    `t` (inclusive) and `n` boundary lines are dropped.

    Returns PaginatedLogs(lines, next_cursor, has_more, truncated). next_cursor
    is None (has_more False) whenever the page does NOT fill page_size strictly-
    older lines, or whenever the decoded cursor t precedes window_start.
    """
    skip_n = 0
    accumulate_t: int | None = None
    accumulate_n = 0
    effective_end = window_end

    if cursor is not None:
        decoded = decode_cursor(cursor)  # raises InvalidCursorError
        accumulate_t = decoded.t
        accumulate_n = decoded.n
        skip_n = decoded.n
        window_start_ns = _iso_to_ns(window_start)
        # Cursor at/below window floor -> nothing older remains.
        if decoded.t <= window_start_ns:
            return PaginatedLogs(lines=[], next_cursor=None, has_more=False, truncated=False)
        # Inclusive upper bound = t (re-include the boundary-ns lines).
        # [A-END-INCLUSIVE] VL [start,end] inclusive. If Build finds end is
        # EXCLUSIVE, change to _ns_to_iso(decoded.t + 1) here.
        effective_end = _ns_to_iso(decoded.t)

    # Fetch page_size + skip_n LATEST lines over [window_start, effective_end].
    fetch_limit = page_size + skip_n
    fetch_limits = VlQueryLimits(
        max_lines=fetch_limit,
        max_bytes=base_limits.max_bytes,
        timeout_seconds=base_limits.timeout_seconds,
    )
    page_client = client.with_limits(fetch_limits)
    result = await page_client.query(expr=expr, start=window_start, end=effective_end)

    # VL returns NEWEST-first within [start,end] under limit=N-latest? The
    # existing client preserves VL response order. The A1 algorithm needs the
    # lines ordered NEWEST->OLDEST so the first `skip_n` are the already-shown
    # boundary lines. Normalize defensively by sorting on _time DESC.
    # Precompute each line's ns ONCE; reuse across sort, boundary detection,
    # n_at_oldest, and has_older checks (avoids re-parsing the ISO timestamp
    # ~4x per line). Keyed on the timestamp STRING (NOT id(ln)): GC-safe (no
    # object-lifetime coupling) and higher hit-rate (same-ts lines, common in
    # logs, share one parse).
    _ns_cache: dict[str, int] = {}

    def _ns(ln: VlLogLine) -> int:
        ts = ln.timestamp
        cached = _ns_cache.get(ts)
        if cached is None:
            cached = _iso_to_ns(ts)
            _ns_cache[ts] = cached
        return cached

    fetched = sorted(result.lines, key=_ns, reverse=True)

    # Drop the skip_n already-shown boundary lines (the newest n at exactly t).
    remaining = fetched[skip_n:]

    # Keep the next page_size.
    kept = remaining[:page_size]

    if not kept:
        return PaginatedLogs(lines=[], next_cursor=None, has_more=False, truncated=result.truncated)

    # Candidate boundary ns = oldest kept line.
    oldest_ns = _ns(kept[-1])

    # [GROUP-COMPLETE] VL's limit truncates MID-GROUP: when the fetch cap lands
    # inside a set of lines sharing the exact same _time (ns), VL returns an
    # ARBITRARY SUBSET of that group (confirmed against live VL). Trusting the
    # capped fetch would drop the truncated-away same-ns lines and miscompute
    # n_at_oldest / has_more, terminating pagination early -> silent data-loss
    # gap. A page boundary must NEVER split a same-ns group: when the initial
    # fetch was capped AND the oldest kept line coincides with the fetch's own
    # oldest line, re-fetch the COMPLETE group at exactly oldest_ns and make the
    # whole group part of this page (even if it exceeds page_size). The next
    # cursor (t=oldest_ns, n=group size) then advances strictly older next page.
    boundary_truncated = result.truncated
    fetch_was_capped = len(fetched) >= fetch_limit
    if fetch_was_capped and oldest_ns == _ns(fetched[-1]):
        group_iso = _ns_to_iso(oldest_ns)
        group_limits = VlQueryLimits(
            max_lines=max(fetch_limit, _BOUNDARY_GROUP_MAX_LINES),
            max_bytes=base_limits.max_bytes,
            timeout_seconds=base_limits.timeout_seconds,
        )
        group_client = client.with_limits(group_limits)
        group_result = await group_client.query(expr=expr, start=group_iso, end=group_iso)
        boundary_truncated = boundary_truncated or group_result.truncated
        full_group = [ln for ln in group_result.lines if _ns(ln) == oldest_ns]
        newer_than_boundary = [ln for ln in kept if _ns(ln) > oldest_ns]
        kept = newer_than_boundary + full_group

    # n_at_oldest is the FULL count at oldest_ns within this (group-complete) page.
    n_at_oldest = sum(1 for ln in kept if _ns(ln) == oldest_ns)

    # Burst accumulation: if the new oldest ns equals the previous cursor t,
    # the boundary n must ACCUMULATE so paging a same-ns burst stays correct.
    # LIMITATION (pathological): for a same-ns burst far larger than page_size,
    # accumulated n (and thus next request's skip_n + fetch_limit) grows each
    # page, so a multi-million-line single-ns burst degrades to fetching the
    # whole burst per page. Real log sources don't produce that; the
    # [GROUP-COMPLETE] branch already returns a complete same-ns group in one
    # page, so normal bursts resolve in a single page.
    if accumulate_t is not None and oldest_ns == accumulate_t:
        new_n = accumulate_n + n_at_oldest
    else:
        new_n = n_at_oldest

    # More strictly-older lines exist iff the fetch SAW an older-than-boundary
    # line OR VL truncated the INITIAL fetch (more-than-fetch_limit lines in the
    # window => older lines exist below the cap). result.truncated is the
    # AUTHORITATIVE "more-in-window" signal: VictoriaLogsClient requests
    # limit=fetch_limit+1 and sets truncated=True only when the (fetch_limit+1)th
    # line arrives. fetch_was_capped (len==fetch_limit) is NOT a valid signal --
    # it is True even for a window holding EXACTLY fetch_limit lines with nothing
    # older, which over-fires the cursor (STAGE-004-007 has_more regression).
    has_older_in_fetch = any(_ns(ln) < oldest_ns for ln in remaining)
    has_more = has_older_in_fetch or result.truncated

    next_cursor = encode_cursor(LogCursor(t=oldest_ns, n=new_n)) if has_more else None

    # kept is NEWEST->OLDEST (the A1 skip/cursor math requires DESC). Present
    # OLDEST->NEWEST so the viewer renders oldest-at-top and "Load older"
    # prepends. Cursor math above already consumed kept[-1] (the oldest line).
    return PaginatedLogs(
        lines=list(reversed(kept)),
        next_cursor=next_cursor,
        has_more=has_more,
        truncated=boundary_truncated,
    )


def _ns_to_iso(ns: int) -> str:
    """Convert integer nanoseconds-since-epoch to an RFC3339 UTC string with
    nanosecond precision (so VL's end bound lands exactly on the boundary line).
    """
    secs, frac = divmod(ns, 1_000_000_000)
    dt = datetime.fromtimestamp(secs, tz=UTC)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{frac:09d}+00:00"


__all__ = [
    "InvalidCursorError",
    "LogCursor",
    "PaginatedLogs",
    "decode_cursor",
    "encode_cursor",
    "paginate_older",
]
