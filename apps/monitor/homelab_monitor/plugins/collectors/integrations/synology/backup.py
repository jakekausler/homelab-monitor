"""synology_backup collector — Hyper Backup task health + repository/aggregate counts.

EPIC-008 STAGE-008-010. Fetches TWO CO-EQUAL DSM APIs in one 5-min tick:
  - SYNO.Backup.Task/list       -> task_list[] of backup jobs (per-job health)
  - SYNO.Backup.Repository/list -> repo_list[]  (COUNTED ONLY, no inner parse)

CO-EQUAL COMBINE (mirrors STAGE-008-009 ups.py): there is NO primary. ``_fetch``
records-and-continues on EITHER fetch's client error; the run is ok=False ONLY
when BOTH fetches fail (``ok = task_resp is not None or repo_resp is not None``).
A single-fetch failure is a DEGRADED ok=True run. ``_emit`` ALWAYS runs.

FIELD NAMES ARE INFERRED. There are zero live backup jobs, so the per-task field
keys are centralized in ``_FIELDS`` (logical name -> DSM key). The tolerant
helpers degrade a wrong/absent field to None -> that metric is skipped. The
EMPTY-PATH aggregates (configured_count / no_backup_configured / repository_count)
are the alertable contract and are always emitted, even on an empty NAS.

PARSE — defensive. ``as_list_of_dicts(nested(payload, "task_list"))`` yields the
job records; per record each field is read through a ``_FIELDS`` key with
``as_float`` / ``nested`` / ``bool_to_gauge`` / ``_parse_dsm_time``. Emit-if-present
guards drop a missing field's metric.

STATE-SET: ``M_BACKUP_LAST_RESULT`` ({job,result}=1.0, observed result) and
``M_BACKUP_STATUS`` ({job,state}=1.0, observed state) are per-state series, each
paired with an always-per-job scalar (``last_result_ok`` / ``status_error``).

CARDINALITY: every family is cap-routed through ``capped_emitter`` +
``cap_for_synology`` (default 500). ``metrics_emitted`` = sum of
``emit_family() + 1`` per family + the api_took gauges from each successful fetch.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology._shared import (
    as_dict,
    as_float,
    as_list_of_dicts,
    bool_to_gauge,
    cap_for_synology,
    capped_emitter,
    client_unconfigured_result,
    fetch_or_result,
    nested,
)

# --- Metric family names
# Labels (kept out of inline comments to stay <=100 cols):
#   info                   : {job, name} = 1.0           (identity, name optional)
#   configured_count       : no labels (single series, always emitted)
#   no_backup_configured   : no labels (single series, always emitted)
#   repository_count       : no labels (single series, emitted when repo fetch ok)
#   enabled                : {job}                       (bool gauge)
#   data_size_bytes        : {job}                       (zero-size still emits)
#   last_run_timestamp     : {job}  (UTC epoch seconds)
#   last_run_age_seconds   : {job}  (now - last_run_timestamp, >= 0)
#   next_run_timestamp     : {job}  (UTC epoch seconds)
#   last_result            : {job, result} = 1.0         (per-result state-set)
#   last_result_ok         : {job}  1.0 success / 0.0 fail (omit when unknown)
#   status                 : {job, state} = 1.0          (per-state state-set)
#   status_error           : {job}  1.0 if state in _ERROR_STATES else 0.0
M_BACKUP_INFO: Final[str] = "homelab_synology_backup_info"
M_BACKUP_CONFIGURED_COUNT: Final[str] = "homelab_synology_backup_configured_count"
M_NO_BACKUP_CONFIGURED: Final[str] = "homelab_synology_no_backup_configured"
M_BACKUP_REPOSITORY_COUNT: Final[str] = "homelab_synology_backup_repository_count"
M_BACKUP_ENABLED: Final[str] = "homelab_synology_backup_enabled"
M_BACKUP_DATA_SIZE_BYTES: Final[str] = "homelab_synology_backup_data_size_bytes"
M_BACKUP_LAST_RUN_TIMESTAMP: Final[str] = "homelab_synology_backup_last_run_timestamp"
M_BACKUP_LAST_RUN_AGE_SECONDS: Final[str] = "homelab_synology_backup_last_run_age_seconds"
M_BACKUP_NEXT_RUN_TIMESTAMP: Final[str] = "homelab_synology_backup_next_run_timestamp"
M_BACKUP_LAST_RESULT: Final[str] = "homelab_synology_backup_last_result"
M_BACKUP_LAST_RESULT_OK: Final[str] = "homelab_synology_backup_last_result_ok"
M_BACKUP_STATUS: Final[str] = "homelab_synology_backup_status"
M_BACKUP_STATUS_ERROR: Final[str] = "homelab_synology_backup_status_error"

# --- Inferred DSM field keys (centralized — ZERO live jobs, so unverified)
# logical name -> DSM task-entry key. Correcting a wrong guess later is a 1-line edit.
_FIELDS: Final[dict[str, str]] = {
    "task_id": "task_id",
    "name": "name",
    "state": "state",
    "enabled": "enabled",
    "data_size": "data_size",
    "last_bkp_time": "last_bkp_time",
    "last_bkp_result": "last_bkp_result",
    "next_bkp_time": "next_bkp_time",
}

# last_bkp_result values that map last_result_ok to a scalar (others -> omit both).
_RESULT_SUCCESS: Final[str] = "success"
_RESULT_FAIL: Final[str] = "fail"
_RESULT_OK: Final[frozenset[str]] = frozenset({_RESULT_SUCCESS, _RESULT_FAIL})

# state strings that mark a job as errored (status_error=1.0).
_ERROR_STATES: Final[frozenset[str]] = frozenset({"error", "unavailable"})

# DSM timestamp string format (UTC). last_bkp_time / next_bkp_time may be epoch OR this.
_DSM_TIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Local DSM-shape helpers (NOT shared — backup-specific)
# ---------------------------------------------------------------------------


def _parse_dsm_time(v: object) -> float | None:
    """Parse a DSM timestamp to a UTC epoch float.

    Accepts EITHER a numeric epoch (int/float/numeric-str, via as_float) returned
    as-is, OR a string ``"%Y-%m-%d %H:%M:%S"`` parsed as UTC. Anything else /
    unparseable -> None. (All internal timestamps are UTC.)
    """
    epoch = as_float(v)
    if epoch is not None:
        return epoch
    if isinstance(v, str):
        try:
            parsed = datetime.strptime(v.strip(), _DSM_TIME_FORMAT)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC).timestamp()
    return None


def _result_ok(result: str) -> float | None:
    """Map a last_bkp_result string to last_result_ok scalar; None if unknown."""
    if result == _RESULT_SUCCESS:
        return 1.0
    if result == _RESULT_FAIL:
        return 0.0
    return None


# ---------------------------------------------------------------------------
# Multi-fetch wrapper: record-and-continue for INDEPENDENT fetches
# (copied verbatim from STAGE-008-009 ups.py)
# ---------------------------------------------------------------------------


def _fetch(
    ctx: CollectorContext,
    response: SynologyResponse | SynologyError,
    start: float,
    emitted: list[int],
    errors: list[str],
) -> SynologyResponse | None:
    """Wrap fetch_or_result for INDEPENDENT (non-early-returning) fetches.

    On a client error fetch_or_result returns a CollectorResult (errors
    populated); we record those error strings into ``errors`` and return None
    instead of aborting. On success it has already emitted api_took + bumped
    emitted[0]; we return the SynologyResponse.
    """
    r = fetch_or_result(ctx, response, start, emitted)
    if isinstance(r, CollectorResult):
        errors.extend(r.errors)
        return None
    return r


# ---------------------------------------------------------------------------
# Per-tick observation accumulator
# ---------------------------------------------------------------------------


class _Built:
    """Per-tick observation lists, one per cap-routed metric family."""

    __slots__ = (
        "configured_count_obs",
        "data_size_bytes_obs",
        "enabled_obs",
        "info_obs",
        "last_result_obs",
        "last_result_ok_obs",
        "last_run_age_seconds_obs",
        "last_run_timestamp_obs",
        "next_run_timestamp_obs",
        "no_backup_configured_obs",
        "repository_count_obs",
        "status_error_obs",
        "status_obs",
    )

    def __init__(self) -> None:
        """Initialise every observation list empty."""
        self.info_obs: list[tuple[dict[str, str], float]] = []
        self.configured_count_obs: list[tuple[dict[str, str], float]] = []
        self.no_backup_configured_obs: list[tuple[dict[str, str], float]] = []
        self.repository_count_obs: list[tuple[dict[str, str], float]] = []
        self.enabled_obs: list[tuple[dict[str, str], float]] = []
        self.data_size_bytes_obs: list[tuple[dict[str, str], float]] = []
        self.last_run_timestamp_obs: list[tuple[dict[str, str], float]] = []
        self.last_run_age_seconds_obs: list[tuple[dict[str, str], float]] = []
        self.next_run_timestamp_obs: list[tuple[dict[str, str], float]] = []
        self.last_result_obs: list[tuple[dict[str, str], float]] = []
        self.last_result_ok_obs: list[tuple[dict[str, str], float]] = []
        self.status_obs: list[tuple[dict[str, str], float]] = []
        self.status_error_obs: list[tuple[dict[str, str], float]] = []


# ---------------------------------------------------------------------------
# Parse passes
# ---------------------------------------------------------------------------


def _job_key(task: dict[str, object]) -> str | None:
    """Return the {job} label for a task record.

    PRIMARY: stringified ``task_id`` (int/float/str). FALLBACK: ``name`` (str).
    None when neither yields a usable key -> caller skips the whole record.
    """
    raw_id = task.get(_FIELDS["task_id"])
    if isinstance(raw_id, bool):
        pass  # bool is an int subclass; never a task id
    elif isinstance(raw_id, int):
        return str(raw_id)
    elif isinstance(raw_id, str) and raw_id.strip():
        return raw_id.strip()
    name = task.get(_FIELDS["name"])
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _parse_job(built: _Built, task: dict[str, object], now: float) -> None:
    """Append per-job observations for one task record.

    PRIMARY KEY: ``_job_key`` (task_id, else name). A record with neither is
    skipped entirely. Every other field is emit-if-present (None -> skip).
    """
    job = _job_key(task)
    if job is None:
        return
    jlabels = {"job": job}

    # Identity series — anchored on job; ``name`` label added only when present.
    # Separate dict from the shared per-job labels so adding `name` here does not
    # leak into the labels used by the other families.
    info_labels = {"job": job}
    name = task.get(_FIELDS["name"])
    if isinstance(name, str) and name.strip():
        info_labels["name"] = name.strip()
    built.info_obs.append((info_labels, 1.0))

    # enabled (bool gauge)
    enabled = bool_to_gauge(task.get(_FIELDS["enabled"]))
    if enabled is not None:
        built.enabled_obs.append((jlabels, enabled))

    # data_size (bytes) — zero-size MUST still emit (as_float(0) == 0.0, not None).
    data_size = as_float(task.get(_FIELDS["data_size"]))
    if data_size is not None:
        built.data_size_bytes_obs.append((jlabels, data_size))

    # last_bkp_time -> last_run_timestamp + derived age
    last_run = _parse_dsm_time(task.get(_FIELDS["last_bkp_time"]))
    if last_run is not None:
        built.last_run_timestamp_obs.append((jlabels, last_run))
        built.last_run_age_seconds_obs.append((jlabels, max(0.0, now - last_run)))

    # next_bkp_time -> next_run_timestamp
    next_run = _parse_dsm_time(task.get(_FIELDS["next_bkp_time"]))
    if next_run is not None:
        built.next_run_timestamp_obs.append((jlabels, next_run))

    # last_bkp_result -> state-set + scalar (omit both when result unknown/absent)
    result = task.get(_FIELDS["last_bkp_result"])
    if isinstance(result, str):
        ok = _result_ok(result)
        if ok is not None:
            built.last_result_obs.append(({"job": job, "result": result}, 1.0))
            built.last_result_ok_obs.append((jlabels, ok))

    # state -> status state-set + status_error scalar (both only when state present)
    state = task.get(_FIELDS["state"])
    if isinstance(state, str) and state:
        built.status_obs.append(({"job": job, "state": state}, 1.0))
        is_error = 1.0 if state in _ERROR_STATES else 0.0
        built.status_error_obs.append((jlabels, is_error))


def _parse_tasks(built: _Built, task_payload: dict[str, object], now: float) -> int:
    """Parse the task_list[] slice; return the parsed task-record count.

    The count is len of the as_list_of_dicts view (non-dict entries dropped),
    which feeds configured_count + the no_backup_configured derivation.
    """
    tasks = as_list_of_dicts(nested(task_payload, "task_list"))
    for task in tasks:
        _parse_job(built, task, now)
    return len(tasks)


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every family through one CappedEmitter."""
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    family(M_BACKUP_INFO, built.info_obs)
    family(M_BACKUP_CONFIGURED_COUNT, built.configured_count_obs)
    family(M_NO_BACKUP_CONFIGURED, built.no_backup_configured_obs)
    family(M_BACKUP_REPOSITORY_COUNT, built.repository_count_obs)
    family(M_BACKUP_ENABLED, built.enabled_obs)
    family(M_BACKUP_DATA_SIZE_BYTES, built.data_size_bytes_obs)
    family(M_BACKUP_LAST_RUN_TIMESTAMP, built.last_run_timestamp_obs)
    family(M_BACKUP_LAST_RUN_AGE_SECONDS, built.last_run_age_seconds_obs)
    family(M_BACKUP_NEXT_RUN_TIMESTAMP, built.next_run_timestamp_obs)
    family(M_BACKUP_LAST_RESULT, built.last_result_obs)
    family(M_BACKUP_LAST_RESULT_OK, built.last_result_ok_obs)
    family(M_BACKUP_STATUS, built.status_obs)
    family(M_BACKUP_STATUS_ERROR, built.status_error_obs)


