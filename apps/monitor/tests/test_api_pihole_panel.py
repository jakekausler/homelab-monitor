"""Tests for STAGE-006-020 Pi-hole panel data endpoints (GET /api/integrations/pihole/*).

Covers 7 new endpoints:
- GET /overview (VM-sourced)
- GET /adlists (VM-sourced, JOIN)
- GET /upstreams (VM-sourced)
- GET /unbound (VM-sourced)
- GET /clients (live)
- GET /recent-blocked (live)
- GET /messages (live)

Plus the pihole_version lifespan startup-run hook.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.api.app import create_app
from homelab_monitor.kernel.pihole.client import PiholeResponse
from homelab_monitor.kernel.pihole.errors import PiholeError

_HTTP_OK = 200
_HTTP_UNAUTH = 401
_HTTP_UNPROCESSABLE = 422
_HTTP_BAD_GATEWAY = 502
_HTTP_UNAVAILABLE = 503
_VM_URL = "http://vm-test:8428"


def _vector(
    samples: list[tuple[dict[str, str], str]], ts: float = 1714867200.0
) -> dict[str, object]:
    """Build a VM instant-vector JSON body from (labels, value_str) tuples."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": labels, "value": [ts, value]} for labels, value in samples],
        },
    }


def _empty_vector() -> dict[str, object]:
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


def _query_of(request: httpx.Request) -> str:
    qs = parse_qs(urlparse(str(request.url)).query)
    return qs["query"][0]


class _FakeRoClient:
    def __init__(
        self,
        *,
        clients_result: object | PiholeError | None = None,
        recent_result: object | PiholeError | None = None,
        messages_result: object | PiholeError | None = None,
    ) -> None:
        self._clients = clients_result
        self._recent = recent_result
        self._messages = messages_result

    async def stats_top_clients(
        self, *, blocked: bool = False, count: int | None = None
    ) -> PiholeResponse | PiholeError:
        if isinstance(self._clients, PiholeError):
            return self._clients
        return PiholeResponse(payload=self._clients, took_seconds=0.0, endpoint="stats/top_clients")

    async def stats_recent_blocked(self) -> PiholeResponse | PiholeError:
        if isinstance(self._recent, PiholeError):
            return self._recent
        return PiholeResponse(
            payload=self._recent, took_seconds=0.0, endpoint="stats/recent_blocked"
        )

    async def info_messages(self) -> PiholeResponse | PiholeError:
        if isinstance(self._messages, PiholeError):
            return self._messages
        return PiholeResponse(payload=self._messages, took_seconds=0.0, endpoint="info/messages")


# ===== VM endpoint tests (overview, adlists, upstreams, unbound) =====


