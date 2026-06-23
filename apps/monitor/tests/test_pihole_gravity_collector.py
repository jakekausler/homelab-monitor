"""Unit tests for PiholeGravityCollector (STAGE-006-007).

100% branch coverage across the two-endpoint resilience matrix, per-adlist
narrowing, status-code mapping, and the derived gravity-update age math.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import structlog

from homelab_monitor.kernel.pihole.client import PiholeResponse
from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole.gravity import (
    M_ADLIST_DOMAINS,
    M_ADLIST_ENABLED,
    M_ADLIST_STATUS,
    M_API_TOOK,
    M_GRAVITY_AGE,
    M_GRAVITY_DOMAINS,
    MAX_ADLISTS,
    PiholeGravityCollector,
)

# ---------------------------------------------------------------------------
# Fake Pi-hole client (info_ftl + lists configurable independently)
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


# Sentinel meaning "make this endpoint raise a PiholeError instead of OK".
_ERR = object()


class _FakeGravity(_FakePiholeBase):
    """info_ftl + lists each independently return PiholeResponse or PiholeError.

    Pass a payload object to return OK, or the ``_ERR`` sentinel (with an optional
    message via the *_err_msg args) to return a PiholeError. None means OK with a
    None payload (the not-a-dict branch).
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        ftl_payload: object = _ERR,
        ftl_err_msg: str = "ftl failed",
        ftl_took: float = 0.0002,
        lists_payload: object = _ERR,
        lists_err_msg: str = "lists failed",
        lists_took: float = 0.0146,
    ) -> None:
        self._ftl_payload = ftl_payload
        self._ftl_err_msg = ftl_err_msg
        self._ftl_took = ftl_took
        self._lists_payload = lists_payload
        self._lists_err_msg = lists_err_msg
        self._lists_took = lists_took

    async def info_ftl(self) -> PiholeResponse | PiholeError:
        if self._ftl_payload is _ERR:
            return PiholeError(reason="timeout", message=self._ftl_err_msg)
        return PiholeResponse(
            payload=self._ftl_payload, took_seconds=self._ftl_took, endpoint="info/ftl"
        )

    async def lists(self) -> PiholeResponse | PiholeError:
        if self._lists_payload is _ERR:
            return PiholeError(reason="timeout", message=self._lists_err_msg)
        return PiholeResponse(
            payload=self._lists_payload, took_seconds=self._lists_took, endpoint="lists"
        )


# ---------------------------------------------------------------------------
# ctx builder (mirrors test_pihole_upstreams_collector.py exactly)
# ---------------------------------------------------------------------------


