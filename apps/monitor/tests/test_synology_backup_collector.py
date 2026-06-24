"""Unit tests for the synology_backup collector (STAGE-008-010, fixture-based).

100% branch coverage of backup.py. Field names are INFERRED (zero live jobs);
fixtures are hand-built. Exercises the CO-EQUAL combine (ok=False ONLY when BOTH
fetches fail) + every emit-if-present guard's BOTH sides.
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
from homelab_monitor.plugins.collectors.integrations.synology.backup import (
    M_BACKUP_CONFIGURED_COUNT,
    M_BACKUP_DATA_SIZE_BYTES,
    M_BACKUP_ENABLED,
    M_BACKUP_INFO,
    M_BACKUP_LAST_RESULT,
    M_BACKUP_LAST_RESULT_OK,
    M_BACKUP_LAST_RUN_AGE_SECONDS,
    M_BACKUP_LAST_RUN_TIMESTAMP,
    M_BACKUP_NEXT_RUN_TIMESTAMP,
    M_BACKUP_REPOSITORY_COUNT,
    M_BACKUP_STATUS,
    M_BACKUP_STATUS_ERROR,
    M_NO_BACKUP_CONFIGURED,
    SynologyBackupCollector,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 300.0
_EXPECTED_TIMEOUT = 30.0

# 13 cap-routed families emitted by _emit.
_FAMILY_COUNT = 13

# Two co-equal fetches: task list + repository list.
_EXPECTED_API_TOOK_COUNT = 2

_EXPECTED_DATA_SIZE = 1024.0
_EXPECTED_LAST_EPOCH = 1_700_000_000.0
_EXPECTED_NEXT_EPOCH = 1_700_086_400.0
# "2026-06-20 03:00:00" UTC as epoch (computed, not hard-coded as a literal).
_STRING_TIME = "2026-06-20 03:00:00"
_EXPECTED_STRING_EPOCH = datetime(2026, 6, 20, 3, 0, 0, tzinfo=UTC).timestamp()


def _resp(payload: object, endpoint: str) -> SynologyResponse:
    return SynologyResponse(payload=payload, took_seconds=0.5, endpoint=endpoint)


def _task_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Backup.Task/list")


def _repo_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.Backup.Repository/list")


class _FakeSynology:
    """Stand-in for ctx.synology with 2 independently programmable methods."""

    def __init__(self, task: object = None, repo: object = None) -> None:
        self._task = task if task is not None else _task_resp(_empty_task_payload())
        self._repo = repo if repo is not None else _repo_resp(_empty_repo_payload())

    async def backup_task_list(self) -> object:
        return self._task

    async def backup_repository_list(self) -> object:
        return self._repo


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in backup tests."""

    vm: MemoryRetainingMetricsWriter = field(default_factory=MemoryRetainingMetricsWriter)
    synology: object = None


def _ctx(writer: MemoryRetainingMetricsWriter, synology: object) -> _Ctx:
    return _Ctx(vm=writer, synology=synology)


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[str, float, dict[str, str]]]:
    return [g for g in writer.gauges if g[0] == name]


# --- Fixtures (ground-truth empty payloads + hand-built jobs) ----------------


def _empty_task_payload() -> dict[str, object]:
    return {
        "is_data_restoring": False,
        "is_downloading": False,
        "is_lun_restoring": False,
        "is_restoring": False,
        "is_snapshot_restoring": False,
        "task_list": [],
        "total": 0,
    }


def _empty_repo_payload() -> dict[str, object]:
    return {"offset": 0, "repo_list": [], "total": 0}


def _full_job() -> dict[str, object]:
    """One fully-populated job (all _FIELDS present, healthy)."""
    return {
        "task_id": 3,
        "name": "DailyHyperBackup",
        "state": "backuping",
        "enabled": True,
        "data_size": 1024,
        "last_bkp_time": 1_700_000_000,
        "last_bkp_result": "success",
        "next_bkp_time": 1_700_086_400,
    }


def _task_payload_with(*jobs: dict[str, object]) -> dict[str, object]:
    return {**_empty_task_payload(), "task_list": list(jobs), "total": len(jobs)}


# --- ClassVar tests ---


def test_backup_classvars() -> None:
    """ClassVars match expected constants."""
    assert SynologyBackupCollector.name == "synology_backup"
    assert SynologyBackupCollector.interval == timedelta(seconds=300)
    assert SynologyBackupCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologyBackupCollector.timeout == timedelta(seconds=30)
    assert SynologyBackupCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologyBackupCollector.concurrency_group == "synology"


# --- Test 1: empty live path ---