class TestGetPiholeOverview:
    async def test_happy_all_present(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All scalars present, versions with 2 components, updates with 1==1 and 1==0."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
            query = _query_of(request)
            if query in ("homelab_pihole_up", "homelab_pihole_blocking_enabled"):
                return httpx.Response(200, json=_vector([({}, "1")]))
            elif query == "homelab_pihole_blocking_timer_seconds":
                return httpx.Response(200, json=_vector([({}, "0")]))
            elif query == "homelab_pihole_percent_blocked":
                return httpx.Response(200, json=_vector([({}, "15.5")]))
            elif query == "homelab_pihole_query_frequency":
                return httpx.Response(200, json=_vector([({}, "100.2")]))
            elif query == "homelab_pihole_privacy_level":
                return httpx.Response(200, json=_vector([({}, "2")]))
            elif query == "homelab_pihole_query_logging_enabled":
                return httpx.Response(200, json=_vector([({}, "1")]))
            elif query == "homelab_pihole_gravity_domains":
                return httpx.Response(200, json=_vector([({}, "50000")]))
            elif query == "homelab_pihole_version_info":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"component": "pihole-FTL", "version": "v5.18"}, "1"),
                            ({"component": "dnsmasq", "version": "2.86"}, "1"),
                        ]
                    ),
                )
            elif query == "homelab_pihole_update_available":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"component": "FTL"}, "1"),
                            ({"component": "core"}, "0"),
                        ]
                    ),
                )
            elif query == "homelab_pihole_messages_count":
                return httpx.Response(200, json=_vector([({}, "5")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )

        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["up"] is True
        assert body["blocking_enabled"] is True
        assert body["blocking_timer_seconds"] == 0.0
        assert body["percent_blocked"] == 15.5  # noqa: PLR2004
        assert body["query_frequency"] == 100.2  # noqa: PLR2004
        assert body["privacy_level"] == 2  # noqa: PLR2004
        assert body["query_logging_enabled"] is True
        assert body["gravity_domains"] == 50000  # noqa: PLR2004
        assert body["messages_count"] == 5  # noqa: PLR2004
        assert len(body["versions"]) == 2  # noqa: PLR2004
        assert body["versions"][0]["component"] == "pihole-FTL"
        assert body["versions"][1]["component"] == "dnsmasq"
        assert len(body["updates_available"]) == 1
        assert body["updates_available"][0]["component"] == "FTL"

    async def test_up_absent_false(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """up absent -> False."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["up"] is False

    async def test_up_zero_false(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """up == 0 -> False."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_up":
                return httpx.Response(200, json=_vector([({}, "0")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["up"] is False

    async def test_up_one_true(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """up == 1 -> True."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_up":
                return httpx.Response(200, json=_vector([({}, "1")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["up"] is True

    async def test_blocking_enabled_states(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """blocking_enabled absent -> None, == 0 -> False, == 1 -> True."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_blocking_enabled":
                return httpx.Response(200, json=_vector([({}, "0")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["blocking_enabled"] is False

    async def test_all_nullable_absent(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All nullable series empty -> all None, up False, messages_count 0, empty lists."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["up"] is False
        assert body["blocking_enabled"] is None
        assert body["blocking_timer_seconds"] is None
        assert body["percent_blocked"] is None
        assert body["query_frequency"] is None
        assert body["privacy_level"] is None
        assert body["query_logging_enabled"] is None
        assert body["gravity_domains"] is None
        assert body["messages_count"] == 0
        assert body["versions"] == []
        assert body["updates_available"] == []

    async def test_version_empty_component_skipped(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """version_info sample with empty component label -> skipped."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_version_info":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"component": "", "version": "1.0"}, "1"),
                        ]
                    ),
                )
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["versions"] == []

    async def test_version_missing_version_label(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """version_info sample with missing version label -> version ''."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_version_info":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"component": "pihole-FTL"}, "1"),
                        ]
                    ),
                )
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["versions"]) == 1
        assert body["versions"][0]["version"] == ""

    async def test_update_non_numeric_skipped(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """update_available sample with non-numeric value_str -> skipped."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_update_available":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"component": "FTL"}, "not_a_number"),
                        ]
                    ),
                )
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["updates_available"] == []

    async def test_update_empty_component_skipped(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """update_available sample with empty component -> skipped."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_update_available":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"component": ""}, "1"),
                        ]
                    ),
                )
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["updates_available"] == []

    async def test_privacy_level_non_numeric_none(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """privacy_level non-numeric value -> None."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_privacy_level":
                return httpx.Response(200, json=_vector([({}, "not_numeric")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["privacy_level"] is None

    async def test_vm_down_502(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """VM down -> 502."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/overview")
        assert response.status_code == _HTTP_BAD_GATEWAY


class TestGetPiholeAdlists:
    async def test_happy_join(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """2 status samples, matching enabled+domains -> rows with enabled bool + domains int."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_adlist_status":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            (
                                {
                                    "list": "adlist-1",
                                    "address": "http://example.com/ads",
                                    "status": "enabled",
                                },
                                "1",
                            ),
                            (
                                {
                                    "list": "adlist-2",
                                    "address": "http://example.com/tracking",
                                    "status": "enabled",
                                },
                                "1",
                            ),
                        ]
                    ),
                )
            elif query == "homelab_pihole_adlist_enabled":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"list": "adlist-1", "address": "http://example.com/ads"}, "1"),
                            ({"list": "adlist-2", "address": "http://example.com/tracking"}, "0"),
                        ]
                    ),
                )
            elif query == "homelab_pihole_adlist_domains":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"list": "adlist-1", "address": "http://example.com/ads"}, "1000"),
                            ({"list": "adlist-2", "address": "http://example.com/tracking"}, "500"),
                        ]
                    ),
                )
            elif query == "homelab_pihole_gravity_domains":
                return httpx.Response(200, json=_vector([({}, "50000")]))
            elif query == "homelab_pihole_gravity_last_update_age_seconds":
                return httpx.Response(200, json=_vector([({}, "3600")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/adlists")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["rows"]) == 2  # noqa: PLR2004
        assert body["rows"][0]["enabled"] is True
        assert body["rows"][0]["domains"] == 1000  # noqa: PLR2004
        assert body["rows"][1]["enabled"] is False
        assert body["rows"][1]["domains"] == 500  # noqa: PLR2004
        assert body["gravity_domains"] == 50000  # noqa: PLR2004
        assert body["gravity_last_update_age_seconds"] == 3600.0  # noqa: PLR2004

    async def test_status_no_matching_enabled(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """status sample with NO matching enabled key -> enabled=False."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_adlist_status":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            (
                                {
                                    "list": "adlist-1",
                                    "address": "http://example.com/ads",
                                    "status": "enabled",
                                },
                                "1",
                            ),
                        ]
                    ),
                )
            elif query == "homelab_pihole_adlist_enabled":
                return httpx.Response(200, json=_empty_vector())
            elif query == "homelab_pihole_adlist_domains":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"list": "adlist-1", "address": "http://example.com/ads"}, "1000"),
                        ]
                    ),
                )
            elif query == "homelab_pihole_gravity_domains":
                return httpx.Response(200, json=_vector([({}, "50000")]))
            elif query == "homelab_pihole_gravity_last_update_age_seconds":
                return httpx.Response(200, json=_empty_vector())
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/adlists")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["enabled"] is False

    async def test_status_no_matching_domains(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """status sample with NO matching domains key -> domains=None."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_adlist_status":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            (
                                {
                                    "list": "adlist-1",
                                    "address": "http://example.com/ads",
                                    "status": "enabled",
                                },
                                "1",
                            ),
                        ]
                    ),
                )
            elif query == "homelab_pihole_adlist_enabled":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"list": "adlist-1", "address": "http://example.com/ads"}, "1"),
                        ]
                    ),
                )
            elif query == "homelab_pihole_adlist_domains":
                return httpx.Response(200, json=_empty_vector())
            elif query == "homelab_pihole_gravity_domains":
                return httpx.Response(200, json=_vector([({}, "50000")]))
            elif query == "homelab_pihole_gravity_last_update_age_seconds":
                return httpx.Response(200, json=_empty_vector())
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/adlists")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["domains"] is None

    async def test_enabled_non_numeric(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """enabled sample non-numeric value -> enabled False."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_adlist_status":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            (
                                {
                                    "list": "adlist-1",
                                    "address": "http://example.com/ads",
                                    "status": "enabled",
                                },
                                "1",
                            ),
                        ]
                    ),
                )
            elif query == "homelab_pihole_adlist_enabled":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            (
                                {"list": "adlist-1", "address": "http://example.com/ads"},
                                "not_numeric",
                            ),
                        ]
                    ),
                )
            elif query == "homelab_pihole_adlist_domains":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"list": "adlist-1", "address": "http://example.com/ads"}, "1000"),
                        ]
                    ),
                )
            elif query == "homelab_pihole_gravity_domains":
                return httpx.Response(200, json=_vector([({}, "50000")]))
            elif query == "homelab_pihole_gravity_last_update_age_seconds":
                return httpx.Response(200, json=_empty_vector())
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/adlists")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["enabled"] is False

    async def test_domains_non_numeric_skipped(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """domains sample non-numeric value -> skipped, row domains None."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_adlist_status":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            (
                                {
                                    "list": "adlist-1",
                                    "address": "http://example.com/ads",
                                    "status": "enabled",
                                },
                                "1",
                            ),
                        ]
                    ),
                )
            elif query == "homelab_pihole_adlist_enabled":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"list": "adlist-1", "address": "http://example.com/ads"}, "1"),
                        ]
                    ),
                )
            elif query == "homelab_pihole_adlist_domains":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            (
                                {"list": "adlist-1", "address": "http://example.com/ads"},
                                "not_numeric",
                            ),
                        ]
                    ),
                )
            elif query == "homelab_pihole_gravity_domains":
                return httpx.Response(200, json=_vector([({}, "50000")]))
            elif query == "homelab_pihole_gravity_last_update_age_seconds":
                return httpx.Response(200, json=_empty_vector())
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/adlists")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["domains"] is None

    async def test_gravity_absent(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """gravity_domains / age absent -> None."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_adlist_status":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            (
                                {
                                    "list": "adlist-1",
                                    "address": "http://example.com/ads",
                                    "status": "enabled",
                                },
                                "1",
                            ),
                        ]
                    ),
                )
            elif query == "homelab_pihole_adlist_enabled":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"list": "adlist-1", "address": "http://example.com/ads"}, "1"),
                        ]
                    ),
                )
            elif query == "homelab_pihole_adlist_domains":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"list": "adlist-1", "address": "http://example.com/ads"}, "1000"),
                        ]
                    ),
                )
            else:
                return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/adlists")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["gravity_domains"] is None
        assert body["gravity_last_update_age_seconds"] is None

    async def test_vm_down_502(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """VM down -> 502."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/adlists")
        assert response.status_code == _HTTP_BAD_GATEWAY


class TestGetPiholeUpstreams:
    async def test_happy_multiple_rows(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """≥2 upstream samples -> rows."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_upstream_queries":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"upstream": "8.8.8.8"}, "1000"),
                            ({"upstream": "8.8.4.4"}, "500"),
                        ]
                    ),
                )
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/upstreams")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["rows"]) == 2  # noqa: PLR2004
        assert body["rows"][0]["upstream"] == "8.8.8.8"
        assert body["rows"][0]["queries"] == 1000.0  # noqa: PLR2004
        assert body["rows"][1]["upstream"] == "8.8.4.4"
        assert body["rows"][1]["queries"] == 500.0  # noqa: PLR2004

    async def test_empty_series_empty_rows(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty series -> empty rows."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/upstreams")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []

    async def test_non_numeric_value_skipped(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """sample with non-numeric value_str -> skipped."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_upstream_queries":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"upstream": "8.8.8.8"}, "not_numeric"),
                        ]
                    ),
                )
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/upstreams")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []

    async def test_vm_down_502(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """VM down -> 502."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/upstreams")
        assert response.status_code == _HTTP_BAD_GATEWAY


