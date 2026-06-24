"""Unit tests for the synology_replication collector (STAGE-008-011, fixture-based).

100% branch coverage of replication.py. Field names are INFERRED (zero live
snapshots); fixtures are hand-built. Exercises the CO-EQUAL combine (ok=False
ONLY when EVERYTHING fails), per-share fetch isolation, and the self-correcting
replication sentinel's BOTH sides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from homelab_monitor.kernel.plugins.context import CollectorContext

from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology.replication import (
    M_REPLICATION_AVAILABLE,
    M_SNAPSHOT_COUNT,
    M_SNAPSHOT_LATEST_AGE_SECONDS,
    M_SNAPSHOTS_CONFIGURED,
    SynologyReplicationCollector,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 300.0
_EXPECTED_TIMEOUT = 30.0

# 4 cap-routed families emitted by _emit.
_FAMILY_COUNT = 4

_EXPECTED_LATEST_EPOCH = 1_700_086_400.0
_STRING_TIME = "2026-06-20 03:00:00"
_EXPECTED_STRING_EPOCH = datetime(2026, 6, 20, 3, 0, 0, tzinfo=UTC).timestamp()

_SNAP_COUNT_TWO = 2.0
_SNAP_COUNT_THREE = 3.0

_EXPECTED_SHARES_COUNT = 3
_EXPECTED_MIXED_SHARES_COUNT = 2
_EXPECTED_API_TOOK_COUNT = 2


def _resp(payload: object, endpoint: str) -> SynologyResponse:
    return SynologyResponse(payload=payload, took_seconds=0.5, endpoint=endpoint)


def _share_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.Share/list")


def _snap_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Core.Share.Snapshot/list")


def _repl_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Btrfs.Replica.Core/list")


def _shares_payload(*names: str) -> dict[str, object]:
    return {
        "shares": [
            {"name": n, "desc": "", "is_usb_share": False, "vol_path": "/volume1"} for n in names
        ],
        "total": len(names),
    }


def _empty_snap_payload() -> dict[str, object]:
    return {"snapshots": [], "total": 0}


def _snap_payload(*snaps: dict[str, object]) -> dict[str, object]:
    return {"snapshots": list(snaps), "total": len(snaps)}


def _repl_payload() -> dict[str, object]:
    """A non-error dict payload for the replica probe (only dict-ness matters)."""
    return {"tasks": []}


class _FakeSynology:
    """Stand-in for ctx.synology with 3 independently programmable methods.

    ``snapshots`` may be:
      - a SynologyResponse/SynologyError used for EVERY share, OR
      - a dict mapping share-name -> (SynologyResponse | SynologyError) for per-share data.
    Each defaults to a healthy empty payload.
    """

    def __init__(
        self,
        shares: object = None,
        snapshots: object = None,
        repl: object = None,
    ) -> None:
        self._shares = shares if shares is not None else _share_resp(_shares_payload())
        self._snapshots = snapshots
        self._repl = repl if repl is not None else _repl_resp(_repl_payload())

    async def share_list(self) -> object:
        return self._shares

    async def share_snapshot_list(self, name: str) -> object:
        snaps = self._snapshots
        if isinstance(snaps, dict):
            mapping = cast("dict[str, object]", snaps)
            return mapping.get(name, _snap_resp(_empty_snap_payload()))
        if snaps is None:
            return _snap_resp(_empty_snap_payload())
        return snaps

    async def replica_core_list(self) -> object:
        return self._repl


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in replication tests."""

    vm: MemoryRetainingMetricsWriter = field(default_factory=MemoryRetainingMetricsWriter)
    synology: object = None


def _ctx(writer: MemoryRetainingMetricsWriter, synology: object) -> _Ctx:
    return _Ctx(vm=writer, synology=synology)


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[str, float, dict[str, str]]]:
    return [g for g in writer.gauges if g[0] == name]


# --- ClassVar tests ---