def _ctx(writer: InMemoryMetricsWriter, pihole: object | None) -> CollectorContext:
    """Build a CollectorContext wired to the given writer and pihole client."""
    return CollectorContext(
        config=CollectorConfig(
            name="pihole_gravity",
            interval_seconds=30,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_gravity"),
        pihole=pihole,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gauge_value(
    writer: InMemoryMetricsWriter, name: str, labels: dict[str, str] | None = None
) -> float | None:
    labels = labels or {}
    for e in writer.recorded:  # pyright: ignore[reportPrivateUsage]
        if e.kind == "gauge" and e.name == name and e.labels == labels:
            return e.value
    return None


def _all_metric_names(writer: InMemoryMetricsWriter) -> set[str]:
    return {e.name for e in writer.recorded}  # pyright: ignore[reportPrivateUsage]


def _count(writer: InMemoryMetricsWriter, name: str) -> int:
    return sum(1 for e in writer.recorded if e.name == name)  # pyright: ignore[reportPrivateUsage]


def _all_labels(writer: InMemoryMetricsWriter) -> list[dict[str, str]]:
    return [e.labels for e in writer.recorded]  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Live-shape fixtures (Pi-hole v6.6.2)
# ---------------------------------------------------------------------------

_FTL_PAYLOAD: dict[str, object] = {
    "ftl": {
        "database": {
            "gravity": 5863264,
            "groups": 3,
            "lists": 5,
            "clients": 12,
            "domains": {"allowed": 4, "denied": 1},
            "regex": {"allowed": 0, "denied": 2},
        }
    },
    "took": 0.0002,
}


def _lists_payload() -> dict[str, object]:
    """Build a 5-adlist payload: 3 status=1 (ok) + 2 status=3 (parse_failed).

    date_updated is set to ~120s ago so the derived age asserts ~120 (range).
    """
    recent = int((datetime.now(UTC) - timedelta(seconds=120)).timestamp())
    return {
        "lists": [
            {
                "id": 1,
                "address": "https://a.example/list.txt",
                "enabled": True,
                "status": 1,
                "type": "block",
                "number": 123456,
                "invalid_domains": 0,
                "abp_entries": 0,
                "date_added": recent,
                "date_modified": recent,
                "date_updated": recent,
                "comment": "primary",
                "groups": [0],
            },
            {
                "id": 2,
                "address": "https://b.example/list.txt",
                "enabled": True,
                "status": 1,
                "type": "block",
                "number": 654321,
                "date_updated": recent,
                "comment": "secondary",
                "groups": [0],
            },
            {
                "id": 3,
                "address": "https://c.example/list.txt",
                "enabled": True,
                "status": 1,
                "type": "block",
                "number": 1000,
                "date_updated": recent,
                "comment": "",
                "groups": [0],
            },
            {
                "id": 4,
                "address": "https://d.example/fail.txt",
                "enabled": True,
                "status": 3,
                "type": "block",
                "number": 0,
                "date_updated": recent,
                "comment": "broken-1",
                "groups": [0],
            },
            {
                "id": 5,
                "address": "https://e.example/fail.txt",
                "enabled": False,
                "status": 3,
                "type": "block",
                "number": 0,
                "date_updated": recent,
                "comment": "broken-2",
                "groups": [0],
            },
        ],
        "took": 0.0146,
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_both_endpoints() -> None:
    """Both endpoints OK -> ok=True, all metrics emitted."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload=_FTL_PAYLOAD, lists_payload=_lists_payload())
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == []
    assert _gauge_value(writer, M_GRAVITY_DOMAINS, {}) == 5863264.0  # noqa: PLR2004

    # api_took emitted twice
    took_ftl = _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"})
    assert took_ftl is not None
    took_lists = _gauge_value(writer, M_API_TOOK, {"endpoint": "lists"})
    assert took_lists is not None

    # per-adlist domains for id 1
    assert (
        _gauge_value(
            writer, M_ADLIST_DOMAINS, {"list": "1", "address": "https://a.example/list.txt"}
        )
        == 123456.0  # noqa: PLR2004
    )

    # enabled: id 1 -> 1.0; id 5 -> 0.0
    assert (
        _gauge_value(
            writer, M_ADLIST_ENABLED, {"list": "1", "address": "https://a.example/list.txt"}
        )
        == 1.0
    )
    assert (
        _gauge_value(
            writer, M_ADLIST_ENABLED, {"list": "5", "address": "https://e.example/fail.txt"}
        )
        == 0.0
    )

    # status: id 1 -> "ok"; id 4 -> "parse_failed"
    assert (
        _gauge_value(
            writer,
            M_ADLIST_STATUS,
            {"list": "1", "address": "https://a.example/list.txt", "status": "ok"},
        )
        == 1.0
    )
    assert (
        _gauge_value(
            writer,
            M_ADLIST_STATUS,
            {"list": "4", "address": "https://d.example/fail.txt", "status": "parse_failed"},
        )
        == 1.0
    )

    # gravity age
    age = _gauge_value(writer, M_GRAVITY_AGE, {})
    assert age is not None
    assert 110.0 <= age <= 130.0  # noqa: PLR2004

    # metrics_emitted = 2 api_took + 1 gravity + 5 domains + 5 enabled + 5 status + 1 age
    assert result.metrics_emitted == 19  # noqa: PLR2004


@pytest.mark.asyncio
async def test_ctx_pihole_none() -> None:
    """ctx.pihole is None -> ok=False, error message, 0 metrics."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    ctx = _ctx(writer, None)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["pihole client not configured"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_both_endpoints_error() -> None:
    """Both endpoints error -> ok=False, both errors, 0 metrics."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload=_ERR, ftl_err_msg="ftl boom", lists_payload=_ERR, lists_err_msg="lists boom"
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["ftl boom", "lists boom"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_ftl_error_lists_ok() -> None:
    """ftl errors, lists OK -> ok=True (lists_ok), ftl error appended."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload=_ERR, ftl_err_msg="ftl boom", lists_payload=_lists_payload())
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["ftl boom"]
    assert M_GRAVITY_DOMAINS not in _all_metric_names(writer)
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "lists"}) is not None
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"}) is None
    assert M_ADLIST_DOMAINS in _all_metric_names(writer)
    assert M_GRAVITY_AGE in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_lists_error_ftl_ok() -> None:
    """lists errors, ftl OK -> ok=True (ftl_ok), lists error appended."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload=_FTL_PAYLOAD, lists_payload=_ERR, lists_err_msg="lists boom")
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["lists boom"]
    assert _gauge_value(writer, M_GRAVITY_DOMAINS, {}) == 5863264.0  # noqa: PLR2004
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"}) is not None
    assert M_ADLIST_DOMAINS not in _all_metric_names(writer)
    assert M_GRAVITY_AGE not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_ftl_payload_not_a_dict() -> None:
    """ftl payload is not a dict -> ok=True, api_took emitted, gravity skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload=["not", "a", "dict"], lists_payload=_ERR)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"}) is not None
    assert M_GRAVITY_DOMAINS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_ftl_missing_ftl_key() -> None:
    """ftl payload missing 'ftl' key -> gravity skipped, api_took present."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload={"took": 0.0002}, lists_payload=_ERR)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"}) is not None
    assert M_GRAVITY_DOMAINS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_ftl_ftl_not_a_dict() -> None:
    """ftl payload['ftl'] is not a dict -> gravity skipped, api_took present."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload={"ftl": "nope", "took": 0.0002}, lists_payload=_ERR)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"}) is not None
    assert M_GRAVITY_DOMAINS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_ftl_missing_database_key() -> None:
    """ftl payload['ftl'] missing 'database' -> gravity skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload={"ftl": {"runtime": 1}, "took": 0.0002}, lists_payload=_ERR)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert M_GRAVITY_DOMAINS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_ftl_database_not_a_dict() -> None:
    """ftl payload['ftl']['database'] is not a dict -> gravity skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload={"ftl": {"database": 5}, "took": 0.0002}, lists_payload=_ERR)
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert M_GRAVITY_DOMAINS not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_ftl_gravity_non_numeric() -> None:
    """ftl['ftl']['database']['gravity'] is non-numeric -> gravity skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload={"ftl": {"database": {"gravity": "lots"}}, "took": 0.0002}, lists_payload=_ERR
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert M_GRAVITY_DOMAINS not in _all_metric_names(writer)
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "info/ftl"}) is not None
    assert result.ok is True


@pytest.mark.asyncio
async def test_lists_payload_not_a_dict() -> None:
    """lists payload is not a dict -> lists_ok=True, adlist/age skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload=_ERR, lists_payload=["x"])
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "lists"}) is not None
    assert M_ADLIST_DOMAINS not in _all_metric_names(writer)
    assert M_GRAVITY_AGE not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_lists_key_missing() -> None:
    """lists payload missing 'lists' key -> adlist/age skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload=_ERR, lists_payload={"took": 0.0146})
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "lists"}) is not None
    assert M_ADLIST_DOMAINS not in _all_metric_names(writer)
    assert M_GRAVITY_AGE not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_lists_key_not_a_list() -> None:
    """lists payload['lists'] is not a list -> adlist/age skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload=_ERR, lists_payload={"lists": {"id": 1}, "took": 0.0146})
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "lists"}) is not None
    assert M_ADLIST_DOMAINS not in _all_metric_names(writer)
    assert M_GRAVITY_AGE not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_lists_empty_array() -> None:
    """lists is empty -> adlist metrics skipped, age SKIPPED (no date_updated)."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload=_ERR, lists_payload={"lists": [], "took": 0.0146})
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_API_TOOK, {"endpoint": "lists"}) is not None
    assert M_ADLIST_DOMAINS not in _all_metric_names(writer)
    assert M_GRAVITY_AGE not in _all_metric_names(writer)
    assert result.metrics_emitted == 1  # lists api_took only


@pytest.mark.asyncio
async def test_adlist_entry_not_dict_skipped() -> None:
    """Non-dict entries are skipped; valid dicts are processed."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    recent = int((datetime.now(UTC) - timedelta(seconds=120)).timestamp())
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={
            "lists": [
                "nope",
                {"id": 9, "address": "x", "number": 5, "date_updated": recent},
            ],
            "took": 0.0146,
        },
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_ADLIST_DOMAINS, {"list": "9", "address": "x"}) == 5.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_adlist_id_missing_skipped() -> None:
    """Entry with missing id is skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={"lists": [{"address": "x", "number": 5}], "took": 0.0146},
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _count(writer, M_ADLIST_DOMAINS) == 0


@pytest.mark.asyncio
async def test_adlist_id_non_int_skipped() -> None:
    """Entry with non-int id is skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={"lists": [{"id": "abc", "address": "x", "number": 5}], "took": 0.0146},
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _count(writer, M_ADLIST_DOMAINS) == 0