class SynologyBackupCollector(BaseCollector):
    """Emit Hyper Backup task health + repository/aggregate counts from 2 CO-EQUAL DSM APIs.

    Polls once per 5-min tick in the ``synology`` concurrency group. Neither fetch
    is primary: a single fetch failing records its error but keeps ok=True with the
    other API's families still emitted; ok=False ONLY when BOTH fetches fail. An
    unconfigured client is ok=False. The configured_count / no_backup_configured /
    repository_count aggregates ALWAYS emit (the alertable empty-NAS contract).
    """

    name: ClassVar[str] = "synology_backup"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch task + repository lists co-equally, parse jobs, emit cap-routed families."""
        start = time.monotonic()
        if ctx.synology is None:
            return client_unconfigured_result(start)

        emitted: list[int] = [0]
        errors: list[str] = []
        events: list[CollectorEvent] = []
        built = _Built()
        now = datetime.now(UTC).timestamp()

        # CO-EQUAL fetch 1: task list.
        task_resp = _fetch(ctx, await ctx.synology.backup_task_list(), start, emitted, errors)
        task_count = 0
        task_parsed = False
        if task_resp is not None:
            task_payload = as_dict(task_resp.payload)
            if task_payload is not None:
                task_count = _parse_tasks(built, task_payload, now)
                task_parsed = True

        # CO-EQUAL fetch 2: repository list (COUNTED ONLY).
        repo_resp = _fetch(ctx, await ctx.synology.backup_repository_list(), start, emitted, errors)
        if repo_resp is not None:
            repo_payload = as_dict(repo_resp.payload)
            if repo_payload is not None:
                repo_count = len(as_list_of_dicts(nested(repo_payload, "repo_list")))
                built.repository_count_obs.append(({}, float(repo_count)))

        # ALWAYS-EMIT aggregates derived from the task fetch.
        # configured_count: parsed task-record count (0 when empty or fetch failed).
        built.configured_count_obs.append(({}, float(task_count)))
        # no_backup_configured: 1.0 iff task fetch SUCCEEDED (dict payload) AND count==0.
        no_backup = 1.0 if task_parsed and task_count == 0 else 0.0
        built.no_backup_configured_obs.append(({}, no_backup))

        # ALWAYS emit (even on a both-failed run: empty families emit drop gauge only).
        _emit(ctx, built, events, emitted)

        # CO-EQUAL: ok=False ONLY when BOTH fetches failed.
        ok = task_resp is not None or repo_resp is not None
        return CollectorResult(
            ok=ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=events,
            duration_seconds=time.monotonic() - start,
        )
