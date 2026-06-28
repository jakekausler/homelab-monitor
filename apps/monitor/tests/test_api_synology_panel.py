"""Tests for STAGE-008-024 Synology panel data endpoints.

Covers GET /api/integrations/synology/{summary,hardware,ops,disks/{disk}/smart-attrs,connections}.

VM-sourced endpoints: HTTPXMock stubs /api/v1/query. /connections is live: a fake
SynologyRestClient is set on app.state. Branch coverage: present/absent series,
data_available true/false, /connections success/SynologyError/client-missing-503,
VM-502, auth-401.
"""

from __future__ import annotations

import re
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pytest_httpx import HTTPXMock

from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError

_HTTP_OK = 200
_HTTP_UNAUTH = 401
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


class _FakeSynologyClient:
    """Minimal fake SynologyRestClient for /connections tests."""

    def __init__(self, *, result: object | SynologyError) -> None:
        self._result = result

    async def current_connection_list(self) -> SynologyResponse | SynologyError:
        if isinstance(self._result, SynologyError):
            return self._result
        return SynologyResponse(
            payload=self._result,
            took_seconds=0.0,
            endpoint="SYNO.Core.CurrentConnection/list",
        )


class TestGetSynologySummary:
    async def test_happy_all_present(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            query_responses = {
                "homelab_synology_health_ok": _vector([({}, "1")]),
                "max(homelab_synology_volume_used_percent)": _vector([({}, "73.4")]),
                "homelab_synology_ups_on_battery": _vector([({}, "0")]),
                "homelab_synology_ups_charge_percent": _vector([({}, "100")]),
                "homelab_synology_dsm_update_available": _vector([({}, "1")]),
                "homelab_synology_security_safe": _vector([({}, "1")]),
                "homelab_synology_no_backup_configured": _vector([({}, "0")]),
            }
            return httpx.Response(200, json=query_responses.get(query, _empty_vector()))

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/synology/summary")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["dsm_up"] is True
        assert body["volume_used_percent_max"] == 73.4  # noqa: PLR2004
        assert body["ups_on_battery"] is False
        assert body["ups_charge_percent"] == 100.0  # noqa: PLR2004
        assert body["update_available"] is True
        assert body["security_safe"] is True
        assert body["backup_configured"] is True
        assert body["last_seen"] is not None

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
        response = await authenticated_client.get("/api/integrations/synology/summary")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["dsm_up"] is False
        assert body["volume_used_percent_max"] is None
        assert body["ups_on_battery"] is False
        assert body["ups_charge_percent"] is None
        assert body["update_available"] is False
        assert body["security_safe"] is False
        # no_backup_configured absent -> _bool_metric False -> backup_configured True
        assert body["backup_configured"] is True
        assert body["last_seen"] is None

    async def test_no_backup_configured_true(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """no_backup_configured == 1 -> backup_configured False."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            if _query_of(request) == "homelab_synology_no_backup_configured":
                return httpx.Response(200, json=_vector([({}, "1")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/synology/summary")
        assert response.status_code == _HTTP_OK
        assert response.json()["backup_configured"] is False

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
        response = await authenticated_client.get("/api/integrations/synology/summary")
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
            if query == "homelab_synology_ups_charge_percent":
                return httpx.Response(200, json=_vector([({}, "notafloat")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/synology/summary")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["ups_charge_percent"] is None


class TestGetSynologyHardware:
    async def test_happy_all_present(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            query_responses = {
                "homelab_synology_volume_used_percent": _vector([({"volume": "volume_1"}, "73.4")]),
                "homelab_synology_volume_status": _vector(
                    [({"volume": "volume_1", "status": "normal"}, "1")]
                ),
                "homelab_synology_pool_status": _vector(
                    [({"pool": "pool_1", "status": "normal"}, "1")]
                ),
                "homelab_synology_raid_status": _vector(
                    [({"pool": "pool_1", "raid": "raid_5"}, "1")]
                ),
                "homelab_synology_disk_status": _vector([({"disk": "sda"}, "1")]),
                "homelab_synology_disk_smart_status": _vector([({"disk": "sda"}, "1")]),
                "homelab_synology_disk_temp_celsius": _vector(
                    [({"disk": "sda", "model": "M"}, "38")]
                ),
                "homelab_synology_smart_attr_failing": _vector([({"disk": "sda"}, "0")]),
                "homelab_synology_system_uptime_seconds": _vector([({}, "123456")]),
                "homelab_synology_sys_temp_celsius": _vector([({}, "41")]),
                "homelab_synology_need_reboot": _vector([({}, "0")]),
                "homelab_synology_info": _vector(
                    [
                        (
                            {
                                "model": "DS920+",
                                "serial": "ABC123",
                                "firmware": "DSM 7.2",
                            },
                            "1",
                        )
                    ]
                ),
                "homelab_synology_fan_status": _vector([({"state": "normal"}, "1")]),
                "homelab_synology_health_ok": _vector([({}, "1")]),
                "homelab_synology_ups_connected": _vector([({}, "1")]),
                "homelab_synology_ups_on_battery": _vector([({}, "0")]),
                "homelab_synology_ups_charge_percent": _vector([({}, "100")]),
                "homelab_synology_ssh_load1": _vector([({}, "0.42")]),
                "homelab_synology_ssh_cpu_temp_celsius": _vector([({}, "45")]),
                "homelab_synology_mdstat_array_degraded": _vector([({"array": "md0"}, "0")]),
                'homelab_collector_run_success_total{name="synology-probe"}': _vector(
                    [({"name": "synology-probe"}, "5")]
                ),
            }
            return httpx.Response(200, json=query_responses.get(query, _empty_vector()))

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/synology/hardware")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert len(body["volumes"]) == 1
        assert body["volumes"][0]["volume"] == "volume_1"
        assert body["volumes"][0]["used_percent"] == 73.4  # noqa: PLR2004
        assert body["volumes"][0]["status"] == "normal"
        assert len(body["pools"]) == 1
        assert body["pools"][0]["pool"] == "pool_1"
        assert body["pools"][0]["status"] == "normal"
        assert body["pools"][0]["raid_status"] == "raid_5"
        assert len(body["disks"]) == 1
        assert body["disks"][0]["disk"] == "sda"
        assert body["disks"][0]["smart_status"] == 1.0
        assert body["disks"][0]["temp_celsius"] == 38.0  # noqa: PLR2004
        assert body["disks"][0]["smart_attr_failing"] is False
        assert body["system"]["health_ok"] is True
        assert body["system"]["uptime_seconds"] == 123456.0  # noqa: PLR2004
        assert body["system"]["model"] == "DS920+"
        assert body["system"]["serial"] == "ABC123"
        assert body["system"]["firmware"] == "DSM 7.2"
        assert len(body["system"]["fans"]) == 1
        assert body["system"]["fans"][0]["state"] == "normal"
        assert body["ups"]["connected"] is True
        assert body["ups"]["on_battery"] is False
        assert body["ups"]["charge_percent"] == 100.0  # noqa: PLR2004
        assert body["ssh_probe"]["load1"] == 0.42  # noqa: PLR2004
        assert body["ssh_probe"]["cpu_temp_celsius"] == 45.0  # noqa: PLR2004
        assert body["ssh_probe"]["mdstat_array_degraded"] is False
        assert body["ssh_probe_data_available"] is True

    async def test_smart_failing_and_info_absent(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """smart_attr_failing == 1 -> True; info absent -> model/serial/firmware None;
        ssh probe self-metric absent -> ssh_probe_data_available False."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_synology_disk_status":
                return httpx.Response(200, json=_vector([({"disk": "sda"}, "1")]))
            if query == "homelab_synology_smart_attr_failing":
                return httpx.Response(200, json=_vector([({"disk": "sda"}, "1")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/synology/hardware")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["disks"][0]["smart_attr_failing"] is True
        assert body["system"]["model"] is None
        assert body["system"]["serial"] is None
        assert body["system"]["firmware"] is None
        assert body["ssh_probe_data_available"] is False

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
        response = await authenticated_client.get("/api/integrations/synology/hardware")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["volumes"] == []
        assert body["pools"] == []
        assert body["disks"] == []
        assert body["system"]["fans"] == []
        assert body["system"]["health_ok"] is False
        assert body["ssh_probe_data_available"] is False

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
        response = await authenticated_client.get("/api/integrations/synology/hardware")
        assert response.status_code == _HTTP_BAD_GATEWAY


class TestGetSynologyOps:
    async def test_happy_all_present(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            query_responses = {
                "homelab_synology_backup_configured_count": _vector([({}, "2")]),
                "homelab_synology_no_backup_configured": _vector([({}, "0")]),
                "homelab_synology_backup_last_result_ok": _vector([({"job": "j1"}, "1")]),
                "homelab_synology_snapshot_count": _vector([({"share": "share_a"}, "12")]),
                "homelab_synology_replication_available": _vector([({}, "1")]),
                "homelab_synology_dsm_update_available": _vector([({}, "1")]),
                "homelab_synology_packages_with_updates_count": _vector([({}, "3")]),
                "homelab_synology_package_update_available": _vector([({"package": "Plex"}, "1")]),
                "homelab_synology_security_findings_total": _vector(
                    [({"severity": "warning"}, "4")]
                ),
                "homelab_synology_security_safe": _vector([({}, "1")]),
                "homelab_synology_mount_up": _vector([({"mount": "/mnt/a"}, "1")]),
                "homelab_synology_mount_free_bytes": _vector([({"mount": "/mnt/a"}, "9000")]),
                'homelab_collector_run_success_total{name="synology_mount_health"}': _vector(
                    [({"name": "synology_mount_health"}, "7")]
                ),
            }
            return httpx.Response(200, json=query_responses.get(query, _empty_vector()))

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/synology/ops")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["backup"]["configured_count"] == 2  # noqa: PLR2004
        assert body["backup"]["no_backup_configured"] is False
        assert body["backup"]["last_result_ok"] is True
        assert len(body["replication"]["shares"]) == 1
        assert body["replication"]["shares"][0]["share"] == "share_a"
        assert body["replication"]["shares"][0]["snapshot_count"] == 12.0  # noqa: PLR2004
        assert body["replication"]["replication_available"] is True
        assert body["updates"]["dsm_update_available"] is True
        assert body["updates"]["packages_with_updates_count"] == 3  # noqa: PLR2004
        assert len(body["updates"]["packages"]) == 1
        assert body["updates"]["packages"][0]["package"] == "Plex"
        assert body["updates"]["packages"][0]["update_available"] is True
        assert len(body["security"]["findings"]) == 1
        assert body["security"]["findings"][0]["severity"] == "warning"
        assert body["security"]["findings"][0]["count"] == 4.0  # noqa: PLR2004
        assert body["security"]["security_safe"] is True
        assert len(body["mounts"]) == 1
        assert body["mounts"][0]["mount"] == "/mnt/a"
        assert body["mounts"][0]["mount_up"] is True
        assert body["mounts"][0]["mount_free_bytes"] == 9000.0  # noqa: PLR2004
        assert body["mount_data_available"] is True

    async def test_backup_ok_false_and_package_not_available(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """backup_last_result_ok == 0 -> False; package update == 0 -> False;
        mount_up == 0 -> False."""
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == "homelab_synology_backup_last_result_ok":
                return httpx.Response(200, json=_vector([({"job": "j1"}, "0")]))
            if query == "homelab_synology_package_update_available":
                return httpx.Response(200, json=_vector([({"package": "Plex"}, "0")]))
            if query == "homelab_synology_mount_up":
                return httpx.Response(200, json=_vector([({"mount": "/mnt/a"}, "0")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get("/api/integrations/synology/ops")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["backup"]["last_result_ok"] is False
        assert body["updates"]["packages"][0]["update_available"] is False
        assert body["mounts"][0]["mount_up"] is False

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
        response = await authenticated_client.get("/api/integrations/synology/ops")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["backup"]["configured_count"] == 0
        assert body["backup"]["last_result_ok"] is None
        assert body["replication"]["shares"] == []
        assert body["updates"]["packages"] == []
        assert body["updates"]["packages_with_updates_count"] == 0
        assert body["security"]["findings"] == []
        assert body["mounts"] == []
        assert body["mount_data_available"] is False

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
        response = await authenticated_client.get("/api/integrations/synology/ops")
        assert response.status_code == _HTTP_BAD_GATEWAY


class TestGetSynologyDiskSmartAttrs:
    async def test_happy_present(
        self,
        authenticated_client: AsyncClient,
        httpx_mock: HTTPXMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", _VM_URL)

        def vm_callback(request: httpx.Request) -> httpx.Response:
            query = _query_of(request)
            if query == 'homelab_synology_smart_attr_raw{disk="sda"}':
                return httpx.Response(
                    200,
                    json=_vector(
                        [({"disk": "sda", "attr_id": "5", "attr_name": "Reallocated"}, "0")]
                    ),
                )
            if query == 'homelab_synology_smart_attr_worst{disk="sda"}':
                return httpx.Response(200, json=_vector([({"disk": "sda", "attr_id": "5"}, "100")]))
            if query == 'homelab_synology_smart_attr_threshold{disk="sda"}':
                return httpx.Response(200, json=_vector([({"disk": "sda", "attr_id": "5"}, "10")]))
            if query == 'homelab_collector_run_success_total{name="synology-probe"}':
                return httpx.Response(200, json=_vector([({"name": "synology-probe"}, "5")]))
            return httpx.Response(200, json=_empty_vector())

        httpx_mock.add_callback(
            vm_callback,
            url=re.compile(r"http://vm-test:8428/api/v1/query\b.*"),
            method="GET",
            is_reusable=True,
        )
        response = await authenticated_client.get(
            "/api/integrations/synology/disks/sda/smart-attrs"
        )
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["disk"] == "sda"
        assert len(body["attrs"]) == 1
        assert body["attrs"][0]["attr_id"] == "5"
        assert body["attrs"][0]["attr_name"] == "Reallocated"
        assert body["attrs"][0]["raw"] == 0.0
        assert body["attrs"][0]["worst"] == 100.0  # noqa: PLR2004
        assert body["attrs"][0]["threshold"] == 10.0  # noqa: PLR2004
        assert body["data_available"] is True

    async def test_absent(
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
        response = await authenticated_client.get(
            "/api/integrations/synology/disks/sda/smart-attrs"
        )
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["disk"] == "sda"
        assert body["attrs"] == []
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
        response = await authenticated_client.get(
            "/api/integrations/synology/disks/sda/smart-attrs"
        )
        assert response.status_code == _HTTP_BAD_GATEWAY


class TestGetSynologyConnections:
    async def test_success_who_from_keys(self, authenticated_client: AsyncClient) -> None:
        """DSM-shape payload with who/from keys; one non-dict item is skipped."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.synology_client = _FakeSynologyClient(
            result={
                "items": [
                    {"who": "admin", "from": "192.168.2.50", "type": "SMB"},
                    "not-a-dict",
                    {"who": None, "from": None, "type": None},
                ]
            }
        )
        response = await authenticated_client.get("/api/integrations/synology/connections")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["data_available"] is True
        assert len(body["connections"]) == 2  # noqa: PLR2004
        assert body["connections"][0]["user"] == "admin"
        assert body["connections"][0]["ip"] == "192.168.2.50"
        assert body["connections"][0]["type"] == "SMB"
        assert body["connections"][1]["user"] == ""
        assert body["connections"][1]["ip"] == ""
        assert body["connections"][1]["type"] == ""

    async def test_success_user_ip_fallback_keys(self, authenticated_client: AsyncClient) -> None:
        """Fallback user/ip keys exercised."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.synology_client = _FakeSynologyClient(
            result={"items": [{"user": "bob", "ip": "10.0.0.9", "type": "AFP"}]}
        )
        response = await authenticated_client.get("/api/integrations/synology/connections")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["connections"][0]["user"] == "bob"
        assert body["connections"][0]["ip"] == "10.0.0.9"
        assert body["connections"][0]["type"] == "AFP"

    async def test_payload_not_dict(self, authenticated_client: AsyncClient) -> None:
        """Non-dict payload -> empty connections, data_available True."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.synology_client = _FakeSynologyClient(result=["unexpected"])
        response = await authenticated_client.get("/api/integrations/synology/connections")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["connections"] == []
        assert body["data_available"] is True

    async def test_items_not_list(self, authenticated_client: AsyncClient) -> None:
        """Payload dict but items not a list -> empty connections, data_available True."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.synology_client = _FakeSynologyClient(result={"items": "nope"})
        response = await authenticated_client.get("/api/integrations/synology/connections")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["connections"] == []
        assert body["data_available"] is True

    async def test_synology_error_degrades(self, authenticated_client: AsyncClient) -> None:
        """SynologyError -> data_available False, empty list, NOT 502."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.synology_client = _FakeSynologyClient(
            result=SynologyError(reason="unreachable", message="down", status=None)
        )
        response = await authenticated_client.get("/api/integrations/synology/connections")
        assert response.status_code == _HTTP_OK
        body = response.json()
        assert body["connections"] == []
        assert body["data_available"] is False

    async def test_client_uninit_503(self, authenticated_client: AsyncClient) -> None:
        """client uninitialized -> 503."""
        app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
        app.state.synology_client = None
        response = await authenticated_client.get("/api/integrations/synology/connections")
        assert response.status_code == _HTTP_UNAVAILABLE


class TestSynologyPanelAuth:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/integrations/synology/summary",
            "/api/integrations/synology/hardware",
            "/api/integrations/synology/ops",
            "/api/integrations/synology/disks/sda/smart-attrs",
            "/api/integrations/synology/connections",
        ],
    )
    async def test_requires_auth(self, unauthenticated_client: AsyncClient, path: str) -> None:
        resp = await unauthenticated_client.get(path)
        assert resp.status_code == _HTTP_UNAUTH
