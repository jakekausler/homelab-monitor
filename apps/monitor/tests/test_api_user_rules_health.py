"""Tests for GET /api/logs/user-rules-health endpoint (STAGE-004-043A)."""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.api.schemas import LogUserRulesHealthResponse


async def test_health_both_instances_ok(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Both vmalert instances reachable with rules -> 200 with merged rules."""
    logs_rules = {
        "data": {
            "groups": [
                {
                    "name": "user-rules-logs",
                    "rules": [
                        {"name": "LogAlert1", "health": "ok", "lastError": ""},
                        {"name": "LogAlert2", "health": "err", "lastError": "parse error"},
                    ],
                }
            ]
        }
    }
    metrics_rules = {
        "data": {
            "groups": [
                {
                    "name": "user-rules-metrics",
                    "rules": [
                        {"name": "MetricsAlert1", "health": "ok", "lastError": ""},
                    ],
                }
            ]
        }
    }
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-logs:8880/api/v1/rules",
        json=logs_rules,
    )
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-metrics:8880/api/v1/rules",
        json=metrics_rules,
    )

    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data: LogUserRulesHealthResponse = LogUserRulesHealthResponse(**resp.json())
    assert len(data.rules) == 3  # noqa: PLR2004
    assert data.rules["LogAlert1"].health == "ok"
    assert data.rules["LogAlert1"].last_error == ""
    assert data.rules["LogAlert2"].health == "err"
    assert data.rules["LogAlert2"].last_error == "parse error"
    assert data.rules["MetricsAlert1"].health == "ok"


async def test_health_one_instance_unreachable(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """One vmalert instance unreachable -> 200 with only reachable rules (degraded)."""
    logs_rules = {
        "data": {
            "groups": [
                {
                    "name": "user-rules-logs",
                    "rules": [
                        {"name": "LogAlert1", "health": "ok", "lastError": ""},
                    ],
                }
            ]
        }
    }
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-logs:8880/api/v1/rules",
        json=logs_rules,
    )
    httpx_mock.add_exception(
        httpx.ConnectError("down"),
        url="http://vmalert-metrics:8880/api/v1/rules",
        method="GET",
    )

    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data: LogUserRulesHealthResponse = LogUserRulesHealthResponse(**resp.json())
    assert len(data.rules) == 1


async def test_health_both_instances_down(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Both vmalert instances unreachable -> 200 with empty rules dict."""
    httpx_mock.add_exception(
        httpx.ConnectError("down"),
        url="http://vmalert-logs:8880/api/v1/rules",
        method="GET",
    )
    httpx_mock.add_exception(
        httpx.ConnectError("down"),
        url="http://vmalert-metrics:8880/api/v1/rules",
        method="GET",
    )

    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data: LogUserRulesHealthResponse = LogUserRulesHealthResponse(**resp.json())
    assert len(data.rules) == 0


async def test_health_ignores_other_groups(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Non-user-rule groups in payload are ignored."""
    payload = {
        "data": {
            "groups": [
                {
                    "name": "builtin-alerts",
                    "rules": [
                        {"name": "BuiltinAlert", "health": "ok", "lastError": ""},
                    ],
                },
                {
                    "name": "user-rules-logs",
                    "rules": [
                        {"name": "UserAlert", "health": "ok", "lastError": ""},
                    ],
                },
            ]
        }
    }
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-logs:8880/api/v1/rules",
        json=payload,
    )
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-metrics:8880/api/v1/rules",
        json={"data": {"groups": []}},
    )

    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data: LogUserRulesHealthResponse = LogUserRulesHealthResponse(**resp.json())
    assert len(data.rules) == 1


async def test_health_unknown_health_value(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Unknown health value maps to 'unknown'."""
    payload = {
        "data": {
            "groups": [
                {
                    "name": "user-rules-logs",
                    "rules": [
                        {"name": "NoMatch", "health": "no_match", "lastError": ""},
                    ],
                }
            ]
        }
    }
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-logs:8880/api/v1/rules",
        json=payload,
    )
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-metrics:8880/api/v1/rules",
        json={"data": {"groups": []}},
    )

    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data: LogUserRulesHealthResponse = LogUserRulesHealthResponse(**resp.json())
    assert data.rules["NoMatch"].health == "unknown"