def test_replication_classvars() -> None:
    """ClassVars match expected constants."""
    assert SynologyReplicationCollector.name == "synology_replication"
    assert SynologyReplicationCollector.interval == timedelta(seconds=300)
    assert SynologyReplicationCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologyReplicationCollector.timeout == timedelta(seconds=30)
    assert SynologyReplicationCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologyReplicationCollector.concurrency_group == "synology"


# --- Test 1: empty live path ---


async def test_replication_empty_live_path() -> None:
    """Three empty shares, repl probe error -> available=0, configured=0, ok=True."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload("Audiobooks", "Books", "Comics")),
                repl=SynologyError(reason="api_error", message="DSM error 103"),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    count_gauges = _gauges_named(writer, M_SNAPSHOT_COUNT)
    assert len(count_gauges) == _EXPECTED_SHARES_COUNT
    assert (M_SNAPSHOT_COUNT, 0.0, {"share": "Audiobooks"}) in count_gauges
    assert (M_SNAPSHOT_COUNT, 0.0, {"share": "Books"}) in count_gauges
    assert (M_SNAPSHOT_COUNT, 0.0, {"share": "Comics"}) in count_gauges
    assert _gauges_named(writer, M_SNAPSHOTS_CONFIGURED) == [(M_SNAPSHOTS_CONFIGURED, 0.0, {})]
    assert _gauges_named(writer, M_REPLICATION_AVAILABLE) == [(M_REPLICATION_AVAILABLE, 0.0, {})]
    assert _gauges_named(writer, M_SNAPSHOT_LATEST_AGE_SECONDS) == []


# --- Test 2: populated snapshots with epoch times (including MAX branch coverage) ---


async def test_replication_populated_snapshots() -> None:
    """One share with 2 snapshots (large epoch, then small epoch) covering latest MAX."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload("Movies")),
                snapshots={
                    "Movies": _snap_resp(
                        _snap_payload(
                            {"time": 1_700_086_400, "snapshot": "@GMT-1"},
                            {"time": 1_700_000_000, "snapshot": "@GMT-2"},
                        )
                    )
                },
                repl=_repl_resp(_empty_snap_payload()),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SNAPSHOT_COUNT) == [
        (M_SNAPSHOT_COUNT, _SNAP_COUNT_TWO, {"share": "Movies"})
    ]
    age = _gauges_named(writer, M_SNAPSHOT_LATEST_AGE_SECONDS)
    assert len(age) == 1
    assert age[0][2] == {"share": "Movies"}
    assert age[0][1] >= 0.0
    assert _gauges_named(writer, M_SNAPSHOTS_CONFIGURED) == [(M_SNAPSHOTS_CONFIGURED, 1.0, {})]


# --- Test 3a: snapshot time as string ---


async def test_replication_snapshot_time_string() -> None:
    """One snapshot with time as DSM string format; parses and emits age."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload("TV")),
                snapshots={
                    "TV": _snap_resp(_snap_payload({"time": _STRING_TIME, "snapshot": "@GMT-x"}))
                },
                repl=SynologyError(reason="api_error", message="unavailable"),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    age = _gauges_named(writer, M_SNAPSHOT_LATEST_AGE_SECONDS)
    assert len(age) == 1
    assert age[0][2] == {"share": "TV"}
    assert age[0][1] >= 0.0


# --- Test 3b: snapshot time unparseable (missing time/create_time) ---


async def test_replication_snapshot_time_unparseable() -> None:
    """Snapshot with no time/create_time → count emitted, age omitted."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload("TV")),
                snapshots={"TV": _snap_resp(_snap_payload({"snapshot": "@GMT-x", "desc": "d"}))},
                repl=SynologyError(reason="api_error", message="unavailable"),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SNAPSHOT_COUNT) == [(M_SNAPSHOT_COUNT, 1.0, {"share": "TV"})]
    assert _gauges_named(writer, M_SNAPSHOT_LATEST_AGE_SECONDS) == []


# --- Test 3c: snapshot time garbage string ---


