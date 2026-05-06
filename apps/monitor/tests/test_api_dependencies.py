"""Tests for kernel/api/dependencies.py — coverage for dependency injection."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from homelab_monitor.kernel.api.dependencies import (
    DependencyUnavailableProblem,
    get_failure_budget,
    get_http_client,
    get_started_at,
)


def _make_empty_request() -> object:
    """Return a duck-typed object that satisfies dependency function attribute access."""
    return type("R", (), {"app": type("A", (), {"state": type("S", (), {})()})()})()


def test_get_http_client_raises_when_missing() -> None:
    """get_http_client raises DependencyUnavailableProblem when http_client missing."""
    req = _make_empty_request()
    with pytest.raises(DependencyUnavailableProblem) as exc_info:
        get_http_client(req)  # pyright: ignore[reportArgumentType]
    assert exc_info.value.code == "http_client_unavailable"


def test_get_started_at_raises_when_missing() -> None:
    """get_started_at raises DependencyUnavailableProblem when started_at missing."""
    req = _make_empty_request()
    with pytest.raises(DependencyUnavailableProblem) as exc_info:
        get_started_at(req)  # pyright: ignore[reportArgumentType]
    assert exc_info.value.code == "state_unavailable"


def test_get_failure_budget_raises_when_missing() -> None:
    """get_failure_budget raises DependencyUnavailableProblem when failure_budget missing."""
    req = _make_empty_request()
    with pytest.raises(DependencyUnavailableProblem) as exc_info:
        get_failure_budget(req)  # pyright: ignore[reportArgumentType]
    assert exc_info.value.code == "failure_budget_unavailable"


@pytest.mark.asyncio
async def test_get_broker_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_broker raises DependencyUnavailableProblem when broker missing from state."""
    monkeypatch.setenv("HOMELAB_MONITOR_DEV_AUTH", "1")
    from homelab_monitor.kernel.api.app import create_app  # noqa: PLC0415

    app = create_app(lifespan_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/events", headers={"X-Auth": "dev"})
        assert resp.status_code == 503  # noqa: PLR2004
        data = resp.json()
        assert data.get("error", {}).get("code") == "broker_unavailable"


def test_get_scheduler_raises_when_missing() -> None:
    """get_scheduler raises DependencyUnavailableProblem when scheduler missing."""
    from homelab_monitor.kernel.api.dependencies import get_scheduler  # noqa: PLC0415

    req = _make_empty_request()
    with pytest.raises(DependencyUnavailableProblem) as exc_info:
        get_scheduler(req)  # pyright: ignore[reportArgumentType]
    assert exc_info.value.code == "scheduler_unavailable"


def test_get_repo_raises_when_missing() -> None:
    """get_repo raises DependencyUnavailableProblem when repo missing."""
    from homelab_monitor.kernel.api.dependencies import get_repo  # noqa: PLC0415

    req = _make_empty_request()
    with pytest.raises(DependencyUnavailableProblem) as exc_info:
        get_repo(req)  # pyright: ignore[reportArgumentType]
    assert exc_info.value.code == "database_unavailable"


def test_get_loader_raises_when_missing() -> None:
    """get_loader raises DependencyUnavailableProblem when loader missing."""
    from homelab_monitor.kernel.api.dependencies import get_loader  # noqa: PLC0415

    req = _make_empty_request()
    with pytest.raises(DependencyUnavailableProblem) as exc_info:
        get_loader(req)  # pyright: ignore[reportArgumentType]
    assert exc_info.value.code == "loader_unavailable"


def test_get_metrics_writer_raises_when_missing() -> None:
    """get_metrics_writer raises DependencyUnavailableProblem when writer missing."""
    from homelab_monitor.kernel.api.dependencies import get_metrics_writer  # noqa: PLC0415

    req = _make_empty_request()
    with pytest.raises(DependencyUnavailableProblem) as exc_info:
        get_metrics_writer(req)  # pyright: ignore[reportArgumentType]
    assert exc_info.value.code == "metrics_unavailable"


def test_get_degraded_collectors_raises_when_missing() -> None:
    """get_degraded_collectors raises DependencyUnavailableProblem when list missing."""
    from homelab_monitor.kernel.api.dependencies import get_degraded_collectors  # noqa: PLC0415

    req = _make_empty_request()
    with pytest.raises(DependencyUnavailableProblem) as exc_info:
        get_degraded_collectors(req)  # pyright: ignore[reportArgumentType]
    assert exc_info.value.code == "state_unavailable"


def test_get_failure_budget_returns_when_present() -> None:
    """get_failure_budget returns budget when present in state."""
    from unittest.mock import Mock  # noqa: PLC0415

    req = _make_empty_request()
    mock_budget = Mock()
    req.app.state.failure_budget = mock_budget  # type: ignore
    result = get_failure_budget(req)  # pyright: ignore[reportArgumentType]
    assert result is mock_budget


def test_require_dev_auth_returns_dev() -> None:
    """require_dev_auth returns 'dev' as actor identity."""
    from homelab_monitor.kernel.api.dependencies import require_dev_auth  # noqa: PLC0415

    req = _make_empty_request()
    result = require_dev_auth(req)  # pyright: ignore[reportArgumentType]
    assert result == "dev"


def test_get_scheduler_returns_when_present() -> None:
    """get_scheduler returns scheduler when present in state."""
    from unittest.mock import Mock  # noqa: PLC0415

    from homelab_monitor.kernel.api.dependencies import get_scheduler  # noqa: PLC0415

    req = _make_empty_request()
    mock_scheduler = Mock()
    req.app.state.scheduler = mock_scheduler  # type: ignore
    result = get_scheduler(req)  # pyright: ignore[reportArgumentType]
    assert result is mock_scheduler


def test_get_repo_returns_when_present() -> None:
    """get_repo returns repository when present in state."""
    from unittest.mock import Mock  # noqa: PLC0415

    from homelab_monitor.kernel.api.dependencies import get_repo  # noqa: PLC0415

    req = _make_empty_request()
    mock_repo = Mock()
    req.app.state.repo = mock_repo  # type: ignore
    result = get_repo(req)  # pyright: ignore[reportArgumentType]
    assert result is mock_repo


def test_get_broker_returns_when_present() -> None:
    """get_broker returns broker when present in state."""
    from unittest.mock import Mock  # noqa: PLC0415

    from homelab_monitor.kernel.api.dependencies import get_broker  # noqa: PLC0415

    req = _make_empty_request()
    mock_broker = Mock()
    req.app.state.broker = mock_broker  # type: ignore
    result = get_broker(req)  # pyright: ignore[reportArgumentType]
    assert result is mock_broker


def test_get_loader_returns_when_present() -> None:
    """get_loader returns loader when present in state."""
    from unittest.mock import Mock  # noqa: PLC0415

    from homelab_monitor.kernel.api.dependencies import get_loader  # noqa: PLC0415

    req = _make_empty_request()
    mock_loader = Mock()
    req.app.state.loader = mock_loader  # type: ignore
    result = get_loader(req)  # pyright: ignore[reportArgumentType]
    assert result is mock_loader


def test_get_metrics_writer_returns_when_present() -> None:
    """get_metrics_writer returns writer when present in state."""
    from unittest.mock import Mock  # noqa: PLC0415

    from homelab_monitor.kernel.api.dependencies import get_metrics_writer  # noqa: PLC0415

    req = _make_empty_request()
    mock_writer = Mock()
    req.app.state.metrics_writer = mock_writer  # type: ignore
    result = get_metrics_writer(req)  # pyright: ignore[reportArgumentType]
    assert result is mock_writer


def test_get_http_client_returns_when_present() -> None:
    """get_http_client returns client when present in state."""
    from unittest.mock import Mock  # noqa: PLC0415

    req = _make_empty_request()
    mock_client = Mock()
    req.app.state.http_client = mock_client  # type: ignore
    result = get_http_client(req)  # pyright: ignore[reportArgumentType]
    assert result is mock_client


def test_get_started_at_returns_when_present() -> None:
    """get_started_at returns timestamp when present in state."""
    req = _make_empty_request()
    req.app.state.started_at = "2026-05-06T00:00:00Z"  # type: ignore
    result = get_started_at(req)  # pyright: ignore[reportArgumentType]
    assert result == "2026-05-06T00:00:00Z"


def test_get_degraded_collectors_returns_when_present() -> None:
    """get_degraded_collectors returns list when present in state."""
    from homelab_monitor.kernel.api.dependencies import get_degraded_collectors  # noqa: PLC0415

    req = _make_empty_request()
    req.app.state.degraded_collectors = ["collector1", "collector2"]  # type: ignore
    result = get_degraded_collectors(req)  # pyright: ignore[reportArgumentType]
    assert result == ["collector1", "collector2"]
