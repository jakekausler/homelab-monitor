"""Unit tests for the integration rig health-probe gate.

These run in the normal suite (NOT @pytest.mark.integration) and require NO rig.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from pytest_httpx import HTTPXMock

from tests.integration.helpers import rig_health


def _probe_all_up(_name: str) -> bool:
    """Stub probe: every component healthy."""
    return True


def _probe_all_down(_name: str) -> bool:
    """Stub probe: every component unhealthy."""
    return False


def _probe_all_but_vl(name: str) -> bool:
    """Stub probe: every component healthy except victorialogs."""
    return name != "victorialogs"


def _probe_all_but_grafana(name: str) -> bool:
    """Stub probe: every component healthy except grafana."""
    return name != "grafana"


@pytest.fixture(autouse=True)
def _reset_cache() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Clear the module-level session cache before each test."""
    rig_health.reset_health_cache()
    yield


def test_require_passes_when_all_required_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """All required components healthy -> require_rig_components returns (no skip)."""
    monkeypatch.setattr(rig_health, "probe_component", _probe_all_up)
    # Must not raise Skipped.
    rig_health.require_rig_components("monitor", "victorialogs")


def test_require_skips_when_a_required_component_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A down required component -> pytest.skip naming it."""
    monkeypatch.setattr(rig_health, "probe_component", _probe_all_but_vl)
    with pytest.raises(pytest.skip.Exception, match="victorialogs"):
        rig_health.require_rig_components("monitor", "victorialogs")


def test_partial_rig_only_skips_affected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Components NOT in the required set don't cause a skip (partial-rig aware)."""
    # grafana is down, but the test only requires monitor + victorialogs.
    monkeypatch.setattr(rig_health, "probe_component", _probe_all_but_grafana)
    rig_health.require_rig_components("monitor", "victorialogs")  # no skip
    with pytest.raises(pytest.skip.Exception, match="grafana"):
        rig_health.require_rig_components("grafana", "monitor")


def test_all_down_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """All components down -> skip naming the required ones."""
    monkeypatch.setattr(rig_health, "probe_component", _probe_all_down)
    with pytest.raises(pytest.skip.Exception):
        rig_health.require_rig_components("monitor")


def test_probe_component_returns_false_on_connection_error(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A connection error -> probe_component returns False, does NOT raise."""
    monkeypatch.setenv("MONITOR_URL", "http://monitor-unreachable:9090")
    httpx_mock.add_exception(httpx.ConnectError("no route"))
    assert rig_health.probe_component("monitor") is False


def test_probe_component_returns_false_on_timeout(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A timeout -> probe_component returns False, does NOT raise (within ~2s)."""
    monkeypatch.setenv("MONITOR_URL", "http://monitor-slow:9090")
    httpx_mock.add_exception(httpx.ReadTimeout("slow"))
    assert rig_health.probe_component("monitor") is False


def test_probe_component_returns_true_on_2xx(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A 2xx response -> probe_component returns True."""
    monkeypatch.setenv("MONITOR_URL", "http://monitor:9090")
    httpx_mock.add_response(url="http://monitor:9090/api/healthz", status_code=200)
    assert rig_health.probe_component("monitor") is True


def test_probe_component_returns_false_on_non_2xx(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """A 503 response -> probe_component returns False."""
    monkeypatch.setenv("MONITOR_URL", "http://monitor:9090")
    httpx_mock.add_response(url="http://monitor:9090/api/healthz", status_code=503)
    assert rig_health.probe_component("monitor") is False


def test_probe_component_rejects_unknown_name() -> None:
    """Unknown component name -> ValueError (programmer error, not a probe failure)."""
    with pytest.raises(ValueError, match="unknown rig component"):
        rig_health.probe_component("nonexistent")


def test_rig_health_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """rig_health() probes once and caches; second call does not re-probe."""
    calls: list[str] = []

    def _counting_probe(name: str) -> bool:
        calls.append(name)
        return True

    monkeypatch.setattr(rig_health, "probe_component", _counting_probe)
    first = rig_health.rig_health()
    n_after_first = len(calls)
    second = rig_health.rig_health()
    assert first == second
    assert len(calls) == n_after_first  # no additional probes on 2nd call