async def test_health_missing_last_error_field(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Rule missing lastError field defaults to empty string."""
    payload = {
        "data": {
            "groups": [
                {
                    "name": "user-rules-logs",
                    "rules": [
                        {"name": "NoError", "health": "ok"},  # No lastError
                    ],
                }
            ]
        }
    }
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-logs:8880/api/v1/rules",
        json=payload,
    )
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-metrics:8880/api/v1/rules",
        json={"data": {"groups": []}},
    )

    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data: LogUserRulesHealthResponse = LogUserRulesHealthResponse(**resp.json())
    assert data.rules["NoError"].last_error == ""


async def test_health_requires_session(
    unauthenticated_client: AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """GET /api/logs/user-rules-health without session -> 401."""
    resp = await unauthenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 401  # noqa: PLR2004


async def test_health_malformed_json_response(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """vmalert returns non-JSON -> treated as unreachable, degraded to empty."""
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-logs:8880/api/v1/rules",
        text="not json",
        headers={"content-type": "text/plain"},
    )
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-metrics:8880/api/v1/rules",
        json={"data": {"groups": []}},
    )

    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data: LogUserRulesHealthResponse = LogUserRulesHealthResponse(**resp.json())
    assert len(data.rules) == 0  # Logs instance ignored due to parse error


async def test_health_non_200_response(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """vmalert returns non-200 status -> instance skipped."""
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-logs:8880/api/v1/rules",
        status_code=503,
        json={"error": "service unavailable"},
    )
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-metrics:8880/api/v1/rules",
        json={"data": {"groups": []}},
    )

    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data: LogUserRulesHealthResponse = LogUserRulesHealthResponse(**resp.json())
    assert len(data.rules) == 0  # Both instances failed or empty


async def test_health_env_url_override(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOMELAB_MONITOR_VMALERT_*_URL env vars are honored."""
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_LOGS_URL", "http://custom-logs:9999")
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_METRICS_URL", "http://custom-metrics:9999")

    payload = {
        "data": {
            "groups": [
                {
                    "name": "user-rules-logs",
                    "rules": [
                        {"name": "CustomLogs", "health": "ok", "lastError": ""},
                    ],
                }
            ]
        }
    }
    httpx_mock.add_response(
        method="GET",
        url="http://custom-logs:9999/api/v1/rules",
        json=payload,
    )
    httpx_mock.add_response(
        method="GET",
        url="http://custom-metrics:9999/api/v1/rules",
        json={"data": {"groups": []}},
    )

    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data: LogUserRulesHealthResponse = LogUserRulesHealthResponse(**resp.json())
    assert "CustomLogs" in data.rules


def test_maybe_dryrun_runner_invalid_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB_MONITOR_VMALERT_DRYRUN_TIMEOUT_S=invalid -> falls back to 20.0."""
    from homelab_monitor.kernel.api.routers.logs import (  # noqa: PLC0415
        _maybe_dryrun_runner,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_DRYRUN_ENABLED", "1")
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_DRYRUN_TIMEOUT_S", "not-a-float")
    monkeypatch.setenv("HOMELAB_MONITOR_VMALERT_DRYRUN_MOUNT", "/tmp")

    runner = _maybe_dryrun_runner()  # pyright: ignore[reportPrivateUsage]
    assert runner is not None


async def test_health_non_dict_group_skipped(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Non-dict entry in groups list is silently skipped (group isinstance continue)."""
    payload = {
        "data": {
            "groups": [
                "not-a-dict-group",
                {
                    "name": "user-rules-logs",
                    "rules": [
                        {"name": "RealAlert", "health": "ok", "lastError": ""},
                    ],
                },
            ]
        }
    }
    httpx_mock.add_response(method="GET", url="http://vmalert-logs:8880/api/v1/rules", json=payload)
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-metrics:8880/api/v1/rules",
        json={"data": {"groups": []}},
    )
    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data = LogUserRulesHealthResponse(**resp.json())
    assert "RealAlert" in data.rules
    assert len(data.rules) == 1


async def test_health_non_dict_rule_skipped(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """A non-dict entry in rules list is silently skipped (covers the rule isinstance continue)."""
    payload = {
        "data": {
            "groups": [
                {
                    "name": "user-rules-logs",
                    "rules": [
                        "not-a-dict-rule",
                        {"name": "RealAlert", "health": "ok", "lastError": ""},
                    ],
                }
            ]
        }
    }
    httpx_mock.add_response(method="GET", url="http://vmalert-logs:8880/api/v1/rules", json=payload)
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-metrics:8880/api/v1/rules",
        json={"data": {"groups": []}},
    )
    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data = LogUserRulesHealthResponse(**resp.json())
    assert "RealAlert" in data.rules
    assert len(data.rules) == 1


async def test_health_non_str_rule_name_skipped(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """Rule with non-string name is silently skipped (name isinstance continue)."""
    payload = {
        "data": {
            "groups": [
                {
                    "name": "user-rules-logs",
                    "rules": [
                        {"name": 42, "health": "ok", "lastError": ""},
                        {"name": "RealAlert", "health": "ok", "lastError": ""},
                    ],
                }
            ]
        }
    }
    httpx_mock.add_response(method="GET", url="http://vmalert-logs:8880/api/v1/rules", json=payload)
    httpx_mock.add_response(
        method="GET",
        url="http://vmalert-metrics:8880/api/v1/rules",
        json={"data": {"groups": []}},
    )
    resp = await authenticated_client.get("/api/logs/user-rules-health")
    assert resp.status_code == 200  # noqa: PLR2004
    data = LogUserRulesHealthResponse(**resp.json())
    assert "RealAlert" in data.rules
    assert len(data.rules) == 1
