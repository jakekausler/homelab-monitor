"""Unit tests for kernel.logs.fields (STAGE-004-018)."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.api.schemas import LogsFieldsResponse
from homelab_monitor.kernel.config import VlQueryLimits
from homelab_monitor.kernel.logs.fields import (
    FieldsCache,
    fetch_fields,
    infer_type_hint,
)
from homelab_monitor.kernel.logs.victorialogs_client import VictoriaLogsClient

_VL_URL = "http://vl-test:9428"


def _make_client(http: httpx.AsyncClient) -> VictoriaLogsClient:
    return VictoriaLogsClient(
        vl_url=_VL_URL,
        http_client=http,
        limits=VlQueryLimits(max_lines=200, max_bytes=1_000_000, timeout_seconds=5.0),
    )


# --- infer_type_hint -------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], "unknown"),
        (["1", "2", "3"], "numeric"),
        (["1.5", "2"], "numeric"),
        (["true", "FALSE", "True"], "bool"),
        (['{"a":1}', "{}"], "object"),
        (["[1,2]", "[]"], "array"),
        (["hello", "world"], "string"),
        (["1", "hello"], "mixed"),
        (["true", "5"], "mixed"),
        ([""], "string"),
    ],
)
def test_infer_type_hint(values: list[str], expected: str) -> None:
    assert infer_type_hint(values) == expected


def test_infer_numeric_rejects_empty_string() -> None:
    assert infer_type_hint(["", ""]) == "string"


def test_infer_numeric_accepts_float_words() -> None:
    # float("inf"), float("nan") parse — treated as numeric tokens.
    assert infer_type_hint(["inf", "nan"]) == "numeric"


# --- fetch_fields ----------------------------------------------------------


def _field_names_json(rows: list[tuple[str, int]]) -> dict[str, object]:
    return {"values": [{"value": n, "hits": h} for n, h in rows]}


@pytest.mark.asyncio
async def test_fetch_fields_happy_path_coverage_and_sort(httpx_mock: HTTPXMock) -> None:
    """Coverage = hits/_msg-hits; sorted DESC by coverage; _msg/_time excluded."""
    import re  # noqa: PLC0415

    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/field_names.*"),
        method="GET",
        json=_field_names_json([("_msg", 100), ("level", 100), ("user_id", 45), ("_time", 100)]),
    )
    ndjson = (
        '{"_stream_id":"s","_msg":"m","_time":"t","level":"error","user_id":"42"}\n'
        '{"_stream_id":"s","_msg":"m","_time":"t","level":"warn","user_id":"42"}\n'
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text=ndjson,
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        resp = await fetch_fields(
            client=client,
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
            sample_n=200,
        )
    assert isinstance(resp, LogsFieldsResponse)
    names = [f.name for f in resp.fields]
    assert "_msg" not in names and "_time" not in names
    # level coverage 1.0 sorts before user_id 0.45
    assert names == ["level", "user_id"]
    level = resp.fields[0]
    assert level.coverage == 1.0
    assert level.type_hint == "string"
    assert set(level.sample_values) == {"error", "warn"}
    user = resp.fields[1]
    assert user.coverage == pytest.approx(0.45)  # pyright: ignore[reportUnknownMemberType]
    assert user.sample_values == ["42"]  # deduped
    assert user.type_hint == "numeric"
    assert resp.sampled_lines == 2  # noqa: PLR2004
    assert resp.truncated is False


@pytest.mark.asyncio
async def test_fetch_fields_zero_total_no_div_by_zero(httpx_mock: HTTPXMock) -> None:
    """No _msg entry (total 0) → coverage 0.0, no ZeroDivisionError."""
    import re  # noqa: PLC0415

    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/field_names.*"),
        method="GET",
        json=_field_names_json([("level", 5)]),  # no _msg row
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text="",
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        resp = await fetch_fields(
            client=client,
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
            sample_n=200,
        )
    assert resp.fields[0].coverage == 0.0
    assert resp.fields[0].sample_values == []
    assert resp.fields[0].type_hint == "unknown"


@pytest.mark.asyncio
async def test_fetch_fields_coverage_clamped_to_one(httpx_mock: HTTPXMock) -> None:
    """A field with hits > _msg total clamps coverage to 1.0."""
    import re  # noqa: PLC0415

    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/field_names.*"),
        method="GET",
        json=_field_names_json([("_msg", 10), ("weird", 12)]),
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text="",
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        resp = await fetch_fields(
            client=client,
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
            sample_n=200,
        )
    assert resp.fields[0].coverage == 1.0


@pytest.mark.asyncio
async def test_fetch_fields_sample_value_cap_k(httpx_mock: HTTPXMock) -> None:
    """At most k_values distinct sample values, first-seen order."""
    import re  # noqa: PLC0415

    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/field_names.*"),
        method="GET",
        json=_field_names_json([("_msg", 10), ("tag", 10)]),
    )
    lines = "".join(f'{{"_stream_id":"s","_msg":"m","_time":"t","tag":"v{i}"}}\n' for i in range(8))
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text=lines,
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        resp = await fetch_fields(
            client=client,
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
            sample_n=200,
            k_values=3,
        )
    tag = next(f for f in resp.fields if f.name == "tag")
    assert tag.sample_values == ["v0", "v1", "v2"]


@pytest.mark.asyncio
async def test_fetch_fields_truncated_when_sample_caps(httpx_mock: HTTPXMock) -> None:
    """sample_n smaller than available lines → truncated True, sampled_lines==sample_n."""
    import re  # noqa: PLC0415

    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/field_names.*"),
        method="GET",
        json=_field_names_json([("_msg", 100), ("level", 100)]),
    )
    lines = "".join(
        f'{{"_stream_id":"s","_msg":"m","_time":"t","level":"info{i}"}}\n' for i in range(5)
    )
    httpx_mock.add_response(
        url=re.compile(r"http://vl-test:9428/select/logsql/query.*"),
        method="GET",
        text=lines,
    )
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        resp = await fetch_fields(
            client=client,
            expr="*",
            start="2026-05-19T00:00:00+00:00",
            end="2026-05-19T01:00:00+00:00",
            sample_n=2,
        )
    assert resp.truncated is True
    assert resp.sampled_lines == 2  # noqa: PLR2004


# --- FieldsCache -----------------------------------------------------------


def _empty_resp() -> LogsFieldsResponse:
    return LogsFieldsResponse(fields=[], sampled_lines=0, truncated=False)


def test_fields_cache_hit_within_ttl() -> None:
    now = [0.0]
    cache = FieldsCache(ttl_seconds=30, clock=lambda: now[0])
    key = FieldsCache.make_key(expr="*", start="a", end="b", sample_n=200)
    val = _empty_resp()
    cache.put(key, val)
    assert cache.get(key) is val


def test_fields_cache_miss_after_ttl() -> None:
    now = [0.0]
    cache = FieldsCache(ttl_seconds=30, clock=lambda: now[0])
    key = FieldsCache.make_key(expr="*", start="a", end="b", sample_n=200)
    cache.put(key, _empty_resp())
    now[0] = 30.0
    assert cache.get(key) is None
    assert cache.get(key) is None  # evicted; still None


def test_fields_cache_miss_on_missing_key() -> None:
    cache = FieldsCache()
    key = FieldsCache.make_key(expr="*", start="a", end="b", sample_n=200)
    assert cache.get(key) is None


def test_fields_cache_key_hashes_expr() -> None:
    """Different exprs → different keys; same expr → same key."""
    k1 = FieldsCache.make_key(expr="a", start="s", end="e", sample_n=200)
    k2 = FieldsCache.make_key(expr="b", start="s", end="e", sample_n=200)
    k3 = FieldsCache.make_key(expr="a", start="s", end="e", sample_n=200)
    assert k1 != k2
    assert k1 == k3
    assert k1[0] != "a"  # expr is hashed, not stored raw