@staticmethod
async def test_backup_empty_live_path() -> None:
    """Both fetches return empty task/repo payloads; task_parsed=True, no_backup=1."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_CONFIGURED_COUNT) == [
        (M_BACKUP_CONFIGURED_COUNT, 0.0, {})
    ]
    assert _gauges_named(writer, M_NO_BACKUP_CONFIGURED) == [(M_NO_BACKUP_CONFIGURED, 1.0, {})]
    assert _gauges_named(writer, M_BACKUP_REPOSITORY_COUNT) == [
        (M_BACKUP_REPOSITORY_COUNT, 0.0, {})
    ]
    assert _gauges_named(writer, M_BACKUP_INFO) == []
    assert _gauges_named(writer, M_BACKUP_STATUS) == []


# --- Test 2: one full job ---


async def test_backup_one_full_job() -> None:
    """_full_job() with all fields; every emit-if-present TRUE side."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(_full_job())))),
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_INFO) == [
        (M_BACKUP_INFO, 1.0, {"job": "3", "name": "DailyHyperBackup"})
    ]
    assert _gauges_named(writer, M_BACKUP_ENABLED) == [(M_BACKUP_ENABLED, 1.0, {"job": "3"})]
    assert _gauges_named(writer, M_BACKUP_DATA_SIZE_BYTES) == [
        (M_BACKUP_DATA_SIZE_BYTES, _EXPECTED_DATA_SIZE, {"job": "3"})
    ]
    assert _gauges_named(writer, M_BACKUP_LAST_RUN_TIMESTAMP) == [
        (M_BACKUP_LAST_RUN_TIMESTAMP, _EXPECTED_LAST_EPOCH, {"job": "3"})
    ]
    assert _gauges_named(writer, M_BACKUP_NEXT_RUN_TIMESTAMP) == [
        (M_BACKUP_NEXT_RUN_TIMESTAMP, _EXPECTED_NEXT_EPOCH, {"job": "3"})
    ]
    assert _gauges_named(writer, M_BACKUP_LAST_RESULT) == [
        (M_BACKUP_LAST_RESULT, 1.0, {"job": "3", "result": "success"})
    ]
    assert _gauges_named(writer, M_BACKUP_LAST_RESULT_OK) == [
        (M_BACKUP_LAST_RESULT_OK, 1.0, {"job": "3"})
    ]
    assert _gauges_named(writer, M_BACKUP_STATUS) == [
        (M_BACKUP_STATUS, 1.0, {"job": "3", "state": "backuping"})
    ]
    assert _gauges_named(writer, M_BACKUP_STATUS_ERROR) == [
        (M_BACKUP_STATUS_ERROR, 0.0, {"job": "3"})
    ]
    assert _gauges_named(writer, M_NO_BACKUP_CONFIGURED) == [(M_NO_BACKUP_CONFIGURED, 0.0, {})]
    assert _gauges_named(writer, M_BACKUP_CONFIGURED_COUNT) == [
        (M_BACKUP_CONFIGURED_COUNT, 1.0, {})
    ]
    age_gauges = _gauges_named(writer, M_BACKUP_LAST_RUN_AGE_SECONDS)
    assert len(age_gauges) == 1
    assert age_gauges[0][1] >= 0.0


# --- Test 3: failed job ---


async def test_backup_failed_job() -> None:
    """Job with last_bkp_result=fail and state=error."""
    failed_job = {
        **_full_job(),
        "last_bkp_result": "fail",
        "state": "error",
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(failed_job)))),
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_LAST_RESULT) == [
        (M_BACKUP_LAST_RESULT, 1.0, {"job": "3", "result": "fail"})
    ]
    assert _gauges_named(writer, M_BACKUP_LAST_RESULT_OK) == [
        (M_BACKUP_LAST_RESULT_OK, 0.0, {"job": "3"})
    ]
    assert _gauges_named(writer, M_BACKUP_STATUS) == [
        (M_BACKUP_STATUS, 1.0, {"job": "3", "state": "error"})
    ]
    assert _gauges_named(writer, M_BACKUP_STATUS_ERROR) == [
        (M_BACKUP_STATUS_ERROR, 1.0, {"job": "3"})
    ]


# --- Test 4: zero-size data still emits ---


async def test_backup_zero_size_still_emits() -> None:
    """data_size=0 must emit (as_float(0)==0.0, not None)."""
    job = {**_full_job(), "data_size": 0, "last_bkp_result": "success"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_DATA_SIZE_BYTES) == [
        (M_BACKUP_DATA_SIZE_BYTES, 0.0, {"job": "3"})
    ]


# --- Test 5a-k: missing fields & edge cases ---