@pytest.mark.asyncio
async def test_adlist_id_bool_skipped() -> None:
    """Entry with bool id (bool is int subclass) is skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={"lists": [{"id": True, "address": "x", "number": 5}], "took": 0.0146},
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _count(writer, M_ADLIST_DOMAINS) == 0


@pytest.mark.asyncio
async def test_adlist_address_missing_defaults_empty() -> None:
    """Entry with missing address gets empty string."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    recent = int((datetime.now(UTC) - timedelta(seconds=120)).timestamp())
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={"lists": [{"id": 7, "number": 9, "date_updated": recent}], "took": 0.0146},
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_ADLIST_DOMAINS, {"list": "7", "address": ""}) == 9.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_adlist_address_non_str_defaults_empty() -> None:
    """Entry with non-str address gets empty string."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={"lists": [{"id": 7, "address": 123, "number": 9}], "took": 0.0146},
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_ADLIST_DOMAINS, {"list": "7", "address": ""}) == 9.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_adlist_number_non_numeric_skipped() -> None:
    """Entry with non-numeric 'number' skips domains but enabled/status still emit."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={
            "lists": [{"id": 7, "address": "x", "number": "lots", "enabled": True, "status": 1}],
            "took": 0.0146,
        },
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_ADLIST_DOMAINS, {"list": "7", "address": "x"}) is None
    assert _gauge_value(writer, M_ADLIST_ENABLED, {"list": "7", "address": "x"}) == 1.0


