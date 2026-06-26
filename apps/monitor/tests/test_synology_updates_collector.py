"""Unit tests for the synology_updates collector (STAGE-008-012, fixture-based).

100% branch coverage of updates.py. Field names are LIVE-VERIFIED (captured JSON).
Exercises the CO-EQUAL combine (ok=False ONLY when ALL THREE fetches fail), the
always-emit 0-baseline scalars, and every emit-if-present guard's BOTH sides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from homelab_monitor.kernel.plugins.context import CollectorContext

from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology.updates import (
    M_DSM_UPDATE_AVAILABLE,
    M_DSM_UPDATE_INFO,
    M_DSM_UPDATE_IS_SECURITY,
    M_PACKAGE_COUNT,
    M_PACKAGE_INFO,
    M_PACKAGE_UPDATE_AVAILABLE,
    M_PACKAGES_WITH_UPDATES_COUNT,
    SynologyUpdatesCollector,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 3600.0
_EXPECTED_TIMEOUT = 30.0

# 7 cap-routed families emitted by _emit.
_FAMILY_COUNT = 7

# Three co-equal fetches: upgrade + server + package.
_EXPECTED_API_TOOK_COUNT = 3

_EXPECTED_PKG_COUNT_3 = 3.0
_EXPECTED_PKG_COUNT_1 = 1.0
_EXPECTED_WITH_UPDATES_1 = 1.0
_EXPECTED_WITH_UPDATES_0 = 0.0
_DSM_VERSION = "DSM 7.3.2-86009 Update 3"


def _resp(payload: object, endpoint: str) -> SynologyResponse:
    return SynologyResponse(payload=payload, took_seconds=0.5, endpoint=endpoint)


def _upgrade_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.Upgrade.Server/check")


def _pkg_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.Package/list")


def _server_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.Package.Server/list")


def _empty_upgrade_payload() -> dict[str, object]:
    return {"update": {"available": False}}


def _empty_pkg_payload() -> dict[str, object]:
    return {"packages": [], "total": 0}


def _empty_server_payload() -> dict[str, object]:
    return {"packages": [], "beta_packages": []}


class _FakeSynology:
    """Stand-in for ctx.synology with 3 independently programmable methods."""

    def __init__(
        self, upgrade: object = None, packages: object = None, server: object = None
    ) -> None:
        self._upgrade = upgrade if upgrade is not None else _upgrade_resp(_empty_upgrade_payload())
        self._packages = packages if packages is not None else _pkg_resp(_empty_pkg_payload())
        self._server = server if server is not None else _server_resp(_empty_server_payload())

    async def upgrade_check(self) -> object:
        return self._upgrade

    async def package_list(self) -> object:
        return self._packages

    async def package_server_list(self) -> object:
        return self._server


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in updates tests."""

    vm: MemoryRetainingMetricsWriter = field(default_factory=MemoryRetainingMetricsWriter)
    synology: object = None


def _ctx(writer: MemoryRetainingMetricsWriter, synology: object) -> _Ctx:
    return _Ctx(vm=writer, synology=synology)


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[str, float, dict[str, str]]]:
    return [g for g in writer.gauges if g[0] == name]


# --- ClassVar tests ---


def test_updates_classvars() -> None:
    """ClassVars match expected constants."""
    assert SynologyUpdatesCollector.name == "synology_updates"
    assert SynologyUpdatesCollector.interval == timedelta(seconds=3600)
    assert SynologyUpdatesCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologyUpdatesCollector.timeout == timedelta(seconds=30)
    assert SynologyUpdatesCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologyUpdatesCollector.concurrency_group == "synology"


# --- Row 1: update available + security (live shape) ---