async def test_backup_job_missing_name() -> None:
    """job without name label in info."""
    job = {k: v for k, v in _full_job().items() if k != "name"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_INFO) == [(M_BACKUP_INFO, 1.0, {"job": "3"})]


async def test_backup_job_missing_state() -> None:
    """no state → no status/status_error."""
    job = {k: v for k, v in _full_job().items() if k != "state"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_STATUS) == []
    assert _gauges_named(writer, M_BACKUP_STATUS_ERROR) == []


async def test_backup_job_missing_enabled() -> None:
    """no enabled → no enabled metric."""
    job = {k: v for k, v in _full_job().items() if k != "enabled"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_ENABLED) == []


async def test_backup_job_missing_data_size() -> None:
    """no data_size → no data_size_bytes metric."""
    job = {k: v for k, v in _full_job().items() if k != "data_size"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_DATA_SIZE_BYTES) == []


async def test_backup_job_missing_last_time() -> None:
    """no last_bkp_time → no timestamp/age metrics."""
    job = {k: v for k, v in _full_job().items() if k != "last_bkp_time"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_LAST_RUN_TIMESTAMP) == []
    assert _gauges_named(writer, M_BACKUP_LAST_RUN_AGE_SECONDS) == []


async def test_backup_job_missing_next_time() -> None:
    """no next_bkp_time → no next_run_timestamp metric."""
    job = {k: v for k, v in _full_job().items() if k != "next_bkp_time"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_NEXT_RUN_TIMESTAMP) == []


async def test_backup_job_missing_result() -> None:
    """no last_bkp_result → no result/result_ok metrics."""
    job = {k: v for k, v in _full_job().items() if k != "last_bkp_result"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_LAST_RESULT) == []
    assert _gauges_named(writer, M_BACKUP_LAST_RESULT_OK) == []


async def test_backup_job_unknown_result_omits() -> None:
    """result='none' (not success/fail) → omit both result metrics."""
    job = {**_full_job(), "last_bkp_result": "none"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_LAST_RESULT) == []
    assert _gauges_named(writer, M_BACKUP_LAST_RESULT_OK) == []


async def test_backup_job_empty_state_skipped() -> None:
    """state='' (empty string) → no status metrics."""
    job = {**_full_job(), "state": ""}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_STATUS) == []
    assert _gauges_named(writer, M_BACKUP_STATUS_ERROR) == []


async def test_backup_job_task_id_missing_falls_back_to_name() -> None:
    """task_id absent, name present → job={name}."""
    job = {k: v for k, v in _full_job().items() if k != "task_id"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_INFO) == [
        (M_BACKUP_INFO, 1.0, {"job": "DailyHyperBackup", "name": "DailyHyperBackup"})
    ]


async def test_backup_job_no_id_no_name_skipped() -> None:
    """neither task_id nor name → record skipped (but still counted)."""
    job = {k: v for k, v in _full_job().items() if k not in ("task_id", "name")}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_INFO) == []
    assert _gauges_named(writer, M_BACKUP_CONFIGURED_COUNT) == [
        (M_BACKUP_CONFIGURED_COUNT, 1.0, {})
    ]
    assert _gauges_named(writer, M_NO_BACKUP_CONFIGURED) == [(M_NO_BACKUP_CONFIGURED, 0.0, {})]


async def test_backup_job_bool_task_id_falls_back() -> None:
    """task_id=True (bool is int subclass) rejected, falls back to name."""
    job = {**_full_job(), "task_id": True}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_INFO) == [
        (M_BACKUP_INFO, 1.0, {"job": "DailyHyperBackup", "name": "DailyHyperBackup"})
    ]


async def test_backup_job_task_id_string() -> None:
    """task_id as string."""
    job = {**_full_job(), "task_id": "job7"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    info_gauges = _gauges_named(writer, M_BACKUP_INFO)
    assert info_gauges[0][2]["job"] == "job7"


# --- Test 6a-b: timestamp parsing ---


async def test_backup_last_time_string_parsed() -> None:
    """last_bkp_time as DSM string format."""
    job = {**_full_job(), "last_bkp_time": _STRING_TIME}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_LAST_RUN_TIMESTAMP) == [
        (M_BACKUP_LAST_RUN_TIMESTAMP, _EXPECTED_STRING_EPOCH, {"job": "3"})
    ]


async def test_backup_last_time_unparseable_string() -> None:
    """Unparseable timestamp string → None."""
    job = {**_full_job(), "last_bkp_time": "not a date"}
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext", _ctx(writer, _FakeSynology(task=_task_resp(_task_payload_with(job))))
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_LAST_RUN_TIMESTAMP) == []
    assert _gauges_named(writer, M_BACKUP_LAST_RUN_AGE_SECONDS) == []


