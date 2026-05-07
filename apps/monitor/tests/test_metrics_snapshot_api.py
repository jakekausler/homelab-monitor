"""Tests for ``GET /api/metrics/snapshot``."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter


@pytest.mark.asyncio
async def test_snapshot_requires_session(authenticated_client: AsyncClient) -> None:
    """Strip the cookie and confirm 401."""
    # Use a brand-new client without cookies but pointed at the same app.
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    from httpx import ASGITransport  # noqa: PLC0415

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/api/metrics/snapshot")
    assert resp.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_snapshot_returns_empty_when_no_metrics_or_seeded(
    authenticated_client: AsyncClient,
) -> None:
    """Schema-correct response even when nothing has been recorded yet."""
    resp = await authenticated_client.get("/api/metrics/snapshot")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert "ts" in body
    assert "entries" in body
    # The host collector ticks every 10s; a freshly-booted lifespan may not
    # have ticked yet. We accept either empty or non-empty here.
    assert isinstance(body["entries"], list)


@pytest.mark.asyncio
async def test_snapshot_returns_written_metrics(
    authenticated_client: AsyncClient,
) -> None:
    """Manually push a metric through the writer; snapshot reflects it."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    writer = cast(MemoryRetainingMetricsWriter, app.state.metrics_writer)
    assert isinstance(writer, MemoryRetainingMetricsWriter)
    writer.write_gauge("snapshot_test_metric", 42.0, {"k": "v"})
    resp = await authenticated_client.get("/api/metrics/snapshot")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    matching = [e for e in body["entries"] if e["name"] == "snapshot_test_metric"]
    assert len(matching) == 1
    assert matching[0]["value"] == 42.0  # noqa: PLR2004
    assert matching[0]["labels"] == {"k": "v"}
    assert matching[0]["kind"] == "gauge"
    assert matching[0]["ts"].endswith("+00:00")


@pytest.mark.asyncio
async def test_snapshot_response_shape(authenticated_client: AsyncClient) -> None:
    """Response keys exactly match Pydantic model (extra=forbid)."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    writer = cast(MemoryRetainingMetricsWriter, app.state.metrics_writer)
    assert isinstance(writer, MemoryRetainingMetricsWriter)
    writer.write_gauge("shape_check", 1.0, {})
    resp = await authenticated_client.get("/api/metrics/snapshot")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert set(body.keys()) == {"ts", "entries"}
    if body["entries"]:
        e = body["entries"][0]
        assert set(e.keys()) == {"name", "value", "labels", "kind", "ts"}


@pytest.mark.asyncio
async def test_snapshot_csrf_not_enforced_on_get(
    authenticated_client: AsyncClient,
) -> None:
    """GET endpoints don't require X-CSRF-Token. Strip headers and confirm 200."""
    resp = await authenticated_client.get(
        "/api/metrics/snapshot",
        headers={},  # no X-CSRF-Token deliberately
    )
    assert resp.status_code == 200  # noqa: PLR2004


@pytest.mark.asyncio
async def test_snapshot_after_replace_family(
    authenticated_client: AsyncClient,
) -> None:
    """After replace_family, snapshot reflects only the latest entries."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    writer = cast(MemoryRetainingMetricsWriter, app.state.metrics_writer)
    assert isinstance(writer, MemoryRetainingMetricsWriter)
    writer.write_gauge("rf_test", 1.0, {"k": "old"})
    writer.replace_family("rf_test", [(99.0, {"k": "new"})])
    resp = await authenticated_client.get("/api/metrics/snapshot")
    assert resp.status_code == 200  # noqa: PLR2004
    matches = [e for e in resp.json()["entries"] if e["name"] == "rf_test"]
    assert len(matches) == 1
    assert matches[0]["labels"] == {"k": "new"}
    assert matches[0]["value"] == 99.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_snapshot_with_base_writer_returns_empty(
    authenticated_client: AsyncClient,
) -> None:
    """Endpoint gracefully returns empty entries when writer is not retaining."""
    from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter  # noqa: PLC0415

    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    original = app.state.metrics_writer
    app.state.metrics_writer = InMemoryMetricsWriter()
    try:
        resp = await authenticated_client.get("/api/metrics/snapshot")
        assert resp.status_code == 200  # noqa: PLR2004
        body = resp.json()
        assert body["entries"] == []
        assert body["ts"].endswith("+00:00")
    finally:
        app.state.metrics_writer = original