async def test_updates_available_and_security() -> None:
    """available:True, isSecurityVersion:True, version+type present."""
    upgrade = {
        "update": {
            "available": True,
            "type": "nano",
            "version": _DSM_VERSION,
            "version_details": {"isSecurityVersion": True},
        }
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(upgrade=_upgrade_resp(upgrade))))

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_DSM_UPDATE_AVAILABLE) == [(M_DSM_UPDATE_AVAILABLE, 1.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_IS_SECURITY) == [(M_DSM_UPDATE_IS_SECURITY, 1.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_INFO) == [
        (M_DSM_UPDATE_INFO, 1.0, {"available_version": _DSM_VERSION, "type": "nano"})
    ]


# --- Row 2: no update — available:False ---


async def test_updates_not_available() -> None:
    """{"update": {"available": False}} -> available=0, is_security=0, no info."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_DSM_UPDATE_AVAILABLE) == [(M_DSM_UPDATE_AVAILABLE, 0.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_IS_SECURITY) == [(M_DSM_UPDATE_IS_SECURITY, 0.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_INFO) == []


# --- Row 3: no update — update key ABSENT ---


async def test_updates_update_key_absent() -> None:
    """Payload with no 'update' key -> as_dict(nested) None -> defaults hold."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(upgrade=_upgrade_resp({"other": 1}))))

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_DSM_UPDATE_AVAILABLE) == [(M_DSM_UPDATE_AVAILABLE, 0.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_IS_SECURITY) == [(M_DSM_UPDATE_IS_SECURITY, 0.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_INFO) == []


# --- Row 4: no update — update non-dict ---


async def test_updates_update_non_dict() -> None:
    """{"update": "garbage"} -> as_dict None -> defaults hold."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(upgrade=_upgrade_resp({"update": "garbage"}))),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_DSM_UPDATE_AVAILABLE) == [(M_DSM_UPDATE_AVAILABLE, 0.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_INFO) == []


# --- Row 5: available but version MISSING ---


async def test_updates_available_no_version() -> None:
    """available:True but no version -> available=1, is_security=0, no info."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(upgrade=_upgrade_resp({"update": {"available": True}}))),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_DSM_UPDATE_AVAILABLE) == [(M_DSM_UPDATE_AVAILABLE, 1.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_IS_SECURITY) == [(M_DSM_UPDATE_IS_SECURITY, 0.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_INFO) == []


# --- Row 6: available, is_security key absent ---


async def test_updates_available_no_security_details() -> None:
    """available + version present, no version_details -> is_security=0, info present."""
    upgrade = {"update": {"available": True, "version": "X", "type": "nano"}}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(upgrade=_upgrade_resp(upgrade))))

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_DSM_UPDATE_IS_SECURITY) == [(M_DSM_UPDATE_IS_SECURITY, 0.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_INFO) == [
        (M_DSM_UPDATE_INFO, 1.0, {"available_version": "X", "type": "nano"})
    ]


# --- Row 7: type label absent on info ---


async def test_updates_info_no_type_label() -> None:
    """available + version present, no type -> info has only available_version."""
    upgrade = {"update": {"available": True, "version": "X"}}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(upgrade=_upgrade_resp(upgrade))))

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_DSM_UPDATE_INFO) == [
        (M_DSM_UPDATE_INFO, 1.0, {"available_version": "X"})
    ]


# --- Row 8: packages present, mixed ---


async def test_updates_packages_present() -> None:
    """3 installed packages -> package_info x3, package_count=3."""
    pkg = {
        "packages": [
            {"id": "A", "name": "Pkg A", "version": "1.0"},
            {"id": "B", "name": "Pkg B", "version": "2.0"},
            {"id": "C", "name": "Pkg C", "version": "3.0"},
        ],
        "total": 3,
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(packages=_pkg_resp(pkg))))

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_PACKAGE_INFO)) == int(_EXPECTED_PKG_COUNT_3)
    assert _gauges_named(writer, M_PACKAGE_COUNT) == [(M_PACKAGE_COUNT, _EXPECTED_PKG_COUNT_3, {})]
    assert _gauges_named(writer, M_PACKAGE_INFO)[0] == (
        M_PACKAGE_INFO,
        1.0,
        {"package": "A", "name": "Pkg A", "version": "1.0"},
    )


# --- Row 9: package with no id -> skipped from info but still counted ---


async def test_updates_package_no_id_counted_not_emitted() -> None:
    """One record missing id -> not in package_info, still in package_count."""
    pkg = {
        "packages": [
            {"id": "A", "name": "Pkg A", "version": "1.0"},
            {"name": "No Id", "version": "9.9"},
        ],
        "total": 2,
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(packages=_pkg_resp(pkg))))

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    info = _gauges_named(writer, M_PACKAGE_INFO)
    assert len(info) == 1
    assert info[0][2]["package"] == "A"
    assert _gauges_named(writer, M_PACKAGE_COUNT) == [(M_PACKAGE_COUNT, 2.0, {})]


# --- Row 10: package name/version label absent ---


async def test_updates_package_missing_labels() -> None:
    """One package missing name, one missing version -> labels omitted accordingly."""
    pkg = {
        "packages": [
            {"id": "A", "version": "1.0"},
            {"id": "B", "name": "Pkg B"},
        ],
        "total": 2,
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology(packages=_pkg_resp(pkg))))

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    info = _gauges_named(writer, M_PACKAGE_INFO)
    by_pkg = {g[2]["package"]: g[2] for g in info}
    assert by_pkg["A"] == {"package": "A", "version": "1.0"}
    assert by_pkg["B"] == {"package": "B", "name": "Pkg B"}