# --- Test 7: both fetches fail ---


async def test_backup_both_fetches_fail() -> None:
    """Both task and repo fetches fail; ok=False; both errors recorded."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                task=SynologyError(reason="timeout", message="task timed out"),
                repo=SynologyError(reason="timeout", message="repo timed out"),
            ),
        ),
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["task timed out", "repo timed out"]
    assert _gauges_named(writer, M_NO_BACKUP_CONFIGURED) == [(M_NO_BACKUP_CONFIGURED, 0.0, {})]
    assert _gauges_named(writer, M_BACKUP_CONFIGURED_COUNT) == [
        (M_BACKUP_CONFIGURED_COUNT, 0.0, {})
    ]
    assert _gauges_named(writer, M_BACKUP_REPOSITORY_COUNT) == []
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT


# --- Test 8a-b: single fetch fails (degraded) ---


async def test_backup_task_fails_repo_ok_degraded() -> None:
    """Task fetch fails, repo succeeds; ok=True (degraded)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                task=SynologyError(reason="timeout", message="task timed out"),
                repo=_repo_resp(_empty_repo_payload()),
            ),
        ),
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["task timed out"]
    assert _gauges_named(writer, M_BACKUP_REPOSITORY_COUNT) == [
        (M_BACKUP_REPOSITORY_COUNT, 0.0, {})
    ]
    assert _gauges_named(writer, M_NO_BACKUP_CONFIGURED) == [(M_NO_BACKUP_CONFIGURED, 0.0, {})]


async def test_backup_repo_fails_task_ok_degraded() -> None:
    """Repo fetch fails, task succeeds (empty); ok=True (degraded)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer,
            _FakeSynology(
                task=_task_resp(_empty_task_payload()),
                repo=SynologyError(reason="timeout", message="repo timed out"),
            ),
        ),
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["repo timed out"]
    assert _gauges_named(writer, M_NO_BACKUP_CONFIGURED) == [(M_NO_BACKUP_CONFIGURED, 1.0, {})]
    assert _gauges_named(writer, M_BACKUP_REPOSITORY_COUNT) == []


# --- Test 9: unconfigured client ---


async def test_backup_unconfigured_client() -> None:
    """synology=None → unconfigured."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, None))

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert writer.gauges == []


# --- Test 10a-b: non-dict payloads ---


async def test_backup_task_payload_non_dict() -> None:
    """Task payload=None (non-dict) → task_parsed=False."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(task=_task_resp(None), repo=_repo_resp(_empty_repo_payload()))),
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_NO_BACKUP_CONFIGURED) == [(M_NO_BACKUP_CONFIGURED, 0.0, {})]
    assert _gauges_named(writer, M_BACKUP_CONFIGURED_COUNT) == [
        (M_BACKUP_CONFIGURED_COUNT, 0.0, {})
    ]
    assert _gauges_named(writer, M_BACKUP_REPOSITORY_COUNT) == [
        (M_BACKUP_REPOSITORY_COUNT, 0.0, {})
    ]


async def test_backup_repo_payload_non_dict() -> None:
    """Repo payload non-dict (string) → repository_count omitted."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(
            writer, _FakeSynology(task=_task_resp(_empty_task_payload()), repo=_repo_resp("nope"))
        ),
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_REPOSITORY_COUNT) == []
    assert _gauges_named(writer, M_NO_BACKUP_CONFIGURED) == [(M_NO_BACKUP_CONFIGURED, 1.0, {})]


# --- Test 11: repo with entries ---


async def test_backup_repo_with_entries_counted() -> None:
    """Repo list with 2 entries → count=2."""
    repo_payload = {
        "offset": 0,
        "repo_list": [{"id": 1}, {"id": 2}],
        "total": 2,
    }
    writer = MemoryRetainingMetricsWriter()
    ctx = cast(
        "CollectorContext",
        _ctx(writer, _FakeSynology(repo=_repo_resp(repo_payload))),
    )

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert _gauges_named(writer, M_BACKUP_REPOSITORY_COUNT) == [
        (M_BACKUP_REPOSITORY_COUNT, 2.0, {})
    ]


# --- Test 12: metrics accounting ---


async def test_backup_metrics_emitted_accounting() -> None:
    """api_took x2 (both fetches); drop x13 (family count)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))

    collector = SynologyBackupCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, _API_TOOK)) == _EXPECTED_API_TOOK_COUNT
    assert len(_gauges_named(writer, _DROP)) == _FAMILY_COUNT
    assert result.metrics_emitted == len(writer.gauges)
