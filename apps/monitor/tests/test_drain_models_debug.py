"""Tests for the drain models debug API endpoints (STAGE-004-030).

Covers:
  GET /api/logs/signatures/models          (list summaries)
  GET /api/logs/signatures/models/{key}    (detail + templates)
  GET /api/logs/signatures/cycle/last      (last cycle stats)

Route-order regression (literal /models and /cycle/last not swallowed by
  the {template_hash}/{service_key} param route)
Auth: all three require_session → 401 for anon clients.

Project test conventions:
- Framework: pytest-asyncio (asyncio_mode=auto)
- Repo seeding: direct SQL INSERT via SqliteRepository (drain_models columns)
- Consumer injection: construct via _consumer() helper from test_drain_consumer,
  then set on app.state.drain_consumer for 200-path tests.
- 503 paths: default fixture leaves drain_consumer = None.
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.drain_consumer import DrainConsumer

# Re-use the consumer-construction helper and _FakeVlClient from test_drain_consumer.
from tests.test_drain_consumer import (
    _consumer,  # pyright: ignore[reportPrivateUsage]
    _FakeVlClient,  # pyright: ignore[reportPrivateUsage]
)

_MODELS_URL = "/api/logs/signatures/models"
_CYCLE_LAST_URL = "/api/logs/signatures/cycle/last"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_app(client: AsyncClient) -> FastAPI:
    return cast(FastAPI, client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]


def _get_app_repo(client: AsyncClient) -> SqliteRepository:
    app = _get_app(client)
    return cast(SqliteRepository, app.state.repo)  # pyright: ignore[reportAttributeAccessIssue]


def _set_consumer(client: AsyncClient, consumer: DrainConsumer) -> None:
    """Inject a DrainConsumer onto app.state for the 200-path tests."""
    app = _get_app(client)
    app.state.drain_consumer = consumer  # pyright: ignore[reportAttributeAccessIssue]


def _clear_consumer(client: AsyncClient) -> None:
    """Remove consumer from app.state (restores 503 default)."""
    app = _get_app(client)
    app.state.drain_consumer = None  # pyright: ignore[reportAttributeAccessIssue]


async def _insert_drain_model(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    model_key: str,
    snapshot: bytes = b"x",
    line_count: int = 0,
    template_count: int = 0,
    last_processed_ts: int | None = None,
    first_seen_map: str = "{}",
    updated_at: int = 1000,
) -> None:
    """Seed one drain_models row directly via SQL (mirrors test_drain_persistence.py style)."""
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO drain_models "
                "  (model_key, snapshot, line_count, template_count, "
                "   last_processed_ts, first_seen_map, updated_at) "
                "VALUES "
                "  (:mk, :snap, :lc, :tc, :lpts, :fsm, :ua)"
            ),
            {
                "mk": model_key,
                "snap": snapshot,
                "lc": line_count,
                "tc": template_count,
                "lpts": last_processed_ts,
                "fsm": first_seen_map,
                "ua": updated_at,
            },
        )


# ===========================================================================
# GET /api/logs/signatures/models — list
# ===========================================================================


async def test_list_drain_models_503_no_consumer(authenticated_client: AsyncClient) -> None:
    """GET /models returns 503 when drain_consumer is None (drain disabled)."""
    resp = await authenticated_client.get(_MODELS_URL)
    assert resp.status_code == 503  # noqa: PLR2004


async def test_list_drain_models_200_empty(authenticated_client: AsyncClient) -> None:
    """GET /models returns 200 with empty list when drain_models table is empty."""
    repo = _get_app_repo(authenticated_client)
    consumer, *_ = _consumer(repo, _FakeVlClient([]))
    _set_consumer(authenticated_client, consumer)
    try:
        resp = await authenticated_client.get(_MODELS_URL)
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["models"] == []
    finally:
        _clear_consumer(authenticated_client)


async def test_list_drain_models_200_seeded_rows(authenticated_client: AsyncClient) -> None:
    """GET /models returns 200 with all rows, sorted by model_key, with correct fields."""
    repo = _get_app_repo(authenticated_client)
    # Insert two rows; "zz-model" comes before "aa-model" in insert order to test sort.
    await _insert_drain_model(
        repo,
        model_key="zz-svc",
        line_count=10,
        template_count=3,
        last_processed_ts=9000,
        updated_at=5000,
    )
    await _insert_drain_model(
        repo,
        model_key="aa-svc",
        line_count=5,
        template_count=1,
        last_processed_ts=None,
        updated_at=2000,
    )
    consumer, *_ = _consumer(repo, _FakeVlClient([]))
    _set_consumer(authenticated_client, consumer)
    try:
        resp = await authenticated_client.get(_MODELS_URL)
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        models = body["models"]
        assert len(models) == 2  # noqa: PLR2004
        # Sorted by model_key ascending
        assert models[0]["model_key"] == "aa-svc"
        assert models[1]["model_key"] == "zz-svc"
        # Column values for first row
        assert models[0]["line_count"] == 5  # noqa: PLR2004
        assert models[0]["template_count"] == 1
        assert models[0]["last_processed_ts"] is None
        assert models[0]["updated_at"] == 2000  # noqa: PLR2004
        # Column values for second row
        assert models[1]["line_count"] == 10  # noqa: PLR2004
        assert models[1]["template_count"] == 3  # noqa: PLR2004
        assert models[1]["last_processed_ts"] == 9000  # noqa: PLR2004
        assert models[1]["updated_at"] == 5000  # noqa: PLR2004
    finally:
        _clear_consumer(authenticated_client)


async def test_list_drain_models_401_anon(authenticated_client: AsyncClient) -> None:
    """GET /models returns 401 for unauthenticated client."""
    app = _get_app(authenticated_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(_MODELS_URL)
    assert resp.status_code == 401  # noqa: PLR2004


# ===========================================================================
# GET /api/logs/signatures/models/{model_key} — detail
# ===========================================================================


async def test_get_drain_model_503_no_consumer(authenticated_client: AsyncClient) -> None:
    """GET /models/{key} returns 503 when drain_consumer is None."""
    resp = await authenticated_client.get(f"{_MODELS_URL}/some-svc")
    assert resp.status_code == 503  # noqa: PLR2004


async def test_get_drain_model_404_missing_row(authenticated_client: AsyncClient) -> None:
    """GET /models/{key} returns 404 when no drain_models row for that key."""
    repo = _get_app_repo(authenticated_client)
    consumer, *_ = _consumer(repo, _FakeVlClient([]))
    _set_consumer(authenticated_client, consumer)
    try:
        resp = await authenticated_client.get(f"{_MODELS_URL}/nonexistent-model")
        assert resp.status_code == 404  # noqa: PLR2004
        body = resp.json()
        assert body["error"]["code"] == "not_found"
    finally:
        _clear_consumer(authenticated_client)


async def test_get_drain_model_200_corrupt_blob_fallback(
    authenticated_client: AsyncClient,
) -> None:
    """GET /models/{key} 200; corrupt snapshot blob → templates:[] (engine fallback).

    Also exercises a colon-bearing model_key (cron:abc123) to verify single
    path segment handles colons.
    """
    repo = _get_app_repo(authenticated_client)
    colon_key = "cron:abc123"
    await _insert_drain_model(
        repo,
        model_key=colon_key,
        snapshot=b"not-a-real-snapshot",  # corrupt
        line_count=7,
        template_count=2,
        last_processed_ts=8888,
        updated_at=4000,
    )
    consumer, *_ = _consumer(repo, _FakeVlClient([]))
    _set_consumer(authenticated_client, consumer)
    try:
        resp = await authenticated_client.get(f"{_MODELS_URL}/{colon_key}")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["model_key"] == colon_key
        # Summary columns are from the DB (authoritative)
        assert body["summary"]["model_key"] == colon_key
        assert body["summary"]["line_count"] == 7  # noqa: PLR2004
        assert body["summary"]["template_count"] == 2  # noqa: PLR2004
        assert body["summary"]["last_processed_ts"] == 8888  # noqa: PLR2004
        assert body["summary"]["updated_at"] == 4000  # noqa: PLR2004
        # Corrupt blob → engine fallback → templates:[]
        assert body["templates"] == []
    finally:
        _clear_consumer(authenticated_client)


async def test_get_drain_model_401_anon(authenticated_client: AsyncClient) -> None:
    """GET /models/{key} returns 401 for unauthenticated client."""
    app = _get_app(authenticated_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(f"{_MODELS_URL}/svc-x")
    assert resp.status_code == 401  # noqa: PLR2004


# ===========================================================================
# GET /api/logs/signatures/cycle/last
# ===========================================================================


async def test_cycle_last_503_no_consumer(authenticated_client: AsyncClient) -> None:
    """GET /cycle/last returns 503 when drain_consumer is None."""
    resp = await authenticated_client.get(_CYCLE_LAST_URL)
    assert resp.status_code == 503  # noqa: PLR2004


async def test_cycle_last_200_has_run_false_before_any_cycle(
    authenticated_client: AsyncClient,
) -> None:
    """GET /cycle/last returns has_run=False when no cycle has run yet."""
    repo = _get_app_repo(authenticated_client)
    consumer, *_ = _consumer(repo, _FakeVlClient([]))
    _set_consumer(authenticated_client, consumer)
    try:
        resp = await authenticated_client.get(_CYCLE_LAST_URL)
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["has_run"] is False
        assert body["started_at"] is None
        assert body["finished_at"] is None
        assert body["lines_processed"] == 0
        assert body["new_templates"] == 0
        assert body["models_touched"] == 0
        assert body["cycle_status"] is None
    finally:
        _clear_consumer(authenticated_client)


async def test_cycle_last_200_has_run_true_after_run_once(
    authenticated_client: AsyncClient,
) -> None:
    """GET /cycle/last returns has_run=True with cycle stats after run_once (empty feed)."""
    repo = _get_app_repo(authenticated_client)
    consumer, *_ = _consumer(repo, _FakeVlClient([]), ingest_lag_grace_seconds=0)
    _set_consumer(authenticated_client, consumer)
    try:
        await consumer.run_once()
        resp = await authenticated_client.get(_CYCLE_LAST_URL)
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["has_run"] is True
        assert body["cycle_status"] == "ok"
        assert body["lines_processed"] == 0
        assert body["models_touched"] == 0
        assert body["started_at"] is not None
        assert body["finished_at"] is not None
    finally:
        _clear_consumer(authenticated_client)


async def test_cycle_last_200_has_run_true_after_restart(
    authenticated_client: AsyncClient,
) -> None:
    """GET /cycle/last returns has_run=True from persisted result when _last_result is None.

    Simulates a process restart: result was persisted by a prior consumer instance,
    a fresh consumer (with _last_result=None) is injected, endpoint reads from settings.
    """
    repo = _get_app_repo(authenticated_client)
    # Run one cycle with a real consumer to persist the result.
    consumer_a, *_ = _consumer(repo, _FakeVlClient([]), ingest_lag_grace_seconds=0)
    await consumer_a.run_once()

    # Simulate restart: fresh consumer with _last_result=None, same repo.
    consumer_b, *_ = _consumer(repo, _FakeVlClient([]), ingest_lag_grace_seconds=0)
    assert consumer_b.last_result is None
    _set_consumer(authenticated_client, consumer_b)
    try:
        resp = await authenticated_client.get(_CYCLE_LAST_URL)
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["has_run"] is True
        assert body["cycle_status"] == "ok"
        assert body["models_touched"] == 0
        assert body["started_at"] is not None
    finally:
        _clear_consumer(authenticated_client)


async def test_cycle_last_401_anon(authenticated_client: AsyncClient) -> None:
    """GET /cycle/last returns 401 for unauthenticated client."""
    app = _get_app(authenticated_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(_CYCLE_LAST_URL)
    assert resp.status_code == 401  # noqa: PLR2004


# ===========================================================================
# Route-order regression
# ===========================================================================


async def test_models_route_resolves_to_list_handler(
    authenticated_client: AsyncClient,
) -> None:
    """GET /models resolves to the model-LIST handler, not a signature handler.

    /models is a single path segment, so it cannot structurally be captured by the
    two-segment {template_hash}/{service_key} param route regardless of registration
    order. To prove it hits the LIST handler (and not some other handler returning a
    different shape), assert the 200 response carries the models-list shape (a `models`
    array of ModelSummary), which only list_drain_models produces.
    """
    repo = _get_app_repo(authenticated_client)
    await _insert_drain_model(repo, model_key="route-order-svc", template_count=2)
    consumer, *_ = _consumer(repo, _FakeVlClient([]))
    _set_consumer(authenticated_client, consumer)
    try:
        resp = await authenticated_client.get(_MODELS_URL)
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        # The models-list shape: a top-level "models" array of ModelSummary objects.
        assert "models" in body
        assert isinstance(body["models"], list)
        assert any(m["model_key"] == "route-order-svc" for m in body["models"])
    finally:
        _clear_consumer(authenticated_client)


async def test_cycle_last_route_not_swallowed_by_param_route(
    authenticated_client: AsyncClient,
) -> None:
    """GET /cycle/last does NOT 404/422 as the {template_hash}/{service_key} route."""
    resp = await authenticated_client.get(_CYCLE_LAST_URL)
    assert resp.status_code not in (404, 422)
    assert resp.status_code == 503  # noqa: PLR2004