# --- Row 11: per-package update, DIFFERENT version -> 1 ---


async def test_updates_package_update_available() -> None:
    """installed 1.0 + non-beta server 2.0 -> update=1, count=1."""
    pkg = {"packages": [{"id": "Foo", "version": "1.0"}], "total": 1}
    server = {"packages": [{"id": "Foo", "version": "2.0", "beta": False}], "beta_packages": []}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(packages=_pkg_resp(pkg), server=_server_resp(server))),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_PACKAGE_UPDATE_AVAILABLE) == [
        (M_PACKAGE_UPDATE_AVAILABLE, 1.0, {"package": "Foo"})
    ]
    assert _gauges_named(writer, M_PACKAGES_WITH_UPDATES_COUNT) == [
        (M_PACKAGES_WITH_UPDATES_COUNT, _EXPECTED_WITH_UPDATES_1, {})
    ]


# --- Row 12: per-package update, SAME version -> 0 ---


async def test_updates_package_same_version() -> None:
    """installed 1.0 + non-beta server 1.0 -> update=0, count=0."""
    pkg = {"packages": [{"id": "Foo", "version": "1.0"}], "total": 1}
    server = {"packages": [{"id": "Foo", "version": "1.0", "beta": False}], "beta_packages": []}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(packages=_pkg_resp(pkg), server=_server_resp(server))),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_PACKAGE_UPDATE_AVAILABLE) == [
        (M_PACKAGE_UPDATE_AVAILABLE, 0.0, {"package": "Foo"})
    ]
    assert _gauges_named(writer, M_PACKAGES_WITH_UPDATES_COUNT) == [
        (M_PACKAGES_WITH_UPDATES_COUNT, _EXPECTED_WITH_UPDATES_0, {})
    ]


# --- Row 13: per-package update, no server match -> 0 ---


async def test_updates_package_no_server_match() -> None:
    """installed id not in server map -> update=0."""
    pkg = {"packages": [{"id": "Foo", "version": "1.0"}], "total": 1}
    server = {"packages": [{"id": "Bar", "version": "2.0", "beta": False}], "beta_packages": []}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(packages=_pkg_resp(pkg), server=_server_resp(server))),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_PACKAGE_UPDATE_AVAILABLE) == [
        (M_PACKAGE_UPDATE_AVAILABLE, 0.0, {"package": "Foo"})
    ]


# --- Row 14: server entry BETA excluded (+ non-beta FALSE side) ---


async def test_updates_server_beta_excluded() -> None:
    """A beta server entry for Foo is excluded; a non-beta Baz is included."""
    pkg = {
        "packages": [
            {"id": "Foo", "version": "1.0"},
            {"id": "Baz", "version": "1.0"},
        ],
        "total": 2,
    }
    server = {
        "packages": [
            {"id": "Foo", "version": "2.0", "beta": True},
            {"id": "Baz", "version": "2.0", "beta": False},
        ],
        "beta_packages": [],
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(packages=_pkg_resp(pkg), server=_server_resp(server))),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    by_pkg = {g[2]["package"]: g[1] for g in _gauges_named(writer, M_PACKAGE_UPDATE_AVAILABLE)}
    # Foo's server entry was beta -> not in map -> 0.0.
    assert by_pkg["Foo"] == 0.0
    # Baz's non-beta entry differs -> 1.0.
    assert by_pkg["Baz"] == 1.0


# --- Row 15: server entry missing id or version -> skipped from map ---


async def test_updates_server_entry_missing_fields() -> None:
    """Server entries missing id or version are skipped from the map."""
    pkg = {"packages": [{"id": "Foo", "version": "1.0"}], "total": 1}
    server = {
        "packages": [
            {"version": "2.0", "beta": False},
            {"id": "Foo", "beta": False},
        ],
        "beta_packages": [],
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(packages=_pkg_resp(pkg), server=_server_resp(server))),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    # Neither server entry is usable -> Foo not in map -> update=0.
    assert _gauges_named(writer, M_PACKAGE_UPDATE_AVAILABLE) == [
        (M_PACKAGE_UPDATE_AVAILABLE, 0.0, {"package": "Foo"})
    ]


# --- Row 16: server fetch fails, pkg ok (degraded) ---


