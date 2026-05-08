"""Tests for lifespan helper functions and DI helpers in dependencies.py."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from homelab_monitor.kernel.api.lifespan import (
    _extract_sqlite_path,  # pyright: ignore[reportPrivateUsage]
)

# ---------------------------------------------------------------------------
# _extract_sqlite_path (lifespan.py lines 72-80)
# ---------------------------------------------------------------------------


def test_extract_sqlite_path_rejects_memory_url() -> None:
    """':memory:' in URL raises ValueError."""
    with pytest.raises(ValueError, match=":memory:"):
        _extract_sqlite_path("sqlite+aiosqlite:///:memory:")


def test_extract_sqlite_path_strips_aiosqlite_prefix() -> None:
    """'sqlite+aiosqlite:///' prefix is stripped, leaving the bare file path."""
    result = _extract_sqlite_path("sqlite+aiosqlite:///tmp/foo.sqlite")
    assert result == "tmp/foo.sqlite"


def test_extract_sqlite_path_passes_through_non_prefix_url() -> None:
    """A URL without the recognized prefix is returned unchanged."""
    result = _extract_sqlite_path("/raw/path.sqlite")
    assert result == "/raw/path.sqlite"


# ---------------------------------------------------------------------------
# get_logs_writer / get_in_memory_logs_writer (dependencies.py lines 238-255)
# ---------------------------------------------------------------------------


def _make_request_with_state(**attrs: object) -> Request:
    """Build a minimal Starlette Request whose app.state carries the given attrs."""
    from fastapi import FastAPI  # noqa: PLC0415

    app = FastAPI()
    for k, v in attrs.items():
        setattr(app.state, k, v)
    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "app": app,
    }
    return Request(scope)  # pyright: ignore[reportArgumentType]


def test_get_logs_writer_returns_state_value() -> None:
    """get_logs_writer returns the object stored in app.state.logs_writer."""
    from homelab_monitor.kernel.api.dependencies import get_logs_writer  # noqa: PLC0415

    sentinel = object()
    request = _make_request_with_state(logs_writer=sentinel)
    assert get_logs_writer(request) is sentinel


def test_get_in_memory_logs_writer_returns_state_value() -> None:
    """get_in_memory_logs_writer returns the object stored in app.state.in_memory_logs_writer."""
    from homelab_monitor.kernel.api.dependencies import get_in_memory_logs_writer  # noqa: PLC0415

    sentinel = object()
    request = _make_request_with_state(in_memory_logs_writer=sentinel)
    assert get_in_memory_logs_writer(request) is sentinel


def test_get_logs_writer_raises_503_when_missing() -> None:
    """get_logs_writer raises DependencyUnavailableProblem when state attr is absent."""
    from homelab_monitor.kernel.api.dependencies import get_logs_writer  # noqa: PLC0415
    from homelab_monitor.kernel.api.errors import DependencyUnavailableProblem  # noqa: PLC0415

    request = _make_request_with_state()
    with pytest.raises(DependencyUnavailableProblem):
        get_logs_writer(request)


def test_get_in_memory_logs_writer_raises_503_when_missing() -> None:
    """get_in_memory_logs_writer raises DependencyUnavailableProblem when state attr is absent."""
    from homelab_monitor.kernel.api.dependencies import get_in_memory_logs_writer  # noqa: PLC0415
    from homelab_monitor.kernel.api.errors import DependencyUnavailableProblem  # noqa: PLC0415

    request = _make_request_with_state()
    with pytest.raises(DependencyUnavailableProblem):
        get_in_memory_logs_writer(request)