@pytest.mark.asyncio
async def test_adlist_enabled_true_false_nonbool() -> None:
    """enabled: true -> 1.0, false -> 0.0, non-bool -> skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={
            "lists": [
                {"id": 1, "address": "a", "enabled": True},
                {"id": 2, "address": "b", "enabled": False},
                {"id": 3, "address": "c", "enabled": "yes"},
            ],
            "took": 0.0146,
        },
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauge_value(writer, M_ADLIST_ENABLED, {"list": "1", "address": "a"}) == 1.0
    assert _gauge_value(writer, M_ADLIST_ENABLED, {"list": "2", "address": "b"}) == 0.0
    assert _gauge_value(writer, M_ADLIST_ENABLED, {"list": "3", "address": "c"}) is None


@pytest.mark.asyncio
async def test_adlist_status_mapping() -> None:
    """status codes 0/1/2/3 map correctly; unknown codes -> unknown_<code>; non-int skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={
            "lists": [
                {"id": 1, "address": "a", "status": 0},
                {"id": 2, "address": "b", "status": 1},
                {"id": 3, "address": "c", "status": 2},
                {"id": 4, "address": "d", "status": 3},
                {"id": 5, "address": "e", "status": 99},
                {"id": 6, "address": "f", "status": "bad"},
            ],
            "took": 0.0146,
        },
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert (
        _gauge_value(writer, M_ADLIST_STATUS, {"list": "1", "address": "a", "status": "not_run"})
        == 1.0
    )
    assert (
        _gauge_value(writer, M_ADLIST_STATUS, {"list": "2", "address": "b", "status": "ok"}) == 1.0
    )
    assert (
        _gauge_value(
            writer, M_ADLIST_STATUS, {"list": "3", "address": "c", "status": "download_failed"}
        )
        == 1.0
    )
    assert (
        _gauge_value(
            writer, M_ADLIST_STATUS, {"list": "4", "address": "d", "status": "parse_failed"}
        )
        == 1.0
    )
    assert (
        _gauge_value(writer, M_ADLIST_STATUS, {"list": "5", "address": "e", "status": "unknown_99"})
        == 1.0
    )
    # No status gauge for list "6" (id=6, address="f")
    assert all(
        "6" not in lbls.get("list", "")
        for lbls in _all_labels(writer)
        if M_ADLIST_STATUS in [e.name for e in writer.recorded]
    )  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_adlist_status_bool_skipped() -> None:
    """status=True (bool is int subclass) is skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={"lists": [{"id": 1, "address": "a", "status": True}], "took": 0.0146},
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    # No status gauge for list "1"
    assert all(
        "1" not in lbls.get("list", "")
        for lbls in _all_labels(writer)
        if M_ADLIST_STATUS in [e.name for e in writer.recorded]
    )  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_age_all_date_updated_missing_skips() -> None:
    """No valid date_updated anywhere -> age gauge SKIPPED."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={"lists": [{"id": 1, "address": "a", "number": 5}], "took": 0.0146},
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert M_ADLIST_DOMAINS in _all_metric_names(writer)
    assert M_GRAVITY_AGE not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_age_date_updated_non_numeric_excluded() -> None:
    """Non-numeric date_updated is not collected -> age skipped."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={
            "lists": [{"id": 1, "address": "a", "date_updated": "soon"}],
            "took": 0.0146,
        },
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert M_GRAVITY_AGE not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_age_future_date_clamped_to_zero() -> None:
    """Future date_updated -> age clamped to 0.0."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    future = int((datetime.now(UTC) + timedelta(seconds=300)).timestamp())
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={
            "lists": [{"id": 1, "address": "a", "date_updated": future}],
            "took": 0.0146,
        },
    )
    ctx = _ctx(writer, fake)

    await collector.run(ctx)

    age = _gauge_value(writer, M_GRAVITY_AGE, {})
    assert age == 0.0


