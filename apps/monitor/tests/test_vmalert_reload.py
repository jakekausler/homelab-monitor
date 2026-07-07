"""Tests for kernel/vmalert/reload.py — VmalertReloader (STAGE-010-001)."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock
from structlog.testing import capture_logs

from homelab_monitor.kernel.vmalert.reload import (
    DEFAULT_VMALERT_URL,
    VmalertReloader,
)


@pytest.mark.asyncio
async def test_reload_success_2xx(httpx_mock: HTTPXMock) -> None:
    """A 200 response makes ``reload()`` return True and emit an ``ok`` log."""
    httpx_mock.add_response(
        method="POST",
        url=f"{DEFAULT_VMALERT_URL}/-/reload",
        status_code=200,
    )
    async with httpx.AsyncClient() as client:
        reloader = VmalertReloader(client=client)
        with capture_logs() as captured:
            ok = await reloader.reload()
    assert ok is True
    assert any(entry.get("event") == "vmalert.reload.ok" for entry in captured)


@pytest.mark.asyncio
async def test_reload_failure_5xx(httpx_mock: HTTPXMock) -> None:
    """A 500 response makes ``reload()`` return False and emit a warning."""
    httpx_mock.add_response(
        method="POST",
        url=f"{DEFAULT_VMALERT_URL}/-/reload",
        status_code=500,
    )
    async with httpx.AsyncClient() as client:
        reloader = VmalertReloader(client=client)
        with capture_logs() as captured:
            ok = await reloader.reload()
    assert ok is False
    non_200_entries = [e for e in captured if e.get("event") == "vmalert.reload.non_200"]
    assert non_200_entries, f"expected vmalert.reload.non_200 log, got: {captured}"
    assert non_200_entries[0].get("status_code") == 500  # noqa: PLR2004


@pytest.mark.asyncio
async def test_reload_failure_network_error(httpx_mock: HTTPXMock) -> None:
    """A network error makes ``reload()`` return False and emit an unreachable warning."""
    httpx_mock.add_exception(
        httpx.ConnectError("connection refused"),
        method="POST",
        url=f"{DEFAULT_VMALERT_URL}/-/reload",
    )
    async with httpx.AsyncClient() as client:
        reloader = VmalertReloader(client=client)
        with capture_logs() as captured:
            ok = await reloader.reload()
    assert ok is False
    unreachable_entries = [e for e in captured if e.get("event") == "vmalert.reload.unreachable"]
    assert unreachable_entries, f"expected vmalert.reload.unreachable log, got: {captured}"
    assert "connection refused" in str(unreachable_entries[0].get("error", ""))


@pytest.mark.asyncio
async def test_reload_uses_default_url(httpx_mock: HTTPXMock) -> None:
    """The default POST target is http://vmalert-metrics:8880/-/reload."""
    httpx_mock.add_response(
        method="POST",
        url="http://vmalert-metrics:8880/-/reload",
        status_code=200,
    )
    async with httpx.AsyncClient() as client:
        reloader = VmalertReloader(client=client)
        ok = await reloader.reload()
    assert ok is True
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert str(requests[0].url) == "http://vmalert-metrics:8880/-/reload"


@pytest.mark.asyncio
async def test_reload_honors_custom_base_url(httpx_mock: HTTPXMock) -> None:
    """A non-default ``base_url`` is honored and trailing slashes stripped."""
    httpx_mock.add_response(
        method="POST",
        url="http://vmalert-alt:9999/-/reload",
        status_code=200,
    )
    async with httpx.AsyncClient() as client:
        reloader = VmalertReloader(base_url="http://vmalert-alt:9999/", client=client)
        ok = await reloader.reload()
    assert ok is True
    requests = httpx_mock.get_requests()
    assert str(requests[0].url) == "http://vmalert-alt:9999/-/reload"


@pytest.mark.asyncio
async def test_reload_creates_client_when_not_injected(httpx_mock: HTTPXMock) -> None:
    """When no client is injected, ``reload()`` still POSTs successfully.

    ``pytest_httpx`` intercepts all httpx clients globally, so the internally
    created ``AsyncClient`` is captured too. This guards the CLI-path lifetime
    (the CLI never injects a client).
    """
    httpx_mock.add_response(
        method="POST",
        url=f"{DEFAULT_VMALERT_URL}/-/reload",
        status_code=200,
    )
    reloader = VmalertReloader()  # no client injected
    ok = await reloader.reload()
    assert ok is True
    assert len(httpx_mock.get_requests()) == 1