async def test_replication_snapshot_time_garbage_string() -> None:
    """Snapshot with unparseable time string → age omitted, count=1."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload("TV")),
                snapshots={"TV": _snap_resp(_snap_payload({"time": "not a date"}))},
                repl=SynologyError(reason="api_error", message="unavailable"),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SNAPSHOT_LATEST_AGE_SECONDS) == []
    assert _gauges_named(writer, M_SNAPSHOT_COUNT) == [(M_SNAPSHOT_COUNT, 1.0, {"share": "TV"})]


# --- Test 4: create_time fallback ---


async def test_replication_create_time_fallback() -> None:
    """Snapshot with no time but valid create_time → age emitted from fallback."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload("Books")),
                snapshots={
                    "Books": _snap_resp(
                        _snap_payload({"create_time": 1_700_086_400, "snapshot": "@GMT-y"})
                    )
                },
                repl=SynologyError(reason="api_error", message="unavailable"),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_SNAPSHOT_LATEST_AGE_SECONDS)) == 1
    assert _gauges_named(writer, M_SNAPSHOT_COUNT) == [(M_SNAPSHOT_COUNT, 1.0, {"share": "Books"})]


# --- Test 5: mixed shares (OR accumulation of any_share_has_snapshots) ---


async def test_replication_mixed_shares() -> None:
    """Two shares: Movies with 3 snapshots, Comics empty -> configured=1, counts differ."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload("Movies", "Comics")),
                snapshots={
                    "Movies": _snap_resp(
                        _snap_payload(
                            {"time": 1},
                            {"time": 2},
                            {"time": 3},
                        )
                    ),
                    "Comics": _snap_resp(_empty_snap_payload()),
                },
                repl=SynologyError(reason="api_error", message="unavailable"),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    count_gauges = _gauges_named(writer, M_SNAPSHOT_COUNT)
    assert len(count_gauges) == _EXPECTED_MIXED_SHARES_COUNT
    assert (M_SNAPSHOT_COUNT, _SNAP_COUNT_THREE, {"share": "Movies"}) in count_gauges
    assert (M_SNAPSHOT_COUNT, 0.0, {"share": "Comics"}) in count_gauges
    assert _gauges_named(writer, M_SNAPSHOTS_CONFIGURED) == [(M_SNAPSHOTS_CONFIGURED, 1.0, {})]
    age_gauges = _gauges_named(writer, M_SNAPSHOT_LATEST_AGE_SECONDS)
    assert len(age_gauges) == 1
    assert age_gauges[0][2] == {"share": "Movies"}


# --- Test 6: replication probe returns dict (available=1) ---


async def test_replication_probe_dict_payload_available() -> None:
    """Replication probe returns a dict payload -> replication_available=1.0."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload()),
                repl=_repl_resp(_repl_payload()),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_REPLICATION_AVAILABLE) == [(M_REPLICATION_AVAILABLE, 1.0, {})]


# --- Test 7: replication probe returns non-dict (available=0) ---


async def test_replication_probe_non_dict_payload_unavailable() -> None:
    """Replication probe returns list (non-dict) -> replication_available=0.0."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload()),
                repl=_repl_resp(["not", "a", "dict"]),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_REPLICATION_AVAILABLE) == [(M_REPLICATION_AVAILABLE, 0.0, {})]


# --- Test 8: share list fails ---


async def test_replication_share_list_fails() -> None:
    """Share fetch fails; repl succeeds -> ok=True, no shares enumerated."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=SynologyError(reason="timeout", message="share timed out"),
                repl=_repl_resp({"replicas": []}),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["share timed out"]
    assert _gauges_named(writer, M_SNAPSHOT_COUNT) == []
    assert _gauges_named(writer, M_SNAPSHOTS_CONFIGURED) == [(M_SNAPSHOTS_CONFIGURED, 0.0, {})]
    assert _gauges_named(writer, M_REPLICATION_AVAILABLE) == [(M_REPLICATION_AVAILABLE, 1.0, {})]


