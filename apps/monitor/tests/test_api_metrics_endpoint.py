"""Tests for the unauthenticated ``GET /metrics`` exposition endpoint."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.metrics.prometheus_writer import PrometheusRegistryWriter


@pytest.mark.asyncio
async def test_metrics_endpoint_no_auth_required(authenticated_client: AsyncClient) -> None:
    """``/metrics`` is reachable without a session cookie (vmagent scrapes anonymously)."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/metrics")
    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_prometheus_format(
    authenticated_client: AsyncClient,
) -> None:
    """Response body is Prometheus exposition text (HELP/TYPE/values)."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    # Plant a metric directly through the prometheus writer.
    # The multiplex's second inner writer is the prometheus writer; we
    # access the registry directly to plant a value via a fresh writer
    # bound to the SAME registry.
    writer = PrometheusRegistryWriter(app.state.prom_registry)
    writer.write_gauge("test_endpoint_metric", 123.0, {"label": "x"})

    resp = await authenticated_client.get("/metrics")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.text
    assert "# HELP test_endpoint_metric" in body
    assert "# TYPE test_endpoint_metric" in body
    assert 'test_endpoint_metric{label="x"} 123.0' in body


@pytest.mark.asyncio
async def test_metrics_endpoint_works_when_registry_empty(
    authenticated_client: AsyncClient,
) -> None:
    """Empty registry still serves a 200 with a (possibly minimal) body."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    # NOTE: not asserting empty body — the host collector's lifespan tick may
    # have populated the registry already. Just confirm 200 + correct
    # content-type.
    del app
    resp = await authenticated_client.get("/metrics")
    assert resp.status_code == 200  # noqa: PLR2004