class TestGetPiholeUnbound:
    async def test_happy_all_present(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All 7 series present (extended_stats=1 -> True)."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
            query = _query_of(request)
            if query == "homelab_unbound_cache_hit_ratio":
                return httpx.Response(200, json=_vector([({}, "0.75")]))
            elif query == "homelab_unbound_queries_total":
                return httpx.Response(200, json=_vector([({}, "10000")]))
            elif query == "homelab_unbound_cache_hits_total":
                return httpx.Response(200, json=_vector([({}, "7500")]))
            elif query == "homelab_unbound_cache_misses_total":
                return httpx.Response(200, json=_vector([({}, "2500")]))
            elif query == "homelab_unbound_prefetch_total":
                return httpx.Response(200, json=_vector([({}, "1000")]))
            elif query == "homelab_unbound_requestlist_current":
                return httpx.Response(200, json=_vector([({}, "50")]))
            elif query == "homelab_pihole_unbound_extended_stats_enabled":
                return httpx.Response(200, json=_vector([({}, "1")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/unbound")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["cache_hit_ratio"] == 0.75  # noqa: PLR2004
        assert body["queries_total"] == 10000.0  # noqa: PLR2004
        assert body["cache_hits_total"] == 7500.0  # noqa: PLR2004
        assert body["cache_misses_total"] == 2500.0  # noqa: PLR2004
        assert body["prefetch_total"] == 1000.0  # noqa: PLR2004
        assert body["requestlist_current"] == 50.0  # noqa: PLR2004
        assert body["extended_stats_enabled"] is True

    async def test_all_absent(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All absent -> all None."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/unbound")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["cache_hit_ratio"] is None
        assert body["queries_total"] is None
        assert body["cache_hits_total"] is None
        assert body["cache_misses_total"] is None
        assert body["prefetch_total"] is None
        assert body["requestlist_current"] is None
        assert body["extended_stats_enabled"] is None

    async def test_extended_stats_false(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """extended_stats ==0 -> False."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_pihole_unbound_extended_stats_enabled":
                return httpx.Response(200, json=_vector([({}, "0")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/unbound")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["extended_stats_enabled"] is False

    async def test_vm_down_502(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """VM down -> 502."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/pihole/unbound")
        assert response.status_code == _HTTP_BAD_GATEWAY


# ===== Live endpoint tests (clients, recent-blocked, messages) =====


class TestGetPiholeClients:
    async def test_happy_multiple_clients(self, authenticated_client: AsyncClient) -> None:
        """Multiple clients, name mapping, count as int."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            clients_result={
                "clients": [
                    {"ip": "127.0.0.1", "name": "pi.hole", "count": 100},
                    {"ip": "192.168.2.10", "name": "", "count": 50},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/clients")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["rows"]) == 2  # noqa: PLR2004
        assert body["rows"][0]["client"] == "127.0.0.1"
        assert body["rows"][0]["name"] == "pi.hole"
        assert body["rows"][0]["count"] == 100  # noqa: PLR2004
        assert body["rows"][1]["client"] == "192.168.2.10"
        assert body["rows"][1]["name"] is None
        assert body["rows"][1]["count"] == 50  # noqa: PLR2004
        assert body["returned"] == 2  # noqa: PLR2004

    async def test_payload_not_dict(self, authenticated_client: AsyncClient) -> None:
        """payload not a dict (e.g. a list) -> rows empty."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(clients_result=[])

        response = await authenticated_client.get("/api/integrations/pihole/clients")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []
        assert body["returned"] == 0

    async def test_missing_clients_key(self, authenticated_client: AsyncClient) -> None:
        """payload dict missing 'clients' -> rows empty."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(clients_result={})

        response = await authenticated_client.get("/api/integrations/pihole/clients")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []

    async def test_clients_not_list(self, authenticated_client: AsyncClient) -> None:
        """'clients' not a list -> rows empty."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(clients_result={"clients": "not_a_list"})

        response = await authenticated_client.get("/api/integrations/pihole/clients")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []

    async def test_entry_not_dict(self, authenticated_client: AsyncClient) -> None:
        """entry not a dict -> skipped."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            clients_result={
                "clients": [
                    {"ip": "127.0.0.1", "name": "pi.hole", "count": 100},
                    "not_a_dict",
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/clients")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["rows"]) == 1

    async def test_missing_blank_ip(self, authenticated_client: AsyncClient) -> None:
        """entry missing/blank ip -> skipped."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            clients_result={
                "clients": [
                    {"ip": "", "name": "blank", "count": 100},
                    {"name": "no_ip", "count": 50},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/clients")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []

    async def test_non_numeric_count(self, authenticated_client: AsyncClient) -> None:
        """count not numeric -> count_val 0."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            clients_result={
                "clients": [
                    {"ip": "127.0.0.1", "name": "pi.hole", "count": "not_numeric"},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/clients")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["count"] == 0

    async def test_blocked_true_passes(self, authenticated_client: AsyncClient) -> None:
        """blocked=true query param passes through."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            clients_result={
                "clients": [
                    {"ip": "127.0.0.1", "name": "pi.hole", "count": 50},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/clients?blocked=true")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["rows"]) == 1

    async def test_pihole_error_502(self, authenticated_client: AsyncClient) -> None:
        """PiholeError -> 502."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            clients_result=PiholeError(reason="bad_response", message="test message", status=None)
        )

        response = await authenticated_client.get("/api/integrations/pihole/clients")
        assert response.status_code == _HTTP_BAD_GATEWAY

    async def test_client_uninit_503(self, authenticated_client: AsyncClient) -> None:
        """client uninitialized -> 503."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = None

        response = await authenticated_client.get("/api/integrations/pihole/clients")
        assert response.status_code == _HTTP_UNAVAILABLE

    async def test_count_below_min_422(self, authenticated_client: AsyncClient) -> None:
        """count=0 (below ge=1) -> 422."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(clients_result={"clients": []})

        response = await authenticated_client.get("/api/integrations/pihole/clients?count=0")
        assert response.status_code == _HTTP_UNPROCESSABLE

    async def test_count_above_max_422(self, authenticated_client: AsyncClient) -> None:
        """count=101 (above le=100) -> 422."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(clients_result={"clients": []})

        response = await authenticated_client.get("/api/integrations/pihole/clients?count=101")
        assert response.status_code == _HTTP_UNPROCESSABLE

    async def test_count_at_bounds(self, authenticated_client: AsyncClient) -> None:
        """count=1 and count=100 -> 200 (bounds inclusive)."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(clients_result={"clients": []})

        response = await authenticated_client.get("/api/integrations/pihole/clients?count=1")
        assert response.status_code == _HTTP_OK

        response = await authenticated_client.get("/api/integrations/pihole/clients?count=100")
        assert response.status_code == _HTTP_OK

    async def test_bool_count_rejected(self, authenticated_client: AsyncClient) -> None:
        """count bool True -> count_val 0 (bool rejected guard)."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            clients_result={
                "clients": [
                    {"ip": "127.0.0.1", "name": "test", "count": True},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/clients")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["count"] == 0


class TestGetPiholeRecentBlocked:
    async def test_happy_dict_shape(self, authenticated_client: AsyncClient) -> None:
        """Happy dict shape {"blocked":[...]} -> rows."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(recent_result={"blocked": ["a.com", "b.com"]})

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == ["a.com", "b.com"]
        assert body["returned"] == 2  # noqa: PLR2004

    async def test_happy_bare_list(self, authenticated_client: AsyncClient) -> None:
        """Happy bare-list shape -> rows."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(recent_result=["a.com", "b.com"])

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == ["a.com", "b.com"]
        assert body["returned"] == 2  # noqa: PLR2004

    async def test_list_of_dicts(self, authenticated_client: AsyncClient) -> None:
        """list-of-dicts with domain key -> rows."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            recent_result=[{"domain": "a.com"}, {"domain": "b.com"}]
        )

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == ["a.com", "b.com"]

    async def test_empty_string_skipped(self, authenticated_client: AsyncClient) -> None:
        """empty-string item skipped; non-str/non-dict item skipped."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(recent_result=["a.com", "", "b.com", 5, None])

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == ["a.com", "b.com"]

    async def test_dict_missing_domain(self, authenticated_client: AsyncClient) -> None:
        """dict item missing/blank domain -> skipped."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            recent_result=[{"domain": "a.com"}, {"domain": ""}, {"other_key": "x"}]
        )

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == ["a.com"]

    async def test_payload_neither_dict_nor_list(self, authenticated_client: AsyncClient) -> None:
        """payload neither dict nor list (e.g. an int) -> rows empty."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(recent_result=123)

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []

    async def test_count_truncation(self, authenticated_client: AsyncClient) -> None:
        """more items than count -> truncated at count."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            recent_result=["a.com", "b.com", "c.com", "d.com", "e.com"]
        )

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked?count=2")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == ["a.com", "b.com"]
        assert body["returned"] == 2  # noqa: PLR2004

    async def test_pihole_error_502(self, authenticated_client: AsyncClient) -> None:
        """PiholeError -> 502."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            recent_result=PiholeError(reason="bad_response", message="test message", status=None)
        )

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked")
        assert response.status_code == _HTTP_BAD_GATEWAY

    async def test_client_uninit_503(self, authenticated_client: AsyncClient) -> None:
        """client uninit -> 503."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = None

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked")
        assert response.status_code == _HTTP_UNAVAILABLE

    async def test_count_bounds(self, authenticated_client: AsyncClient) -> None:
        """count bounds: 0 -> 422, 101 -> 422."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(recent_result=[])

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked?count=0")
        assert response.status_code == _HTTP_UNPROCESSABLE

        response = await authenticated_client.get(
            "/api/integrations/pihole/recent-blocked?count=101"
        )
        assert response.status_code == _HTTP_UNPROCESSABLE

    async def test_dict_payload_blocked_key_missing(
        self, authenticated_client: AsyncClient
    ) -> None:
        """dict payload without 'blocked' key -> rows empty, returned 0."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(recent_result={"other_key": ["x.com"]})

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []
        assert body["returned"] == 0

    async def test_dict_payload_blocked_not_list(self, authenticated_client: AsyncClient) -> None:
        """dict payload with 'blocked' present but not a list -> rows empty."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(recent_result={"blocked": "not-a-list"})

        response = await authenticated_client.get("/api/integrations/pihole/recent-blocked")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []


class TestGetPiholeMessages:
    async def test_happy_v6_plain_field(self, authenticated_client: AsyncClient) -> None:
        """Happy v6 with plain field."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {
                        "id": 1,
                        "timestamp": 1700000000.0,
                        "type": "LIST",
                        "plain": "message text",
                        "html": "<p>message text</p>",
                    },
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["id"] == 1
        assert body["rows"][0]["type"] == "LIST"
        assert body["rows"][0]["message"] == "message text"
        assert body["rows"][0]["timestamp"] == 1700000000.0  # noqa: PLR2004
        assert body["rows"][0]["url"] is None
        assert body["total"] == 1
        assert body["returned"] == 1

    async def test_plain_absent_message_fallback(self, authenticated_client: AsyncClient) -> None:
        """entry missing plain but has message -> message from message."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {
                        "id": 1,
                        "timestamp": 1700000000.0,
                        "type": "LIST",
                        "message": "fallback text",
                    },
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["message"] == "fallback text"

    async def test_both_absent_empty_string(self, authenticated_client: AsyncClient) -> None:
        """entry missing both plain and message -> message ''."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {"id": 1, "timestamp": 1700000000.0, "type": "LIST"},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["message"] == ""

    async def test_non_int_id(self, authenticated_client: AsyncClient) -> None:
        """entry id non-int -> id 0."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {"id": "not_int", "timestamp": 1700000000.0, "type": "LIST", "plain": "text"},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["id"] == 0

    async def test_non_str_type(self, authenticated_client: AsyncClient) -> None:
        """entry type non-str -> 'unknown'."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {"id": 1, "timestamp": 1700000000.0, "type": 123, "plain": "text"},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["type"] == "unknown"

    async def test_missing_timestamp(self, authenticated_client: AsyncClient) -> None:
        """entry missing timestamp -> None."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {"id": 1, "type": "LIST", "plain": "text"},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["timestamp"] is None

    async def test_timestamp_float_and_int(self, authenticated_client: AsyncClient) -> None:
        """timestamp present as int and float both -> float value."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {"id": 1, "type": "LIST", "plain": "text", "timestamp": 1700000000},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["timestamp"] == 1700000000.0  # noqa: PLR2004

    async def test_url_present(self, authenticated_client: AsyncClient) -> None:
        """entry with url present (str) -> url set."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {
                        "id": 1,
                        "type": "LIST",
                        "plain": "text",
                        "timestamp": 1700000000.0,
                        "url": "https://example.com",
                    },
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["url"] == "https://example.com"

    async def test_url_non_str(self, authenticated_client: AsyncClient) -> None:
        """url non-str -> None."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {
                        "id": 1,
                        "type": "LIST",
                        "plain": "text",
                        "timestamp": 1700000000.0,
                        "url": 123,
                    },
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["url"] is None

    async def test_non_dict_entry_skipped(self, authenticated_client: AsyncClient) -> None:
        """non-dict entry in list -> skipped; total counts it."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {"id": 1, "type": "LIST", "plain": "text", "timestamp": 1700000000.0},
                    "not_a_dict",
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["rows"]) == 1
        assert body["total"] == 2  # noqa: PLR2004
        assert body["returned"] == 1

    async def test_messages_not_list(self, authenticated_client: AsyncClient) -> None:
        """messages not a list -> rows empty, total 0."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(messages_result={"messages": "not_a_list"})

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []
        assert body["total"] == 0

    async def test_payload_not_dict(self, authenticated_client: AsyncClient) -> None:
        """payload not a dict -> rows empty, total 0."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(messages_result=[])

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []
        assert body["total"] == 0

    async def test_pihole_error_502(self, authenticated_client: AsyncClient) -> None:
        """PiholeError -> 502."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result=PiholeError(reason="bad_response", message="test message", status=None)
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_BAD_GATEWAY

    async def test_client_uninit_503(self, authenticated_client: AsyncClient) -> None:
        """client uninit -> 503."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = None

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_UNAVAILABLE

    async def test_dict_payload_messages_key_missing(
        self, authenticated_client: AsyncClient
    ) -> None:
        """dict payload without 'messages' key -> rows empty, total 0, returned 0."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(messages_result={"other_key": []})

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []
        assert body["total"] == 0
        assert body["returned"] == 0

    async def test_dict_payload_messages_not_list(self, authenticated_client: AsyncClient) -> None:
        """dict payload with 'messages' present but not a list -> rows empty, total 0."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(messages_result={"messages": "bad"})

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"] == []
        assert body["total"] == 0

    async def test_bool_id_rejected(self, authenticated_client: AsyncClient) -> None:
        """id bool True -> id 0 (bool rejected guard)."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {"id": True, "type": "LIST", "plain": "text", "timestamp": 1700000000.0},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["id"] == 0

    async def test_bool_timestamp_rejected(self, authenticated_client: AsyncClient) -> None:
        """timestamp bool True -> timestamp None (bool rejected guard)."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.pihole_client = _FakeRoClient(
            messages_result={
                "messages": [
                    {"id": 1, "type": "LIST", "plain": "text", "timestamp": True},
                ]
            }
        )

        response = await authenticated_client.get("/api/integrations/pihole/messages")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["rows"][0]["timestamp"] is None


class TestLifespanPiholeVersion:
    @pytest.fixture(autouse=True)
    def _suppress_network_calls(self, httpx_mock: HTTPXMock) -> None:  # pyright: ignore[reportUnusedFunction]
        """Suppress all network calls during lifespan startup.

        Mirrors _suppress_docker_socket_calls from test_lifespan_alertmanager.py
        but adds catch-all response to handle HA/Unifi/Pihole clients.
        """
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://victoriametrics:8428/.*"),
            json={"data": {"resultType": "vector", "result": []}},
            is_optional=True,
            is_reusable=True,
        )
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r".*localhost/events.*"),
            content=b"",
            is_optional=True,
            is_reusable=True,
        )
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r".*localhost/containers/json.*"),
            json=[],
            is_optional=True,
            is_reusable=True,
        )
        httpx_mock.add_response(
            method="POST",
            url=re.compile(r".*localhost/containers/[^/]+/exec.*"),
            json={"Id": "test-exec-id"},
            is_optional=True,
            is_reusable=True,
        )
        httpx_mock.add_response(
            method="POST",
            url=re.compile(r".*localhost/exec/[^/]+/start.*"),
            content=b"",
            is_optional=True,
            is_reusable=True,
        )
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r".*localhost/exec/[^/]+/json.*"),
            json={"ExitCode": 0},
            is_optional=True,
            is_reusable=True,
        )
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://victorialogs:9428/.*"),
            json={},
            is_optional=True,
            is_reusable=True,
        )
        # Catch-all for any remaining calls (HA, Unifi, Pihole clients).
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r".*"),
            json={},
            is_optional=True,
            is_reusable=True,
        )

    @pytest.mark.asyncio
    async def test_success(
        self,
        db_url: str,
        master_key: bytes,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """pihole_version collector startup run completes with full lifespan.

        Mirrors test_lifespan_alertmanager.py pattern to boot the full production
        lifespan while suppressing all network calls and external dependencies.
        The pihole_version startup-run at lifespan.py lines ~1428-1434 must
        execute and complete successfully.
        """
        monkeypatch.setenv("HOMELAB_MONITOR_DB_URL", db_url)
        monkeypatch.setenv("HOMELAB_MONITOR_MASTER_KEY", base64.b64encode(master_key).decode())
        monkeypatch.setenv("HOMELAB_MONITOR_DOCKER_ENABLED", "false")
        monkeypatch.setenv("HOMELAB_MONITOR_HA_URL", "http://127.0.0.1:0")
        monkeypatch.setenv("HOMELAB_MONITOR_UNIFI_URL", "http://127.0.0.1:0")
        monkeypatch.setenv("HOMELAB_MONITOR_PIHOLE_URL", "http://127.0.0.1:0")
        monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "disabled")

        app = create_app(lifespan_enabled=True)
        async with app.router.lifespan_context(app):
            # Lifespan completed; pihole_version startup-run executed.
            pass


class TestPiholePanelAuth:
    """Unauthenticated requests to all panel GET endpoints must return 401."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/api/integrations/pihole/overview",
            "/api/integrations/pihole/adlists",
            "/api/integrations/pihole/upstreams",
            "/api/integrations/pihole/unbound",
            "/api/integrations/pihole/clients",
            "/api/integrations/pihole/recent-blocked",
            "/api/integrations/pihole/messages",
        ],
    )
    async def test_requires_auth(self, unauthenticated_client: AsyncClient, path: str) -> None:
        """No auth -> 401 for every panel GET endpoint."""
        resp = await unauthenticated_client.get(path)
        assert resp.status_code == _HTTP_UNAUTH
