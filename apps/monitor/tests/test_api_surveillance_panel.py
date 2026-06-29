"""Tests for STAGE-008-024 Surveillance Station panel data endpoints.

Covers GET /api/integrations/surveillance/{summary,cameras}. VM-sourced;
branch coverage: present/absent series, data_available true/false, VM-502, auth-401.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from httpx import AsyncClient
from pytest_httpx import HTTPXMock

_HTTP_OK = 200
_HTTP_UNAUTH = 401
_HTTP_BAD_GATEWAY = 502
_VM_URL = "http://vm-test:8428"


def _vector(
    samples: list[tuple[dict[str, str], str]], ts: float = 1714867200.0
) -> dict[str, object]:
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


class TestGetSurveillanceSummary:
    async def test_happy_all_present(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
            query = _query_of(request)
            if query == "homelab_synology_ss_info_license_used":
                return httpx.Response(200, json=_vector([({}, "4")]))
            if query == "homelab_synology_ss_info_license_max":
                return httpx.Response(200, json=_vector([({}, "8")]))
            if query == "homelab_synology_ss_homemode_on":
                return httpx.Response(200, json=_vector([({}, "1")]))
            if query == "homelab_synology_ss_cameras_total":
                return httpx.Response(200, json=_vector([({}, "4")]))
            if query == "homelab_synology_ss_cameras_connected_total":
                return httpx.Response(200, json=_vector([({}, "3")]))
            if query == "homelab_synology_ss_cameras_disconnected_total":
                return httpx.Response(200, json=_vector([({}, "1")]))
            if query == 'homelab_collector_run_success_total{name="synology_cameras"}':
                return httpx.Response(200, json=_vector([({"name": "synology_cameras"}, "9")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/surveillance/summary")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["license_used"] == 4.0  # noqa: PLR2004
        assert body["license_max"] == 8.0  # noqa: PLR2004
        assert body["homemode_on"] is True
        assert body["cameras_total"] == 4.0  # noqa: PLR2004
        assert body["cameras_connected_total"] == 3.0  # noqa: PLR2004
        assert body["cameras_disconnected_total"] == 1.0
        assert body["data_available"] is True

    async def test_all_absent(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/surveillance/summary")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["license_used"] is None
        assert body["license_max"] is None
        assert body["homemode_on"] is False
        assert body["cameras_total"] is None
        assert body["cameras_connected_total"] is None
        assert body["cameras_disconnected_total"] is None
        assert body["data_available"] is False

    async def test_vm_down_502(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/surveillance/summary")
        assert response.status_code == _HTTP_BAD_GATEWAY

    async def test_non_numeric_sample_value(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-numeric value_str in a scalar field -> _sample_float returns None."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_synology_ss_info_license_used":
                return httpx.Response(200, json=_vector([({}, "notanumber")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/surveillance/summary")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["license_used"] is None


class TestGetSurveillanceCameras:
    async def test_happy_present(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
            query = _query_of(request)
            if query == "homelab_synology_ss_camera_connected":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"camera": "FrontDoor"}, "1"),
                            ({"camera": "Garage"}, "0"),
                        ]
                    ),
                )
            if query == "homelab_synology_ss_camera_status":
                return httpx.Response(200, json=_vector([({"camera": "FrontDoor"}, "3")]))
            if query == "homelab_synology_ss_recordings_count":
                return httpx.Response(200, json=_vector([({"camera": "FrontDoor"}, "42")]))
            if query == "homelab_synology_ss_recordings_bytes":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            ({"camera": "FrontDoor"}, "123456789"),
                            ({"camera": "Garage"}, "0"),
                        ]
                    ),
                )
            if query == "homelab_synology_ss_events_today":
                return httpx.Response(200, json=_vector([({}, "5")]))
            if query == "homelab_synology_ss_events_total_all":
                return httpx.Response(200, json=_vector([({}, "1000")]))
            if query == "homelab_synology_ss_recordings_total":
                return httpx.Response(200, json=_vector([({}, "777")]))
            if query == "homelab_synology_ss_recordings_bytes_total":
                return httpx.Response(200, json=_vector([({}, "987654321")]))
            if query == "homelab_synology_ss_camera_info":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            (
                                {
                                    "camera": "FrontDoor",
                                    "id": "1",
                                    "ip": "192.168.2.50",
                                    "model": "TV-IP1314PI",
                                    "vendor": "TRENDnet",
                                    "mac": "aa:bb:cc:dd:ee:ff",
                                },
                                "1",
                            )
                        ]
                    ),
                )
            if query == 'homelab_collector_run_success_total{name="synology_cameras"}':
                return httpx.Response(200, json=_vector([({"name": "synology_cameras"}, "9")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/surveillance/cameras")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["cameras"]) == 2  # noqa: PLR2004
        assert body["cameras"][0]["camera"] == "FrontDoor"
        assert body["cameras"][0]["connected"] is True
        assert body["cameras"][0]["status"] == 3.0  # noqa: PLR2004
        assert body["cameras"][0]["recordings_count"] == 42.0  # noqa: PLR2004
        assert body["cameras"][0]["recordings_bytes"] == 123456789.0  # noqa: PLR2004
        # FrontDoor present in _info -> model/ip/vendor populated
        assert body["cameras"][0]["model"] == "TV-IP1314PI"
        assert body["cameras"][0]["ip"] == "192.168.2.50"
        assert body["cameras"][0]["vendor"] == "TRENDnet"
        assert body["cameras"][1]["camera"] == "Garage"
        assert body["cameras"][1]["connected"] is False
        assert body["cameras"][1]["status"] is None
        assert body["cameras"][1]["recordings_count"] is None
        # Garage present-but-zero in bytes_idx -> honest 0.0 (not None)
        assert body["cameras"][1]["recordings_bytes"] == 0.0
        # Garage absent from _info -> honest nulls (NOT fabricated)
        assert body["cameras"][1]["model"] is None
        assert body["cameras"][1]["ip"] is None
        assert body["cameras"][1]["vendor"] is None
        assert body["events_today"] == 5.0  # noqa: PLR2004
        assert body["events_total_all"] == 1000.0  # noqa: PLR2004
        assert body["recordings_total"] == 777.0  # noqa: PLR2004
        assert body["recordings_bytes_total"] == 987654321.0  # noqa: PLR2004
        assert body["data_available"] is True

    async def test_all_absent(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/surveillance/cameras")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["cameras"] == []
        assert body["events_today"] is None
        assert body["events_total_all"] is None
        assert body["recordings_total"] is None
        assert body["recordings_bytes_total"] is None
        assert body["data_available"] is False

    async def test_vm_down_502(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/surveillance/cameras")
        assert response.status_code == _HTTP_BAD_GATEWAY

    async def test_info_partial_labels(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Camera present in _info but missing the 'vendor' label -> vendor None."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_synology_ss_camera_connected":
                return httpx.Response(200, json=_vector([({"camera": "FrontDoor"}, "1")]))
            if query == "homelab_synology_ss_camera_info":
                return httpx.Response(
                    200,
                    json=_vector([({"camera": "FrontDoor", "ip": "10.0.0.5", "model": "X"}, "1")]),
                )
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/surveillance/cameras")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["cameras"][0]["model"] == "X"
        assert body["cameras"][0]["ip"] == "10.0.0.5"
        assert body["cameras"][0]["vendor"] is None

    async def test_info_empty_string_label_is_none(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Camera with empty-string label (vendor="") -> vendor None (coalesced)."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_synology_ss_camera_connected":
                return httpx.Response(200, json=_vector([({"camera": "FrontDoor"}, "1")]))
            if query == "homelab_synology_ss_camera_info":
                return httpx.Response(
                    200,
                    json=_vector(
                        [
                            (
                                {
                                    "camera": "FrontDoor",
                                    "ip": "192.168.2.50",
                                    "model": "TV-IP1314PI",
                                    "vendor": "",
                                },
                                "1",
                            )
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
        response = await authenticated_client.get("/api/integrations/surveillance/cameras")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["cameras"][0]["model"] == "TV-IP1314PI"
        assert body["cameras"][0]["ip"] == "192.168.2.50"
        assert body["cameras"][0]["vendor"] is None


class TestSurveillancePanelAuth:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/integrations/surveillance/summary",
            "/api/integrations/surveillance/cameras",
        ],
    )
    async def test_requires_auth(self, unauthenticated_client: AsyncClient, path: str) -> None:
        resp = await unauthenticated_client.get(path)
        assert resp.status_code == _HTTP_UNAUTH
