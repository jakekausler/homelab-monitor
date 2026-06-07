"""Tests for GET /api/logs/window (STAGE-004-031A)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.api.dependencies import get_log_window_fetcher
from homelab_monitor.kernel.logs.log_window_fetcher import LogWindowResult
from homelab_monitor.kernel.logs.models import LogLine

_WS = datetime(2026, 5, 7, 0, 0, 0, tzinfo=UTC)
_WE = datetime(2026, 5, 7, 1, 0, 0, tzinfo=UTC)
_QA = datetime(2026, 5, 7, 0, 30, 0, tzinfo=UTC)


def _line(ts: str, msg: str, stream: str = "stdout", service: str | None = "svc") -> LogLine:
    return LogLine(
        timestamp=ts,
        message=msg,
        stream=stream,
        severity=None,
        host=None,
        service=service,
        fields={},
    )


class _FakeFetcher:
    """Records each fetch() call's (window_before_s, window_after_s) and returns
    a queued LogWindowResult per call (before-side first, after-side second)."""

    def __init__(self, results: list[LogWindowResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[int, int, int, str]] = []

    async def fetch(
        self,
        logs_ql: str,
        anchor_ts: datetime,
        window_before_s: int = 60,
        window_after_s: int = 60,
        limit: int = 200,
    ) -> LogWindowResult:
        self.calls.append((window_before_s, window_after_s, limit, logs_ql))
        return self._results.pop(0)


def _result(
    lines: list[LogLine], truncated: bool = False, degraded: bool = False
) -> LogWindowResult:
    return LogWindowResult(
        lines=lines,
        truncated=truncated,
        degraded=degraded,
        window_start=_WS,
        window_end=_WE,
        queried_at=_QA,
    )


def _install(client: AsyncClient, fetcher: _FakeFetcher) -> FastAPI:
    app = cast(FastAPI, client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    app.dependency_overrides[get_log_window_fetcher] = lambda: fetcher
    return app


@pytest.mark.asyncio
async def test_window_requires_session(authenticated_client: AsyncClient) -> None:
    """Anon client → 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_window_both_sides_populated_merges_dedups_sorts(
    authenticated_client: AsyncClient,
) -> None:
    """Before and after both populated; dedup exact match; sort ascending by timestamp."""
    fetcher = _FakeFetcher(
        [
            _result([_line("2026-05-07T00:00:10Z", "B1"), _line("2026-05-07T00:00:05Z", "A")]),
            _result([_line("2026-05-07T00:00:05Z", "A"), _line("2026-05-07T00:00:20Z", "C1")]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        messages = [ln["message"] for ln in body["lines"]]
        assert messages == ["A", "B1", "C1"]
        assert len(body["lines"]) == 3  # noqa: PLR2004
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_before_empty(authenticated_client: AsyncClient) -> None:
    """Before empty; after has 2 lines."""
    fetcher = _FakeFetcher(
        [
            _result([], truncated=True),
            _result([_line("2026-05-07T00:00:15Z", "B"), _line("2026-05-07T00:00:20Z", "C")]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert len(body["lines"]) == 2  # noqa: PLR2004
        assert body["truncated_before"] is True
        assert body["truncated_after"] is False
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_after_empty(authenticated_client: AsyncClient) -> None:
    """Before has 2; after empty."""
    fetcher = _FakeFetcher(
        [
            _result([_line("2026-05-07T00:00:00Z", "A"), _line("2026-05-07T00:00:05Z", "B")]),
            _result([], truncated=False),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert len(body["lines"]) == 2  # noqa: PLR2004
        assert body["truncated_before"] is False
        assert body["truncated_after"] is False
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_both_empty(authenticated_client: AsyncClient) -> None:
    """Both before and after empty → lines == [], anchor_index is None."""
    fetcher = _FakeFetcher(
        [
            _result([]),
            _result([]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["lines"] == []
        assert body["anchor_index"] is None
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_degraded_one_side(authenticated_client: AsyncClient) -> None:
    """Before degraded; after has 1 line → response degraded=True, status 200."""
    fetcher = _FakeFetcher(
        [
            _result([], degraded=True),
            _result([_line("2026-05-07T00:00:20Z", "C")]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["degraded"] is True
        assert len(body["lines"]) == 1
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_degraded_both_sides(authenticated_client: AsyncClient) -> None:
    """Both degraded=True → response degraded=True, lines=[]."""
    fetcher = _FakeFetcher(
        [
            _result([], degraded=True),
            _result([], degraded=True),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["degraded"] is True
        assert body["lines"] == []
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_truncated_flags(authenticated_client: AsyncClient) -> None:
    """Before truncated=True, after=False → flags reflected in response."""
    fetcher = _FakeFetcher(
        [
            _result([_line("2026-05-07T00:00:05Z", "A")], truncated=True),
            _result([_line("2026-05-07T00:00:15Z", "B")], truncated=False),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["truncated_before"] is True
        assert body["truncated_after"] is False
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_truncated_flags_swapped(authenticated_client: AsyncClient) -> None:
    """Before truncated=False, after=True → covers both flags."""
    fetcher = _FakeFetcher(
        [
            _result([_line("2026-05-07T00:00:05Z", "A")], truncated=False),
            _result([_line("2026-05-07T00:00:15Z", "B")], truncated=True),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["truncated_before"] is False
        assert body["truncated_after"] is True
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_anchor_located_exact(authenticated_client: AsyncClient) -> None:
    """Exact match on (ts, stream, message) → anchor_index = its position."""
    fetcher = _FakeFetcher(
        [
            _result([_line("2026-05-07T00:00:05Z", "A")]),
            _result([_line("2026-05-07T00:00:10Z", "ANCHOR"), _line("2026-05-07T00:00:15Z", "C")]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={
                "anchor_ts": "2026-05-07T00:00:10Z",
                "anchor_stream": "stdout",
                "anchor_message": "ANCHOR",
            },
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["anchor_index"] == 1
        assert body["lines"][1]["message"] == "ANCHOR"
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_anchor_insertion_point(authenticated_client: AsyncClient) -> None:
    """No exact match; lines straddle anchor_ts → insertion point."""
    fetcher = _FakeFetcher(
        [
            _result([_line("2026-05-07T00:00:05Z", "A")]),
            _result([_line("2026-05-07T00:00:15Z", "C")]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["anchor_index"] == 1
        assert body["lines"][1]["timestamp"] == "2026-05-07T00:00:15Z"
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_anchor_not_located_all_before(authenticated_client: AsyncClient) -> None:
    """All lines strictly before anchor_ts, no exact match → anchor_index is None."""
    fetcher = _FakeFetcher(
        [
            _result([_line("2026-05-07T00:00:05Z", "A"), _line("2026-05-07T00:00:08Z", "B")]),
            _result([]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["anchor_index"] is None
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_anchor_exact_miss_falls_back_to_insertion(
    authenticated_client: AsyncClient,
) -> None:
    """Exact match params provided but NO match; falls back to insertion point."""
    fetcher = _FakeFetcher(
        [
            _result([_line("2026-05-07T00:00:05Z", "A")]),
            _result([_line("2026-05-07T00:00:15Z", "C")]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={
                "anchor_ts": "2026-05-07T00:00:10Z",
                "anchor_stream": "stdout",
                "anchor_message": "NOTFOUND",
            },
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["anchor_index"] == 1
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_two_sided_fetch_args(authenticated_client: AsyncClient) -> None:
    """Verifies two fetches: before with window_after_s=0, after with window_before_s=0."""
    fetcher = _FakeFetcher(
        [
            _result([]),
            _result([]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={
                "anchor_ts": "2026-05-07T00:00:10Z",
                "before": 75,
                "after": 125,
            },
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert fetcher.calls[0][0] == 1800  # noqa: PLR2004  # window_before_s
        assert fetcher.calls[0][1] == 0  # window_after_s
        assert fetcher.calls[0][2] == 75  # noqa: PLR2004  # limit
        assert fetcher.calls[1][0] == 0  # window_before_s
        assert fetcher.calls[1][1] == 1800  # noqa: PLR2004  # window_after_s
        assert fetcher.calls[1][2] == 125  # noqa: PLR2004  # limit
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_scope_all_services(authenticated_client: AsyncClient) -> None:
    """No service param → logs_ql unchanged."""
    fetcher = _FakeFetcher(
        [
            _result([]),
            _result([]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={
                "anchor_ts": "2026-05-07T00:00:10Z",
                "expr": "level:error",
            },
        )
        assert resp.status_code == 200  # noqa: PLR2004
        assert fetcher.calls[0][3] == "level:error"
        assert fetcher.calls[1][3] == "level:error"
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_scope_only_service(authenticated_client: AsyncClient) -> None:
    """service=nginx&source_type=docker → logs_ql includes service AND source_type."""
    fetcher = _FakeFetcher(
        [
            _result([]),
            _result([]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={
                "anchor_ts": "2026-05-07T00:00:10Z",
                "service": "nginx",
                "source_type": "docker",
                "expr": "*",
            },
        )
        assert resp.status_code == 200  # noqa: PLR2004
        logs_ql = fetcher.calls[0][3]
        assert 'service:"nginx"' in logs_ql
        assert 'source_type:"docker"' in logs_ql
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_scope_service_default_source_type(
    authenticated_client: AsyncClient,
) -> None:
    """service=nginx WITHOUT source_type → default source_type=unknown."""
    fetcher = _FakeFetcher(
        [
            _result([]),
            _result([]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={
                "anchor_ts": "2026-05-07T00:00:10Z",
                "service": "nginx",
                "expr": "*",
            },
        )
        assert resp.status_code == 200  # noqa: PLR2004
        logs_ql = fetcher.calls[0][3]
        assert 'source_type:"unknown"' in logs_ql
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_rejects_long_expr(authenticated_client: AsyncClient) -> None:
    """expr > 4096 chars → 400 invalid_expr."""
    fetcher = _FakeFetcher([])
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={
                "anchor_ts": "2026-05-07T00:00:10Z",
                "expr": "a" * 5000,
            },
        )
        assert resp.status_code == 400  # noqa: PLR2004
        body = resp.json()
        assert body["error"]["code"] == "invalid_expr"
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_rejects_bad_anchor_ts(authenticated_client: AsyncClient) -> None:
    """Bad anchor_ts → 400 invalid_time_format."""
    fetcher = _FakeFetcher([])
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={
                "anchor_ts": "not-a-date",
            },
        )
        assert resp.status_code == 400  # noqa: PLR2004
        body = resp.json()
        assert body["error"]["code"] == "invalid_time_format"
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_before_count_rejected_above_max(authenticated_client: AsyncClient) -> None:
    """before > 500 → 422 (FastAPI le constraint)."""
    fetcher = _FakeFetcher([])
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={
                "anchor_ts": "2026-05-07T00:00:10Z",
                "before": 99999,
            },
        )
        assert resp.status_code == 422  # noqa: PLR2004
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_after_count_rejected_above_max(authenticated_client: AsyncClient) -> None:
    """after > 500 → 422."""
    fetcher = _FakeFetcher([])
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={
                "anchor_ts": "2026-05-07T00:00:10Z",
                "after": 501,
            },
        )
        assert resp.status_code == 422  # noqa: PLR2004
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_response_shape(authenticated_client: AsyncClient) -> None:
    """Response shape: lines, truncated flags, degraded, anchor_index, window bounds, queried_at."""
    fetcher = _FakeFetcher(
        [
            _result([_line("2026-05-07T00:00:05Z", "A")]),
            _result([_line("2026-05-07T00:00:15Z", "B")]),
        ]
    )
    app = _install(authenticated_client, fetcher)
    try:
        resp = await authenticated_client.get(
            "/api/logs/window",
            params={"anchor_ts": "2026-05-07T00:00:10Z"},
        )
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert "lines" in body
        assert "truncated_before" in body
        assert "truncated_after" in body
        assert "degraded" in body
        assert "anchor_index" in body
        assert "window_start" in body
        assert "window_end" in body
        assert "queried_at" in body
        assert isinstance(body["anchor_index"], int | None)
        assert isinstance(body["window_start"], str)
        assert isinstance(body["window_end"], str)
        assert isinstance(body["queried_at"], str)
    finally:
        app.dependency_overrides.pop(get_log_window_fetcher, None)


@pytest.mark.asyncio
async def test_window_real_dependency_resolves_from_app_state(
    authenticated_client: AsyncClient,
) -> None:
    """The real get_log_window_fetcher dependency resolves the app.state singleton.

    No DI override is installed, so the genuine dependency function runs against the
    LogWindowFetcher wired into the test app state (pointed at a dummy VL URL).
    VL is unreachable → the fetcher degrades gracefully → 200 with degraded=true,
    exercising the real dependency body (not a stubbed override).
    """
    resp = await authenticated_client.get(
        "/api/logs/window",
        params={"anchor_ts": "2026-05-07T00:00:10Z"},
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["degraded"] is True
    assert body["lines"] == []
    assert body["anchor_index"] is None