@pytest.mark.asyncio
async def test_age_uses_max_date_updated() -> None:
    """Two entries with different date_updated -> age uses max (most recent)."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    old = int((datetime.now(UTC) - timedelta(seconds=600)).timestamp())
    recent = int((datetime.now(UTC) - timedelta(seconds=60)).timestamp())
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={
            "lists": [
                {"id": 1, "address": "a", "date_updated": old},
                {"id": 2, "address": "b", "date_updated": recent},
            ],
            "took": 0.0146,
        },
    )
    ctx = _ctx(writer, fake)

    await collector.run(ctx)

    age = _gauge_value(writer, M_GRAVITY_AGE, {})
    assert age is not None
    assert 50.0 <= age <= 70.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_max_adlists_cap() -> None:
    """Entries beyond MAX_ADLISTS are ignored."""
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    recent = int((datetime.now(UTC) - timedelta(seconds=120)).timestamp())
    entries = [
        {"id": i, "address": f"a{i}", "number": i, "date_updated": recent}
        for i in range(MAX_ADLISTS + 5)
    ]
    fake = _FakeGravity(
        ftl_payload=_ERR,
        lists_payload={"lists": entries, "took": 0.0146},
    )
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True
    assert _count(writer, M_ADLIST_DOMAINS) == MAX_ADLISTS
    assert (
        _gauge_value(
            writer,
            M_ADLIST_DOMAINS,
            {"list": str(MAX_ADLISTS + 4), "address": f"a{MAX_ADLISTS + 4}"},
        )
        is None
    )


def test_metric_name_constants_match_contract() -> None:
    """Metric name constants match the contract."""
    assert M_API_TOOK == "homelab_pihole_api_took_seconds"
    assert M_GRAVITY_DOMAINS == "homelab_pihole_gravity_domains"
    assert M_GRAVITY_AGE == "homelab_pihole_gravity_last_update_age_seconds"
    assert M_ADLIST_DOMAINS == "homelab_pihole_adlist_domains"
    assert M_ADLIST_ENABLED == "homelab_pihole_adlist_enabled"
    assert M_ADLIST_STATUS == "homelab_pihole_adlist_status"


@pytest.mark.asyncio
async def test_negative_assertions_no_comment_label_or_summed_number() -> None:
    """Negative assertions: no comment label, gravity != sum of adlist numbers.

    Also verify no unwanted fields are emitted.
    """
    writer = InMemoryMetricsWriter()
    collector = PiholeGravityCollector()
    fake = _FakeGravity(ftl_payload=_FTL_PAYLOAD, lists_payload=_lists_payload())
    ctx = _ctx(writer, fake)

    result = await collector.run(ctx)

    assert result.ok is True

    # No emitted metric carries a 'comment' label
    assert all("comment" not in lbls for lbls in _all_labels(writer))

    # gravity_domains is the FTL value, NOT sum of adlist 'number'
    assert _gauge_value(writer, M_GRAVITY_DOMAINS, {}) == 5863264.0  # noqa: PLR2004
    # 123456 + 654321 + 1000 + 0 + 0 = 778777 != 5863264
    # Don't assert the constant comparison; just verify the actual value differs
    adlist_sum = 123456 + 654321 + 1000
    assert _gauge_value(writer, M_GRAVITY_DOMAINS, {}) != adlist_sum

    # No metric name contains unwanted tokens
    metric_names = _all_metric_names(writer)
    unwanted_tokens = {
        "groups",
        "regex",
        "date_added",
        "date_modified",
        "invalid_domains",
        "abp_entries",
    }
    for token in unwanted_tokens:
        assert not any(token in n for n in metric_names)