async def test_updates_server_fails_pkg_ok() -> None:
    """server fetch fails -> empty map -> all update=0; ok=True (degraded)."""
    pkg = {"packages": [{"id": "Foo", "version": "1.0"}], "total": 1}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                packages=_pkg_resp(pkg),
                server=SynologyError(reason="timeout", message="server timed out"),
            ),
        ),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert result.errors == ["server timed out"]
    assert _gauges_named(writer, M_PACKAGE_UPDATE_AVAILABLE) == [
        (M_PACKAGE_UPDATE_AVAILABLE, 0.0, {"package": "Foo"})
    ]
    assert _gauges_named(writer, M_PACKAGES_WITH_UPDATES_COUNT) == [
        (M_PACKAGES_WITH_UPDATES_COUNT, 0.0, {})
    ]


# --- Row 17: pkg fetch fails, upgrade ok (degraded) ---


async def test_updates_pkg_fails_upgrade_ok() -> None:
    """pkg fetch fails -> no package_info, package_count=0 (default); ok=True."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                packages=SynologyError(reason="timeout", message="pkg timed out"),
            ),
        ),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert result.errors == ["pkg timed out"]
    assert _gauges_named(writer, M_PACKAGE_INFO) == []
    assert _gauges_named(writer, M_PACKAGE_COUNT) == [(M_PACKAGE_COUNT, 0.0, {})]
    assert _gauges_named(writer, M_PACKAGE_UPDATE_AVAILABLE) == []


# --- Row 18: upgrade fetch fails, pkg ok (degraded) ---


async def test_updates_upgrade_fails_pkg_ok() -> None:
    """upgrade fetch fails -> dsm scalars stay at 0-baseline; ok=True."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                upgrade=SynologyError(reason="timeout", message="upgrade timed out"),
            ),
        ),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert result.errors == ["upgrade timed out"]
    assert _gauges_named(writer, M_DSM_UPDATE_AVAILABLE) == [(M_DSM_UPDATE_AVAILABLE, 0.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_IS_SECURITY) == [(M_DSM_UPDATE_IS_SECURITY, 0.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_INFO) == []


# --- Row 19: ALL THREE fetches fail ---


async def test_updates_all_fetches_fail() -> None:
    """All three fail -> ok=False; aggregates at defaults; errors populated."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                upgrade=SynologyError(reason="timeout", message="upgrade timed out"),
                packages=SynologyError(reason="timeout", message="pkg timed out"),
                server=SynologyError(reason="timeout", message="server timed out"),
            ),
        ),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is False
    # Errors recorded in fetch order: upgrade, server, pkg.
    assert result.errors == ["upgrade timed out", "server timed out", "pkg timed out"]
    assert _gauges_named(writer, M_DSM_UPDATE_AVAILABLE) == [(M_DSM_UPDATE_AVAILABLE, 0.0, {})]
    assert _gauges_named(writer, M_DSM_UPDATE_IS_SECURITY) == [(M_DSM_UPDATE_IS_SECURITY, 0.0, {})]
    assert _gauges_named(writer, M_PACKAGE_COUNT) == [(M_PACKAGE_COUNT, 0.0, {})]
    assert _gauges_named(writer, M_PACKAGES_WITH_UPDATES_COUNT) == [
        (M_PACKAGES_WITH_UPDATES_COUNT, 0.0, {})
    ]
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT


# --- Row 20: unconfigured client ---


async def test_updates_unconfigured_client() -> None:
    """synology=None -> unconfigured."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, None))

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


# --- Row 21: non-dict payloads ---


async def test_updates_non_dict_payloads() -> None:
    """upgrade/pkg/server payloads non-dict -> defaults hold; ok=True."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                upgrade=_upgrade_resp(None),
                packages=_pkg_resp("nope"),
                server=_server_resp(123),
            ),
        ),
    )

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_DSM_UPDATE_AVAILABLE) == [(M_DSM_UPDATE_AVAILABLE, 0.0, {})]
    assert _gauges_named(writer, M_PACKAGE_COUNT) == [(M_PACKAGE_COUNT, 0.0, {})]
    assert _gauges_named(writer, M_PACKAGE_INFO) == []


# --- Row 22: metrics accounting ---


async def test_updates_metrics_emitted_accounting() -> None:
    """api_took x3 (all fetches) + drop x7 (family count); emitted == len(gauges)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))

    result = await SynologyUpdatesCollector().run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, _API_TOOK)) == _EXPECTED_API_TOOK_COUNT
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT
    assert result.metrics_emitted == len(writer.gauges)