# --- Test 9: one share fetch fails, other ok ---


async def test_replication_one_share_fetch_fails_other_ok() -> None:
    """Two shares: A fails, B succeeds -> B emitted, A skipped, ok=True."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload("A", "B")),
                snapshots={
                    "A": SynologyError(reason="timeout", message="A snap timed out"),
                    "B": _snap_resp(_snap_payload({"time": 1_700_000_000})),
                },
                repl=_repl_resp({"replicas": []}),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["A snap timed out"]
    assert _gauges_named(writer, M_SNAPSHOT_COUNT) == [(M_SNAPSHOT_COUNT, 1.0, {"share": "B"})]
    assert _gauges_named(writer, M_SNAPSHOTS_CONFIGURED) == [(M_SNAPSHOTS_CONFIGURED, 1.0, {})]


# --- Test 10: all fetches fail ---


async def test_replication_all_fetches_fail() -> None:
    """Share and repl both fail -> ok=False, sentinels still emit at 0."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=SynologyError(reason="timeout", message="share timed out"),
                repl=SynologyError(reason="api_error", message="DSM error 103"),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["share timed out", "DSM error 103"]
    assert _gauges_named(writer, M_SNAPSHOTS_CONFIGURED) == [(M_SNAPSHOTS_CONFIGURED, 0.0, {})]
    assert _gauges_named(writer, M_REPLICATION_AVAILABLE) == [(M_REPLICATION_AVAILABLE, 0.0, {})]
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT


# --- Test 11: share payload non-dict ---


async def test_replication_share_payload_non_dict() -> None:
    """Share response successful but payload is None -> no shares enumerated."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(None),
                repl=SynologyError(reason="api_error", message="unavailable"),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SNAPSHOT_COUNT) == []
    assert _gauges_named(writer, M_SNAPSHOTS_CONFIGURED) == [(M_SNAPSHOTS_CONFIGURED, 0.0, {})]


# --- Test 12: snapshot payload non-dict ---


async def test_replication_snap_payload_non_dict() -> None:
    """Snapshot response successful but payload is non-dict -> skip parse."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload("X")),
                snapshots={"X": _snap_resp("not a dict")},
                repl=SynologyError(reason="api_error", message="unavailable"),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SNAPSHOT_COUNT) == []
    assert _gauges_named(writer, M_SNAPSHOTS_CONFIGURED) == [(M_SNAPSHOTS_CONFIGURED, 0.0, {})]


# --- Test 13: share with missing name ---


async def test_replication_share_missing_name_skipped() -> None:
    """Share entry without name is skipped; named share is enumerated."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp({"shares": [{"desc": "x"}, {"name": "Good"}], "total": 2}),
                repl=SynologyError(reason="api_error", message="unavailable"),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SNAPSHOT_COUNT) == [(M_SNAPSHOT_COUNT, 0.0, {"share": "Good"})]


# --- Test 14: share with empty name ---


async def test_replication_share_empty_name_skipped() -> None:
    """Share with name='   ' (whitespace only) is dropped."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp({"shares": [{"name": "   "}, {"name": "Real"}], "total": 2}),
                repl=SynologyError(reason="api_error", message="unavailable"),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_SNAPSHOT_COUNT) == [(M_SNAPSHOT_COUNT, 0.0, {"share": "Real"})]


# --- Test 15: unconfigured client ---


async def test_replication_unconfigured_client() -> None:
    """ctx.synology is None -> ok=False, no metrics emitted."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, None))

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


# --- Test 16: metrics accounting ---


async def test_replication_metrics_emitted_accounting() -> None:
    """Metrics emitted count is accurate."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                shares=_share_resp(_shares_payload()),
                repl=_repl_resp({"replicas": []}),
            ),
        ),
    )

    collector = SynologyReplicationCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    api_took = _gauges_named(writer, _API_TOOK)
    assert len(api_took) == _EXPECTED_API_TOOK_COUNT
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT
    assert result.metrics_emitted == len(writer.gauges)
