"""Unit tests for PiholeFtlMessagesCollector (STAGE-006-010).

Covers 100% branch coverage across:
- happy path: 3 messages (2 LIST, 1 LOAD) -> count==3, per-type emitted
- empty list -> count==0, no per-type series, ok=True
- ctx.pihole is None -> ok=False, 0 emits
- info_messages() returns PiholeError -> ok=False, 0 emits
- payload not a dict (e.g. []) -> ok=False, errors["unexpected payload shape"],
  metrics_emitted==1 (api_took counted)
- "messages" key missing or not a list -> ok=False, errors mention "not a list",
  metrics_emitted==1
- message entry is not a dict -> skipped; count includes ALL list entries (raw len),
  per-type only groups well-formed entries
- message entry has non-string/missing type -> grouped under type="unknown"
- metric-name constants literal match
- registration via register_all + PluginLoader
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import structlog

from homelab_monitor.kernel.pihole.client import PiholeResponse
from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
)
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.plugins.collectors.integrations.pihole import register_all
from homelab_monitor.plugins.collectors.integrations.pihole.ftl_messages import (
    M_API_TOOK,
    M_MESSAGES_BY_TYPE,
    M_MESSAGES_COUNT,
    PiholeFtlMessagesCollector,
)

# ---------------------------------------------------------------------------
# Fake pihole client
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


class _FakeFtlMessagesOk(_FakePiholeBase):
    """info_messages returns a configurable PiholeResponse."""

    def __init__(self, payload: object, took: float = 0.000042) -> None:
        self._payload = payload
        self._took = took

    async def info_messages(self) -> PiholeResponse | PiholeError:
        return PiholeResponse(
            payload=self._payload, took_seconds=self._took, endpoint="info/messages"
        )


class _FakeFtlMessagesError(_FakePiholeBase):
    """info_messages returns a PiholeError."""

    def __init__(self, message: str = "timeout") -> None:
        self._message = message

    async def info_messages(self) -> PiholeResponse | PiholeError:
        return PiholeError(reason="timeout", message=self._message)


# ---------------------------------------------------------------------------
# Context / assertion helpers (verbatim pattern from blocking test)
# ---------------------------------------------------------------------------


def _ctx(writer: InMemoryMetricsWriter, pihole: object | None) -> CollectorContext:
    """Build a CollectorContext wired to the given writer and pihole client."""
    return CollectorContext(
        config=CollectorConfig(
            name="pihole_ftl_messages",
            interval_seconds=60,
            timeout_seconds=15,
        ),
        db=None,  # type: ignore[arg-type]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="pihole_ftl_messages"),
        pihole=pihole,  # type: ignore[arg-type]
    )


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_three_messages() -> None:
    """3 messages (2 LIST, 1 LOAD) -> count==3, per-type emitted, ok=True."""
    payload: dict[str, object] = {
        "messages": [
            {"id": 1, "timestamp": 1700000000, "type": "LIST", "plain": "a", "html": "a"},
            {"id": 2, "timestamp": 1700000001, "type": "LIST", "plain": "b", "html": "b"},
            {"id": 3, "timestamp": 1700000002, "type": "LOAD", "plain": "c", "html": "c"},
        ],
        "took": 0.000042,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlMessagesCollector()
    ctx = _ctx(writer, _FakeFtlMessagesOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == []
    assert result.metrics_emitted == 4  # noqa: PLR2004
    # api_took
    api_took = _gauge_value(writer, M_API_TOOK, {"endpoint": "info/messages"})
    assert api_took == pytest.approx(0.000042)  # pyright: ignore[reportUnknownMemberType]
    # total count
    count_val = _gauge_value(writer, M_MESSAGES_COUNT, {})
    assert count_val == pytest.approx(3.0)  # pyright: ignore[reportUnknownMemberType]
    # per-type
    list_val = _gauge_value(writer, M_MESSAGES_BY_TYPE, {"type": "LIST"})
    assert list_val == pytest.approx(2.0)  # pyright: ignore[reportUnknownMemberType]
    load_val = _gauge_value(writer, M_MESSAGES_BY_TYPE, {"type": "LOAD"})
    assert load_val == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_empty_messages_list() -> None:
    """messages: [] → count==0.0, no per-type series, ok=True."""
    payload: dict[str, object] = {"messages": [], "took": 0.000042}
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlMessagesCollector()
    ctx = _ctx(writer, _FakeFtlMessagesOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == []
    assert result.metrics_emitted == 2  # noqa: PLR2004
    count_val = _gauge_value(writer, M_MESSAGES_COUNT, {})
    assert count_val == 0.0
    assert M_MESSAGES_BY_TYPE not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_ctx_pihole_none() -> None:
    """ctx.pihole is None → ok=False, error message, 0 emits."""
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlMessagesCollector()
    result = await collector.run(_ctx(writer, None))
    assert result.ok is False
    assert result.errors == ["pihole client not configured"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_info_messages_returns_pihole_error() -> None:
    """info_messages() returns PiholeError → ok=False, errors carries message, 0 emits."""
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlMessagesCollector()
    ctx = _ctx(writer, _FakeFtlMessagesError("GET /api/info/messages: timed out"))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["GET /api/info/messages: timed out"]
    assert result.metrics_emitted == 0
    assert writer.recorded == []  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_payload_not_a_dict() -> None:
    """payload is a list (not a dict) → ok=False, errors=["unexpected payload shape"],
    metrics_emitted==1 (api_took already counted)."""
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlMessagesCollector()
    ctx = _ctx(writer, _FakeFtlMessagesOk(["not", "a", "dict"]))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["unexpected payload shape"]
    assert result.metrics_emitted == 1  # api_took already emitted


@pytest.mark.asyncio
async def test_messages_key_missing() -> None:
    """payload has no 'messages' key → ok=False, messages-not-a-list error,
    metrics_emitted==1 (api_took counted), no count metric."""
    payload: dict[str, object] = {"took": 0.000042}
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlMessagesCollector()
    ctx = _ctx(writer, _FakeFtlMessagesOk(payload))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["unexpected payload shape (messages not a list)"]
    assert result.metrics_emitted == 1
    assert M_MESSAGES_COUNT not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_messages_not_a_list() -> None:
    """payload['messages'] is a string (not a list) → ok=False, same error,
    metrics_emitted==1, no count metric."""
    payload: dict[str, object] = {"messages": "x", "took": 0.000042}
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlMessagesCollector()
    ctx = _ctx(writer, _FakeFtlMessagesOk(payload))
    result = await collector.run(ctx)
    assert result.ok is False
    assert result.errors == ["unexpected payload shape (messages not a list)"]
    assert result.metrics_emitted == 1
    assert M_MESSAGES_COUNT not in _all_metric_names(writer)


@pytest.mark.asyncio
async def test_malformed_entry_skipped_in_per_type() -> None:
    """messages=[{type:"LIST"}, "garbage"] → count==2 (raw len), LIST==1,
    sum-of-per-type (1) < count (2) is acceptable documented behaviour."""
    payload: dict[str, object] = {
        "messages": [
            {"id": 1, "timestamp": 1700000000, "type": "LIST", "plain": "a", "html": "a"},
            "garbage",
        ],
        "took": 0.000042,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlMessagesCollector()
    ctx = _ctx(writer, _FakeFtlMessagesOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    count_val = _gauge_value(writer, M_MESSAGES_COUNT, {})
    assert count_val == pytest.approx(2.0)  # pyright: ignore[reportUnknownMemberType]
    list_val = _gauge_value(writer, M_MESSAGES_BY_TYPE, {"type": "LIST"})
    assert list_val == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_non_string_type_falls_back_to_unknown() -> None:
    """Non-string / missing type field → grouped under type="unknown".
    messages=[{type:123}, {}] → unknown==2, count==2."""
    payload: dict[str, object] = {
        "messages": [
            {"id": 1, "timestamp": 1700000000, "type": 123, "plain": "a", "html": "a"},
            {"id": 2, "timestamp": 1700000001, "plain": "b", "html": "b"},
        ],
        "took": 0.000042,
    }
    writer = InMemoryMetricsWriter()
    collector = PiholeFtlMessagesCollector()
    ctx = _ctx(writer, _FakeFtlMessagesOk(payload))

    result = await collector.run(ctx)

    assert result.ok is True
    count_val = _gauge_value(writer, M_MESSAGES_COUNT, {})
    assert count_val == pytest.approx(2.0)  # pyright: ignore[reportUnknownMemberType]
    unknown_val = _gauge_value(writer, M_MESSAGES_BY_TYPE, {"type": "unknown"})
    assert unknown_val == pytest.approx(2.0)  # pyright: ignore[reportUnknownMemberType]


def test_metric_name_constants_match_contract() -> None:
    """Metric-name constants must equal the literal contract names."""
    assert M_API_TOOK == "homelab_pihole_api_took_seconds"
    assert M_MESSAGES_COUNT == "homelab_pihole_messages_count"
    assert M_MESSAGES_BY_TYPE == "homelab_pihole_messages"


@pytest.mark.asyncio
async def test_registration() -> None:
    """PiholeFtlMessagesCollector is registered via register_all + PluginLoader."""
    loader = MagicMock(spec=PluginLoader)
    register_all(loader)

    registered_classes = [call.args[0] for call in loader.register.call_args_list]
    assert PiholeFtlMessagesCollector in registered_classes
