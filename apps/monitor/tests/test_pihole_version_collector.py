"""Unit tests for PiholeVersionCollector (STAGE-006-011).

Covers 100% branch coverage across:
- happy path: all 4 components (core/web/ftl as object-shape, docker as bare-string),
  including update (web, docker) and no-update (core, ftl)
- ctx.pihole is None → ok=False, 0 emits
- info_version() returns PiholeError → ok=False, 0 emits
- payload not a dict → ok=False, errors=["unexpected payload shape"], metrics_emitted==1
- "version" key missing/not-a-dict → ok=False, error contains "version not a dict",
  metrics_emitted==1
- component with object-shape local but missing remote → version_info emitted,
  update_available NOT emitted
- component with local present but remote is empty string → remote_str None →
  update_available NOT emitted; version_info still emitted
- component with local entirely missing → neither series emitted
- component where comp_obj is NOT a dict → _extract_versions returns (None,None) →
  nothing emitted for it; other valid components unaffected
- component object-shape where local dict has no "version" sub-key → local_str None →
  nothing emitted
- metric-name constants literal match (contract test)
- registration via register_all + PluginLoader
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import structlog

from homelab_monitor.kernel.pihole.client import PiholeResponse
from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole import register_all
from homelab_monitor.plugins.collectors.integrations.pihole.version import (
    M_API_TOOK,
    M_UPDATE_AVAILABLE,
    M_VERSION_INFO,
    PiholeVersionCollector,
)

# ---------------------------------------------------------------------------
# Fake pihole clients
# ---------------------------------------------------------------------------


class _FakePiholeBase:
    """Base fake PiholeClient: every method returns a stub PiholeError."""

    async def info_version(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def info_ftl(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def info_database(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def info_messages(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def info_system(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_summary(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_upstreams(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_query_types(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_top_clients(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_top_domains(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def stats_recent_blocked(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def dns_blocking(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def lists(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def network_devices(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def queries(self, params: dict[str, str]) -> PiholeResponse | PiholeError:
        return PiholeError(reason="bad_response", message="stub")

    async def aclose(self) -> None:
        pass


class _FakeVersionOk(_FakePiholeBase):
    """info_version returns a configurable PiholeResponse."""

    def __init__(self, payload: object, took: float = 0.000042) -> None:
        self._payload = payload
        self._took = took

    async def info_version(self) -> PiholeResponse | PiholeError:
        return PiholeResponse(
            payload=self._payload,
            took_seconds=self._took,
            endpoint="info/version",
        )


class _FakeVersionError(_FakePiholeBase):
    """info_version returns a PiholeError."""

    def __init__(self, message: str = "timeout") -> None:
        self._message = message

    async def info_version(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="timeout", message=self._message)


# ---------------------------------------------------------------------------
# Context / assertion helpers (VERBATIM from blocking test conventions)
# ---------------------------------------------------------------------------


def _ctx(writer: InMemoryMetricsWriter, pihole: object | None) -> CollectorContext:
    """Build a CollectorContext wired to the given writer and pihole client."""
    return CollectorContext(
        config=CollectorConfig(
            name="pihole_version",
            interval_seconds=3600,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_version"),
        pihole=pihole,  # type: ignore[arg-type]
    )


def _gauge_value(
    writer: InMemoryMetricsWriter,
    name: str,
    labels: dict[str, str] | None = None,
) -> float | None:
    labels = labels or {}
    for e in writer.recorded:  # pyright: ignore[reportPrivateUsage]
        if e.kind == "gauge" and e.name == name and e.labels == labels:
            return e.value
    return None


def _all_metric_names(writer: InMemoryMetricsWriter) -> set[str]:
    return {e.name for e in writer.recorded}  # pyright: ignore[reportPrivateUsage]


def _labels_for(
    writer: InMemoryMetricsWriter,
    name: str,
) -> list[dict[str, str]]:
    """Return all label-dicts recorded for the given metric name."""
    return [
        e.labels
        for e in writer.recorded  # pyright: ignore[reportPrivateUsage]
        if e.kind == "gauge" and e.name == name
    ]


# ---------------------------------------------------------------------------
# Realistic full payload (all 4 components, two shapes)
# ---------------------------------------------------------------------------

_FULL_PAYLOAD: dict[str, object] = {
    "version": {
        # object-shape, no update
        "core": {
            "local": {"version": "v6.4.2", "branch": "master", "hash": "abc1234"},
            "remote": {"version": "v6.4.2"},
        },
        # object-shape, UPDATE AVAILABLE
        "web": {
            "local": {"version": "v6.5", "branch": "master", "hash": "def5678"},
            "remote": {"version": "v6.5.1"},
        },
        # object-shape, no update
        "ftl": {
            "local": {"version": "v6.6.2", "branch": "master", "hash": "ghi9012"},
            "remote": {"version": "v6.6.2"},
        },
        # bare-string shape (docker), UPDATE AVAILABLE
        "docker": {
            "local": "2026.05.0",
            "remote": "2026.06.0",
        },
    },
    "took": 0.000042,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_all_components() -> None:
    """Full payload: 4 components, two shapes; assert per-component metrics.

    Expected emissions:
      api_took               = 1
      version_info x4        = 4  (one per component, keyed on local version)
      update_available x4    = 4  (one per component, both local+remote present)
      total                  = 9
    """
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    ctx = _ctx(writer, _FakeVersionOk(_FULL_PAYLOAD))

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == []
    assert result.metrics_emitted == 9  # noqa: PLR2004

    # api_took
    api_took = _gauge_value(writer, M_API_TOOK, {"endpoint": "info/version"})
    assert api_took == pytest.approx(0.000042)  # pyright: ignore[reportUnknownMemberType]

    # update_available: web and docker have updates; core and ftl do not
    upd_web = _gauge_value(writer, M_UPDATE_AVAILABLE, {"component": "web"})
    assert upd_web == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]

    upd_core = _gauge_value(writer, M_UPDATE_AVAILABLE, {"component": "core"})
    assert upd_core == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]

    upd_docker = _gauge_value(writer, M_UPDATE_AVAILABLE, {"component": "docker"})
    assert upd_docker == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]

    upd_ftl = _gauge_value(writer, M_UPDATE_AVAILABLE, {"component": "ftl"})
    assert upd_ftl == pytest.approx(0.0)  # pyright: ignore[reportUnknownMemberType]

    # version_info: object-shape (core/web/ftl) and bare-string shape (docker)
    vi_web = _gauge_value(writer, M_VERSION_INFO, {"component": "web", "version": "v6.5"})
    assert vi_web == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]

    vi_docker = _gauge_value(
        writer, M_VERSION_INFO, {"component": "docker", "version": "2026.05.0"}
    )
    assert vi_docker == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]

    vi_core = _gauge_value(writer, M_VERSION_INFO, {"component": "core", "version": "v6.4.2"})
    assert vi_core == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_ctx_pihole_none() -> None:
    """ctx.pihole is None → ok=False, errors="pihole client not configured", 0 emits."""
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    result = await collector.run(_ctx(writer, None))
    assert result.ok is False
    assert result.errors == ["pihole client not configured"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_info_version_returns_pihole_error() -> None:
    """info_version() returns PiholeError → ok=False, errors=[message], 0 emits."""
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    ctx = _ctx(writer, _FakeVersionError("GET /api/info/version: timed out"))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["GET /api/info/version: timed out"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_payload_not_a_dict() -> None:
    """payload is a list → ok=False, errors=["unexpected payload shape"],
    metrics_emitted==1 (api_took counted)."""
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    ctx = _ctx(writer, _FakeVersionOk(["not", "a", "dict"]))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["unexpected payload shape"]
    assert result.metrics_emitted == 1  # api_took already emitted


@pytest.mark.asyncio
async def test_version_key_missing() -> None:
    """payload has no "version" key → version_obj is None → not a dict guard fires.
    ok=False, error contains "version not a dict", metrics_emitted==1."""
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    ctx = _ctx(writer, _FakeVersionOk({"took": 0.000042}))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["unexpected payload shape (version not a dict)"]
    assert result.metrics_emitted == 1  # api_took already emitted
    assert M_VERSION_INFO not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_version_key_not_a_dict() -> None:
    """payload["version"] is a string (not a dict) → guard fires.
    ok=False, error contains "version not a dict", metrics_emitted==1."""
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    ctx = _ctx(writer, _FakeVersionOk({"version": "x", "took": 0.000042}))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["unexpected payload shape (version not a dict)"]
    assert result.metrics_emitted == 1


@pytest.mark.asyncio
async def test_component_object_local_missing_remote() -> None:
    """Component with local present but remote key absent.
    version_info emitted (local present); update_available NOT emitted (remote missing)."""
    payload: dict[str, object] = {
        "version": {
            "core": {
                "local": {"version": "v6.4.2"},
                # "remote" key intentionally absent
            },
        },
        "took": 0.000042,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    ctx = _ctx(writer, _FakeVersionOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    # api_took + 1 version_info = 2; no update_available
    assert result.metrics_emitted == 2  # noqa: PLR2004

    vi = _gauge_value(writer, M_VERSION_INFO, {"component": "core", "version": "v6.4.2"})
    assert vi == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]

    assert M_UPDATE_AVAILABLE not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_component_remote_empty_string() -> None:
    """Component with local present but remote is empty string "" → remote_str None.
    update_available NOT emitted; version_info still emitted."""
    payload: dict[str, object] = {
        "version": {
            "core": {
                "local": {"version": "v6.4.2"},
                "remote": {"version": ""},  # empty → None via _version_str
            },
        },
        "took": 0.000042,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    ctx = _ctx(writer, _FakeVersionOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    vi = _gauge_value(writer, M_VERSION_INFO, {"component": "core", "version": "v6.4.2"})
    assert vi == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    assert M_UPDATE_AVAILABLE not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_component_remote_whitespace_string() -> None:
    """Component where remote bare-string is whitespace-only "  " → remote_str None.
    update_available NOT emitted; version_info still emitted (bare-string local)."""
    payload: dict[str, object] = {
        "version": {
            "docker": {
                "local": "2026.05.0",
                "remote": "  ",  # whitespace-only → None
            },
        },
        "took": 0.000042,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    ctx = _ctx(writer, _FakeVersionOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    vi = _gauge_value(writer, M_VERSION_INFO, {"component": "docker", "version": "2026.05.0"})
    assert vi == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    assert M_UPDATE_AVAILABLE not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_component_local_missing_entirely() -> None:
    """Component where "local" key is absent → local_str None.
    Neither version_info NOR update_available emitted for it."""
    payload: dict[str, object] = {
        "version": {
            "core": {
                # "local" key entirely absent
                "remote": {"version": "v6.4.2"},
            },
        },
        "took": 0.000042,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    ctx = _ctx(writer, _FakeVersionOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    # Only api_took emitted; no version_info or update_available
    assert result.metrics_emitted == 1
    assert M_VERSION_INFO not in _all_metric_names(writer)
    assert M_UPDATE_AVAILABLE not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_component_comp_obj_not_a_dict() -> None:
    """comp_obj is not a dict (e.g. an int) → _extract_versions returns (None,None).
    Nothing emitted for that component; other valid components still emit."""
    payload: dict[str, object] = {
        "version": {
            "bogus": 123,  # not a dict → _extract_versions early return
            "core": {
                "local": {"version": "v6.4.2"},
                "remote": {"version": "v6.4.2"},
            },
        },
        "took": 0.000042,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    ctx = _ctx(writer, _FakeVersionOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    # api_took + 1 version_info(core) + 1 update_available(core) = 3; nothing for "bogus"
    assert result.metrics_emitted == 3  # noqa: PLR2004

    labels_vi = _labels_for(writer, M_VERSION_INFO)
    component_names_vi = [lb.get("component") for lb in labels_vi]
    assert "bogus" not in component_names_vi
    assert "core" in component_names_vi

    labels_ua = _labels_for(writer, M_UPDATE_AVAILABLE)
    component_names_ua = [lb.get("component") for lb in labels_ua]
    assert "bogus" not in component_names_ua


@pytest.mark.asyncio
async def test_component_object_local_dict_no_version_subkey() -> None:
    """Object-shape local is a dict but has no "version" sub-key → local_str None.
    Nothing emitted for that component."""
    payload: dict[str, object] = {
        "version": {
            "core": {
                "local": {"branch": "master"},  # no "version" key
                "remote": {"version": "v6.4.2"},
            },
        },
        "took": 0.000042,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeVersionCollector()
    ctx = _ctx(writer, _FakeVersionOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    # Only api_took; local_str is None so nothing emitted for core
    assert result.metrics_emitted == 1
    assert M_VERSION_INFO not in _all_metric_names(writer)
    assert M_UPDATE_AVAILABLE not in _all_metric_names(writer)


def test_metric_name_constants_match_contract() -> None:
    """Public M_* constants must equal the literal contract strings."""
    assert M_UPDATE_AVAILABLE == "homelab_pihole_update_available"
    assert M_VERSION_INFO == "homelab_pihole_version_info"
    assert M_API_TOOK == "homelab_pihole_api_took_seconds"


@pytest.mark.asyncio
async def test_registration() -> None:
    """PiholeVersionCollector is registered via register_all + PluginLoader."""
    loader = MagicMock(spec=PluginLoader)
    register_all(loader)

    registered_classes = [call.args[0] for call in loader.register.call_args_list]
    assert PiholeVersionCollector in registered_classes
