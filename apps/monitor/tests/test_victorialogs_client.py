"""Unit tests for VictoriaLogsClient (STAGE-002-013).

Project test conventions discovered:
- Framework: pytest + pytest-asyncio, @pytest.mark.asyncio
- HTTP mocking: pytest_httpx.HTTPXMock (httpx_mock fixture)
- Assertions: plain assert, noqa: PLR2004 for magic numbers
- No dedicated CollectorContext needed for client-only tests
- Sync tests for pure functions, async tests for network calls
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.config import VlQueryLimits
from homelab_monitor.kernel.cron.log_match import canonical_log_key
from homelab_monitor.kernel.logs.victorialogs_client import (
    HitsSeries,
    VictoriaLogsClient,
    VictoriaLogsClientError,
    build_amode_query,
    build_bmode_query,
    logsql_quote_phrase,
)

_VL_URL = "http://vl-test:9428"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_limits(**overrides: object) -> VlQueryLimits:
    """Return VlQueryLimits with sensible test defaults."""
    return VlQueryLimits(
        max_lines=overrides.get("max_lines", 100),  # type: ignore[arg-type]
        max_bytes=overrides.get("max_bytes", 1_000_000),  # type: ignore[arg-type]
        timeout_seconds=overrides.get("timeout_seconds", 5.0),  # type: ignore[arg-type]
    )


def _make_client(
    http_client: httpx.AsyncClient,
    *,
    limits: VlQueryLimits | None = None,
) -> VictoriaLogsClient:
    return VictoriaLogsClient(
        vl_url=_VL_URL,
        http_client=http_client,
        limits=limits or _default_limits(),
    )


def _ndjson_line(
    *,
    stream_id: str = "stream1",
    msg: str = "hello",
    ts: str = "2026-05-19T00:00:00+00:00",
    **extra: str,
) -> str:
    fields = {
        "_stream_id": stream_id,
        "_msg": msg,
        "_time": ts,
        **extra,
    }
    import json  # noqa: PLC0415

    return json.dumps(fields)


# ---------------------------------------------------------------------------
# build_amode_query
# ---------------------------------------------------------------------------


def test_build_amode_query_format() -> None:
    """build_amode_query produces the exact LogsQL filter string."""
    result = build_amode_query("abc-123-def")
    assert result == 'SYSLOG_IDENTIFIER:hmrun AND run_id:"abc-123-def"'


def test_build_amode_query_run_id_is_regular_field_not_stream_field() -> None:
    """Guard against stream-field regression: run_id must appear as a regular field filter.

    A stream-field filter would look like {run_id="..."}, not run_id:"...".
    """
    result = build_amode_query("some-uuid-here")
    # Must NOT use curly-brace stream selector syntax
    assert "{" not in result
    assert "}" not in result
    # Must use the regular-field colon syntax
    assert 'run_id:"some-uuid-here"' in result
    # Must include SYSLOG_IDENTIFIER filter
    assert "SYSLOG_IDENTIFIER:hmrun" in result


def test_build_amode_query_uuid_style() -> None:
    """UUID-style run_id is correctly embedded."""
    run_id = "550e8400-e29b-41d4-a716-446655440000"
    result = build_amode_query(run_id)
    assert f'run_id:"{run_id}"' in result


# ---------------------------------------------------------------------------
# build_bmode_query
# ---------------------------------------------------------------------------


def test_build_bmode_query_paren_wrapped_command() -> None:
    """B-mode query uses canonical_log_key and phrase-quotes it."""
    command = "(/usr/bin/backup.sh)"
    expected_key = canonical_log_key(command)
    result = build_bmode_query(command)
    assert result == f'"{expected_key}"'


def test_build_bmode_query_plain_command() -> None:
    """Plain command (not paren-wrapped) also produces a phrase-quoted canonical key."""
    command = "/usr/bin/backup.sh"
    expected_key = canonical_log_key(command)
    result = build_bmode_query(command)
    assert result == f'"{expected_key}"'


def test_build_bmode_query_escapes_double_quotes_in_canonical_key() -> None:
    """Any double quotes in the canonical key are backslash-escaped."""
    # Use a command with a double-quote in it (unusual but should be handled)
    command = '/bin/sh -c "echo hi"'
    result = build_bmode_query(command)
    # Result must start and end with unescaped quote
    assert result.startswith('"')
    assert result.endswith('"')
    # No unescaped inner quotes (they must be \" inside the phrase)
    inner = result[1:-1]
    # Count unescaped double quotes in inner
    unescaped = sum(
        1 for i, ch in enumerate(inner) if ch == '"' and (i == 0 or inner[i - 1] != "\\")
    )
    assert unescaped == 0


# ---------------------------------------------------------------------------
# Happy path query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_happy_path_two_lines(httpx_mock: HTTPXMock) -> None:
    """Happy path: VL returns 2 NDJSON lines; result has 2 entries, truncated=False."""
    line1 = _ndjson_line(stream_id="s1", msg="first", ts="2026-05-19T00:00:00+00:00")
    line2 = _ndjson_line(stream_id="s2", msg="second", ts="2026-05-19T00:00:01+00:00")
    httpx_mock.add_response(method="GET", text=f"{line1}\n{line2}\n")

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        result = await client.query(
            expr='run_id:"abc"',
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert result.truncated is False
    assert len(result.lines) == 2  # noqa: PLR2004
    assert result.lines[0].stream == "s1"
    assert result.lines[0].message == "first"
    assert result.lines[0].timestamp == "2026-05-19T00:00:00+00:00"
    assert result.lines[1].stream == "s2"
    assert result.lines[1].message == "second"


@pytest.mark.asyncio
async def test_query_fields_carry_regular_fields(httpx_mock: HTTPXMock) -> None:
    """Non-builtin fields (run_id, service) surface in VlLogLine.fields."""
    line = _ndjson_line(
        stream_id="st",
        msg="payload",
        ts="2026-05-19T00:00:00+00:00",
        run_id="my-run-uuid",
        service="hmrun",
    )
    httpx_mock.add_response(method="GET", text=f"{line}\n")

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        result = await client.query(
            expr='run_id:"my-run-uuid"',
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert len(result.lines) == 1
    assert result.lines[0].fields["run_id"] == "my-run-uuid"
    assert result.lines[0].fields["service"] == "hmrun"
    # Builtins must NOT appear in fields
    assert "_stream_id" not in result.lines[0].fields
    assert "_msg" not in result.lines[0].fields
    assert "_time" not in result.lines[0].fields


@pytest.mark.asyncio
async def test_query_empty_response(httpx_mock: HTTPXMock) -> None:
    """Empty body from VL returns zero lines and truncated=False."""
    httpx_mock.add_response(method="GET", text="")

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        result = await client.query(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert result.truncated is False
    assert len(result.lines) == 0


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_truncation_by_max_lines(httpx_mock: HTTPXMock) -> None:
    """max_lines=2 cap: 5 lines returned → result has 2 entries, truncated=True."""
    lines = [_ndjson_line(msg=f"line-{i}", ts=f"2026-05-19T00:00:0{i}+00:00") for i in range(5)]
    httpx_mock.add_response(method="GET", text="\n".join(lines) + "\n")

    limits = _default_limits(max_lines=2, max_bytes=1_000_000)
    async with httpx.AsyncClient() as http:
        client = _make_client(http, limits=limits)
        result = await client.query(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert result.truncated is True
    assert len(result.lines) == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_query_truncation_by_max_bytes(httpx_mock: HTTPXMock) -> None:
    """max_bytes cap: large lines trigger truncated=True with fewer lines than input."""
    # Each NDJSON line is ~100 bytes; set max_bytes=50 to hit the cap after 0 parsed lines
    big_msg = "x" * 80
    lines = [_ndjson_line(msg=big_msg) for _ in range(5)]
    httpx_mock.add_response(method="GET", text="\n".join(lines) + "\n")

    limits = _default_limits(max_lines=1000, max_bytes=50)
    async with httpx.AsyncClient() as http:
        client = _make_client(http, limits=limits)
        result = await client.query(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert result.truncated is True
    assert len(result.lines) < 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_query_exactly_at_max_lines_is_not_truncated(httpx_mock: HTTPXMock) -> None:
    """Exactly max_lines=2 lines returned → truncated=False (the +1 sentinel is not present)."""
    # The client sends limit=max_lines+1; if VL returns exactly max_lines, not truncated
    lines = [_ndjson_line(msg=f"line-{i}") for i in range(2)]
    httpx_mock.add_response(method="GET", text="\n".join(lines) + "\n")

    limits = _default_limits(max_lines=2, max_bytes=1_000_000)
    async with httpx.AsyncClient() as http:
        client = _make_client(http, limits=limits)
        result = await client.query(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert result.truncated is False
    assert len(result.lines) == 2  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_raises_on_timeout(httpx_mock: HTTPXMock) -> None:
    """HTTP timeout raises VictoriaLogsClientError."""
    httpx_mock.add_exception(httpx.TimeoutException("timed out"))

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError):
            await client.query(
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T01:00:00+00:00",
            )


@pytest.mark.asyncio
async def test_query_raises_on_connect_error(httpx_mock: HTTPXMock) -> None:
    """Transport error (ConnectError) raises VictoriaLogsClientError."""
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError):
            await client.query(
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T01:00:00+00:00",
            )


@pytest.mark.asyncio
async def test_query_raises_on_non_200_status(httpx_mock: HTTPXMock) -> None:
    """Non-200 HTTP status raises VictoriaLogsClientError."""
    httpx_mock.add_response(status_code=500, text="internal server error body")

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError) as exc_info:
            await client.query(
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T01:00:00+00:00",
            )

    # SECURITY: error message must NOT contain the response body text
    assert "internal server error body" not in str(exc_info.value)
    assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_query_raises_on_502_status(httpx_mock: HTTPXMock) -> None:
    """502 Bad Gateway raises VictoriaLogsClientError."""
    httpx_mock.add_response(status_code=502, text="bad gateway")

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError):
            await client.query(
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T01:00:00+00:00",
            )


# ---------------------------------------------------------------------------
# Malformed / edge-case NDJSON lines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_skips_blank_lines(httpx_mock: HTTPXMock) -> None:
    """Blank lines in the NDJSON response are silently skipped."""
    good_line = _ndjson_line(msg="kept")
    httpx_mock.add_response(method="GET", text=f"\n{good_line}\n\n")

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        result = await client.query(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert len(result.lines) == 1
    assert result.lines[0].message == "kept"


@pytest.mark.asyncio
async def test_query_skips_malformed_json_lines(httpx_mock: HTTPXMock) -> None:
    """Lines that are not valid JSON are silently skipped."""
    good_line = _ndjson_line(msg="ok")
    httpx_mock.add_response(method="GET", text=f"not-json\n{good_line}\n")

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        result = await client.query(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert len(result.lines) == 1
    assert result.lines[0].message == "ok"


@pytest.mark.asyncio
async def test_query_skips_non_dict_json_lines(httpx_mock: HTTPXMock) -> None:
    """NDJSON lines that parse as non-dict (e.g. arrays) are silently skipped."""
    good_line = _ndjson_line(msg="kept")
    httpx_mock.add_response(method="GET", text=f"[1, 2, 3]\n{good_line}\n")

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        result = await client.query(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert len(result.lines) == 1
    assert result.lines[0].message == "kept"


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_strips_trailing_slash_from_vl_url(httpx_mock: HTTPXMock) -> None:
    """vl_url with trailing slash is normalized (no double slash in request path)."""
    httpx_mock.add_response(method="GET", text="")

    async with httpx.AsyncClient() as http:
        client = VictoriaLogsClient(
            vl_url="http://vl-test:9428/",
            http_client=http,
            limits=_default_limits(),
        )
        result = await client.query(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert result.truncated is False


# ---------------------------------------------------------------------------
# Request parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_sends_limit_as_max_lines_plus_one(httpx_mock: HTTPXMock) -> None:
    """The HTTP request sends limit=max_lines+1 so truncation is detectable."""
    httpx_mock.add_response(method="GET", text="")

    limits = _default_limits(max_lines=5)
    async with httpx.AsyncClient() as http:
        client = _make_client(http, limits=limits)
        await client.query(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    sent_limit = requests[0].url.params["limit"]
    assert sent_limit == "6"  # max_lines+1 = 5+1


# ---------------------------------------------------------------------------
# BUG-1 regression: backslash escaping in _logsql_quote_phrase / build_bmode_query
# ---------------------------------------------------------------------------


def test_build_bmode_query_escapes_backslash() -> None:
    """Regression: commands with literal backslashes (e.g. \\!) produce valid LogsQL.

    Before the fix, build_bmode_query embedded the raw canonical key without
    escaping backslashes, producing an invalid quoted phrase that VictoriaLogs
    rejected with HTTP 400.
    """
    command = r"test -x /x -a \! -d /y"
    result = build_bmode_query(command)

    # Must be a quoted phrase
    assert result.startswith('"')
    assert result.endswith('"')

    # Derive what canonical_log_key produces from the command
    key = canonical_log_key(command)

    # Every backslash in the canonical key must be doubled in the output
    inner = result[1:-1]
    # Count literal backslashes in key vs escaped backslashes in inner
    key_backslash_count = key.count("\\")
    assert key_backslash_count > 0, "test precondition: command must contain a backslash"

    # In the escaped inner string, each original backslash becomes \\
    # Verify no lone backslash remains before a non-backslash/non-quote char
    # by checking the key's \! sequence is represented as \\! in inner
    assert r"\\!" in inner, "backslash before ! must be escaped to \\\\ in the phrase"
    # And the bare \! must not appear un-escaped
    # (find \! that is not preceded by another \)
    i = 0
    while i < len(inner):
        if inner[i] == "\\" and i + 1 < len(inner) and inner[i + 1] != "\\" and inner[i + 1] != '"':
            raise AssertionError(
                f"Found un-escaped backslash before {inner[i + 1]!r} at position {i} in: {inner!r}"
            )
        if inner[i] == "\\":
            i += 2  # skip the pair
        else:
            i += 1


def test_build_bmode_query_certbot_realworld() -> None:
    """Regression: real certbot cron command with \\! is properly quoted for LogsQL."""
    command = (
        r"test -x /usr/bin/certbot -a \! -d /run/systemd/system"
        r" && perl -e 'sleep int(rand(43200))' && certbot -q renew"
    )
    result = build_bmode_query(command)

    # Must be a properly framed quoted phrase
    assert result.startswith('"')
    assert result.endswith('"')

    inner = result[1:-1]

    # Every literal backslash must be doubled (\\! → \\\\! in Python repr → \\! in the string)
    assert r"\\!" in inner, r"\\! must appear escaped as \\\\! in the phrase inner"

    # Single quotes are NOT LogsQL-significant inside a quoted phrase — must be literal
    assert "'" in inner, "single quotes must pass through unescaped"

    # No bare backslash should precede a char that is neither \ nor "
    i = 0
    while i < len(inner):
        if inner[i] == "\\" and i + 1 < len(inner) and inner[i + 1] != "\\" and inner[i + 1] != '"':
            raise AssertionError(
                f"Bare backslash before {inner[i + 1]!r} at position {i} in: {inner!r}"
            )
        if inner[i] == "\\":
            i += 2
        else:
            i += 1


def test_logsql_quote_phrase_escape_order() -> None:
    """logsql_quote_phrase escapes backslash BEFORE double-quote (order matters).

    Input has one backslash followed by one double-quote: \\"
    If backslash is escaped first:  \\ → \\\\, then " → \\"  → result inner: \\\\\\"
    If quote is escaped first:      " → \\"  then \\ → \\\\  → result inner: \\\\\\"  (same!)
    BUT if a backslash PRECEDES the escape-char marker it matters:
    Use input containing BOTH — a single char sequence backslash+quote: \\"
    (len 2 in Python: '\\\"')
    Expected: each char is escaped independently: \\ then \\"
    So inner of output should be: \\\\\\"  (four backslash chars then backslash-quote)
    Verify via explicit construction.
    """
    # Input: one backslash + one double-quote  (Python: '\\' + '"' = 2 chars)
    inp = '\\"'
    result = logsql_quote_phrase(inp)

    # Expected inner: \\ + \"  (backslash escaped to \\, then quote escaped to \")
    # So result = '"' + '\\\\' + '\\"' + '"'
    expected = '"' + "\\\\" + '\\"' + '"'
    assert result == expected, (
        f"Expected {expected!r}, got {result!r}. "
        "Backslash must be escaped before quote to avoid double-escaping."
    )


@pytest.mark.asyncio
async def test_query_max_bytes_exact_boundary(httpx_mock: HTTPXMock) -> None:
    """Test exact max_bytes boundary: exactly at limit is not truncated; one byte short is."""
    # Construct a simple line with known byte size:
    # {"_stream_id":"s","_msg":"x","_time":"2026-05-19T20:00:00Z"}\n
    # This is approximately 56 bytes. We'll compute exact size.
    line_template = _ndjson_line(
        stream_id="s",
        msg="x",
        ts="2026-05-19T20:00:00Z",
    )
    line_bytes = len(line_template.encode("utf-8"))

    # Test 1: exactly max_bytes = N * line_bytes (10 lines = exactly at cap)
    n_lines = 10
    exact_max_bytes = n_lines * line_bytes
    lines_exact = [line_template for _ in range(n_lines)]
    response_text_exact = "\n".join(lines_exact) + "\n"

    httpx_mock.add_response(method="GET", text=response_text_exact)

    limits_exact = _default_limits(max_lines=1000, max_bytes=exact_max_bytes)
    async with httpx.AsyncClient() as http:
        client = _make_client(http, limits=limits_exact)
        result_exact = await client.query(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert len(result_exact.lines) == n_lines
    assert result_exact.truncated is False

    # Test 2: one byte short of max_bytes (should truncate at N-1 lines)
    one_short_max_bytes = exact_max_bytes - 1
    lines_short = [line_template for _ in range(n_lines)]
    response_text_short = "\n".join(lines_short) + "\n"

    httpx_mock.add_response(method="GET", text=response_text_short)

    limits_short = _default_limits(max_lines=1000, max_bytes=one_short_max_bytes)
    async with httpx.AsyncClient() as http:
        client = _make_client(http, limits=limits_short)
        result_short = await client.query(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )

    assert len(result_short.lines) == n_lines - 1
    assert result_short.truncated is True


# ---------------------------------------------------------------------------
# field_names (STAGE-004-018)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_field_names_parses_values(httpx_mock: HTTPXMock) -> None:
    """field_names parses {"values":[{value,hits}]} into (name, hits) pairs."""
    httpx_mock.add_response(
        method="GET",
        json={"values": [{"value": "_msg", "hits": 100}, {"value": "level", "hits": 87}]},
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        pairs = await client.field_names(
            expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00"
        )
    assert pairs == [("_msg", 100), ("level", 87)]


@pytest.mark.asyncio
async def test_field_names_sends_request_to_field_names_path(httpx_mock: HTTPXMock) -> None:
    """The GET hits /select/logsql/field_names with query/start/end params."""
    httpx_mock.add_response(method="GET", json={"values": []})
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        await client.field_names(
            expr="service:nginx",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
        )
    req = httpx_mock.get_requests()[0]
    assert req.url.path == "/select/logsql/field_names"
    assert req.url.params["query"] == "service:nginx"
    assert req.url.params["start"] == "2026-05-19T00:00:00+00:00"
    assert req.url.params["end"] == "2026-05-19T01:00:00+00:00"


@pytest.mark.asyncio
async def test_field_names_skips_malformed_rows(httpx_mock: HTTPXMock) -> None:
    """Rows missing value, with non-string value, or non-int hits are skipped."""
    httpx_mock.add_response(
        method="GET",
        json={
            "values": [
                {"value": "ok", "hits": 5},
                {"hits": 3},  # missing value
                {"value": 42, "hits": 1},  # non-string value
                {"value": "bad", "hits": "abc"},  # non-int hits
                [1, 2],  # non-dict row
            ]
        },
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        pairs = await client.field_names(
            expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00"
        )
    assert pairs == [("ok", 5)]


@pytest.mark.asyncio
async def test_field_names_non_object_body_returns_empty(httpx_mock: HTTPXMock) -> None:
    """A JSON array (non-object) top level returns []."""
    httpx_mock.add_response(method="GET", text="[1,2,3]")
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        pairs = await client.field_names(
            expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00"
        )
    assert pairs == []


@pytest.mark.asyncio
async def test_field_names_unparseable_body_returns_empty(httpx_mock: HTTPXMock) -> None:
    """Non-JSON body returns []."""
    httpx_mock.add_response(method="GET", text="not-json")
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        pairs = await client.field_names(
            expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00"
        )
    assert pairs == []


@pytest.mark.asyncio
async def test_field_names_missing_values_key_returns_empty(httpx_mock: HTTPXMock) -> None:
    """Object without a list `values` key returns []."""
    httpx_mock.add_response(method="GET", json={"other": 1})
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        pairs = await client.field_names(
            expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00"
        )
    assert pairs == []


@pytest.mark.asyncio
async def test_field_names_raises_on_non_200(httpx_mock: HTTPXMock) -> None:
    """Non-200 raises VictoriaLogsClientError; body excluded, status included."""
    httpx_mock.add_response(method="GET", status_code=500, text="secret body")
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError) as exc_info:
            await client.field_names(
                expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00"
            )
    assert "secret body" not in str(exc_info.value)
    assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_field_names_raises_on_timeout(httpx_mock: HTTPXMock) -> None:
    """Timeout raises VictoriaLogsClientError."""
    httpx_mock.add_exception(httpx.TimeoutException("timed out"))
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError):
            await client.field_names(
                expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00"
            )


@pytest.mark.asyncio
async def test_field_names_raises_on_connect_error(httpx_mock: HTTPXMock) -> None:
    """Transport error raises VictoriaLogsClientError."""
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError):
            await client.field_names(
                expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00"
            )


# ---------------------------------------------------------------------------
# hits (STAGE-004-019)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hits_parses_per_severity_series(httpx_mock: HTTPXMock) -> None:
    """hits parses {"hits":[{fields,timestamps,values}]} into HitsSeries list."""
    httpx_mock.add_response(
        method="GET",
        json={
            "hits": [
                {
                    "fields": {"severity": "error"},
                    "timestamps": ["2026-05-19T00:00:00Z", "2026-05-19T00:01:00Z"],
                    "values": [3, 5],
                    "total": 8,
                },
                {
                    "fields": {"severity": "info"},
                    "timestamps": ["2026-05-19T00:00:00Z"],
                    "values": [10],
                    "total": 10,
                },
            ]
        },
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        series = await client.hits(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
            step="60000ms",
        )
    assert len(series) == 2  # noqa: PLR2004
    assert series[0] == HitsSeries(
        field_value="error",
        timestamps=["2026-05-19T00:00:00Z", "2026-05-19T00:01:00Z"],
        counts=[3, 5],
    )
    assert series[1].field_value == "info"
    assert series[1].counts == [10]


@pytest.mark.asyncio
async def test_hits_sends_request_to_hits_path_with_field(httpx_mock: HTTPXMock) -> None:
    """GET hits /select/logsql/hits with query/start/end/step/field params."""
    httpx_mock.add_response(method="GET", json={"hits": []})
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        await client.hits(
            expr="service:nginx",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
            step="30000ms",
            field="severity",
        )
    req = httpx_mock.get_requests()[0]
    assert req.url.path == "/select/logsql/hits"
    assert req.url.params["query"] == "service:nginx"
    assert req.url.params["start"] == "2026-05-19T00:00:00+00:00"
    assert req.url.params["end"] == "2026-05-19T01:00:00+00:00"
    assert req.url.params["step"] == "30000ms"
    assert req.url.params["field"] == "severity"


@pytest.mark.asyncio
async def test_hits_total_series_no_fields_key(httpx_mock: HTTPXMock) -> None:
    """A fields:{} (un-grouped total) series yields field_value None."""
    httpx_mock.add_response(
        method="GET",
        json={"hits": [{"fields": {}, "timestamps": ["2026-05-19T00:00:00Z"], "values": [7]}]},
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        series = await client.hits(
            expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00", step="1m"
        )
    assert series[0].field_value is None
    assert series[0].counts == [7]


@pytest.mark.asyncio
async def test_hits_skips_malformed_rows(httpx_mock: HTTPXMock) -> None:
    """Rows missing timestamps/values lists, or non-dict rows, are skipped;
    non-int values coerce to 0; mismatched-length lists zip to the shorter."""
    httpx_mock.add_response(
        method="GET",
        json={
            "hits": [
                {"fields": {"severity": "warn"}, "timestamps": ["t1", "t2"], "values": ["bad", 4]},
                {"fields": {"severity": "x"}, "values": [1]},  # no timestamps list
                [1, 2, 3],  # non-dict row
                {"fields": {"severity": "info"}, "timestamps": ["a", "b", "c"], "values": [9]},
            ]
        },
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        series = await client.hits(
            expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00", step="1m"
        )
    # row 0 kept (count "bad"->0, 4 kept); row 1 skipped; row 2 skipped;
    # row 3 kept (zip to shorter -> 1 pair).
    assert len(series) == 2  # noqa: PLR2004
    assert series[0].field_value == "warn"
    assert series[0].counts == [0, 4]
    assert series[1].field_value == "info"
    assert series[1].timestamps == ["a"]
    assert series[1].counts == [9]


@pytest.mark.asyncio
async def test_hits_skips_non_string_timestamps_and_non_dict_fields(
    httpx_mock: HTTPXMock,
) -> None:
    """_parse_hits tolerates a non-dict 'fields' (field_value stays None) and skips
    a non-string timestamp entry (continue), keeping the valid (ts, count) pair."""
    httpx_mock.add_response(
        method="GET",
        json={
            "hits": [
                {
                    "fields": ["not", "a", "dict"],
                    "timestamps": [99999, "2026-05-19T00:00:00Z"],
                    "values": [1, 2],
                }
            ]
        },
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        series = await client.hits(
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
            step="1m",
        )
    assert len(series) == 1
    assert series[0].field_value is None
    assert series[0].timestamps == ["2026-05-19T00:00:00Z"]
    assert series[0].counts == [2]


@pytest.mark.asyncio
async def test_hits_non_object_body_returns_empty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", text="[1,2,3]")
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        series = await client.hits(
            expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00", step="1m"
        )
    assert series == []


@pytest.mark.asyncio
async def test_hits_unparseable_body_returns_empty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", text="not-json")
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        series = await client.hits(
            expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00", step="1m"
        )
    assert series == []


@pytest.mark.asyncio
async def test_hits_missing_hits_key_returns_empty(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", json={"other": 1})
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        series = await client.hits(
            expr="*", start="2026-05-19T00:00:00+00:00", end="2026-05-19T01:00:00+00:00", step="1m"
        )
    assert series == []


@pytest.mark.asyncio
async def test_hits_raises_on_non_200(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", status_code=500, text="secret body")
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError) as exc_info:
            await client.hits(
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T01:00:00+00:00",
                step="1m",
            )
    assert "secret body" not in str(exc_info.value)
    assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_hits_raises_on_timeout(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.TimeoutException("timed out"))
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError):
            await client.hits(
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T01:00:00+00:00",
                step="1m",
            )


@pytest.mark.asyncio
async def test_hits_raises_on_connect_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError):
            await client.hits(
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T01:00:00+00:00",
                step="1m",
            )


# ---------------------------------------------------------------------------
# stream_query (STAGE-004-020)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_query_skips_blank_lines(httpx_mock: HTTPXMock) -> None:
    """Blank lines in the streaming NDJSON body are skipped (line 231 coverage).

    Mirrors test_query_skips_blank_lines but calls stream_query directly so the
    `continue` branch in that method is exercised.
    """
    good_line = _ndjson_line(msg="kept", ts="2026-05-19T00:00:00+00:00")
    httpx_mock.add_response(method="GET", text=f"\n{good_line}\n\n")

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        results = [
            line
            async for line in client.stream_query(
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T01:00:00+00:00",
                limit=100,
            )
        ]

    assert len(results) == 1
    assert results[0].message == "kept"


@pytest.mark.asyncio
async def test_stream_query_skips_malformed_json_lines(httpx_mock: HTTPXMock) -> None:
    """Malformed NDJSON lines returned by _parse_one as None are silently skipped
    (branch 233->227 coverage: `if parsed is not None` evaluates False).

    Mirrors test_query_skips_malformed_json_lines but calls stream_query directly.
    """
    good_line = _ndjson_line(msg="ok", ts="2026-05-19T00:00:00+00:00")
    httpx_mock.add_response(method="GET", text=f"not-json\n{good_line}\n")

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        results = [
            line
            async for line in client.stream_query(
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T01:00:00+00:00",
                limit=100,
            )
        ]

    assert len(results) == 1
    assert results[0].message == "ok"


@pytest.mark.asyncio
async def test_stream_query_raises_on_transport_error(httpx_mock: HTTPXMock) -> None:
    """Transport error during stream_query raises VictoriaLogsClientError.

    pytest_httpx cannot inject a true mid-stream (post-200 headers) failure;
    the exception fires at the request level.  This test pins that the
    except (TimeoutException, RequestError) block in stream_query maps any
    httpx transport failure to VictoriaLogsClientError.
    """
    httpx_mock.add_exception(httpx.ConnectError("refused"))

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        with pytest.raises(VictoriaLogsClientError):
            async for _ in client.stream_query(
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T01:00:00+00:00",
                limit=100,
            ):
                pass  # pragma: no cover


@pytest.mark.asyncio
async def test_stream_query_sends_limit_param(httpx_mock: HTTPXMock) -> None:
    """stream_query sends the requested limit verbatim as the VL `limit` param."""
    httpx_mock.add_response(method="GET", text="")

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        results = [
            line
            async for line in client.stream_query(
                expr="*",
                start="2026-05-19T00:00:00+00:00",
                end="2026-05-19T01:00:00+00:00",
                limit=77,
            )
        ]

    assert results == []
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    sent_limit = requests[0].url.params["limit"]
    assert sent_limit == "77"
