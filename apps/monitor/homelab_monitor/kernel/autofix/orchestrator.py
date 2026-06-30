"""Auto-fix orchestrator (STAGE-009-005, keystone)."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import Alert, AlertOutcome
from homelab_monitor.kernel.autofix.matcher import matching_runbooks
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import DenialReason, RunMode, RunOutcome, RunResult
from homelab_monitor.kernel.config import FixerRunnerConfig
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.socket_client import (
    DockerExecTimeoutError,
    DockerSocketClient,
    DockerSocketError,
    ExecResult,
)
from homelab_monitor.kernel.runbooks.repository import RunbookRecord, RunbookRepo
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

_TRUTHY = frozenset({"true", "1", "yes"})
_STALE_CLAIM_SLACK_SECONDS = 300  # margin past exec_timeout before a claim is treated as orphaned


def _is_truthy(value: str | None) -> bool:
    """Interpret an app_settings string flag as a bool."""
    return value is not None and value.strip().lower() in _TRUTHY


class AutoFixOrchestrator:
    """Auto-fix orchestrator: alert -> match -> gate -> durable claim -> exec -> persist."""

    def __init__(  # noqa: PLR0913 -- keyword-only dependency injection
        self,
        *,
        runbook_repo: RunbookRepo,
        alert_repo: AlertRepository,
        app_settings_repo: AppSettingsRepository,
        secrets_repo: AsyncSecretsRepository,
        docker_client: DockerSocketClient,
        db: SqliteRepository,
        runs_repo: RunbookRunsRepository,
        config: FixerRunnerConfig,
        log: BoundLogger,
    ) -> None:
        self._runbook_repo = runbook_repo
        self._alert_repo = alert_repo
        self._app_settings_repo = app_settings_repo
        self._secrets_repo = secrets_repo
        self._docker = docker_client
        self._db = db
        self._runs = runs_repo
        self._config = config
        self._log = log
        self._locks: dict[str, asyncio.Lock] = {}
        # Process-wide lock serializing the transcript snapshot->exec->resolve
        # critical section across ALL runbooks. The per-runbook lock cannot
        # protect transcript-dir attribution because transcript_dir is a single
        # shared mount; two different runbooks executing concurrently would diff
        # the same dir and misattribute each other's transcript (Important #4).
        # Trade-off: this serializes ALL fixes process-wide. Acceptable for a
        # conservative single-user safety subsystem at this scale.
        self._transcript_lock = asyncio.Lock()

    def _lock_for(self, runbook_id: str) -> asyncio.Lock:
        """Get or create a per-runbook lock."""
        lock = self._locks.get(runbook_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[runbook_id] = lock
        return lock

    async def handle_alert(self, alert: Alert) -> RunResult | None:
        """Orchestrate auto-fix for an alert. Returns None only on no-match."""
        # Phase 1: match. No-match => record NOTHING, return None.
        records = await self._runbook_repo.list_runbooks()
        matched = matching_runbooks(records, alert)
        if not matched:
            return None
        if len(matched) > 1:
            return await self._deny(
                alert=alert,
                runbook_id=None,
                reason=DenialReason.AMBIGUOUS_MATCH,
                detail=f"matched {len(matched)} runbooks: " + ",".join(r.id for r in matched),
                extra={"runbook_ids": [r.id for r in matched]},
            )
        record = matched[0]

        # Phase 2: gates (strict order).
        denial = await self._check_gates(record)
        if denial is not None:
            return await self._deny(
                alert=alert,
                runbook_id=record.id,
                reason=denial,
                detail=self._gate_detail(record, denial),
            )

        # Phase 3: claim + exec.
        return await self._claim_and_exec(alert=alert, record=record)

    async def _check_gates(self, record: RunbookRecord) -> DenialReason | None:
        """Check all gates in strict order. Returns denial reason or None if all pass."""
        # 1. kill switch
        flag = await self._app_settings_repo.get("autofix_enabled")
        if not _is_truthy(flag):
            return DenialReason.KILL_SWITCH
        # 2. allow list
        if not (record.enabled and record.auto_trigger):
            return DenialReason.ALLOW_LIST
        # 3. rate limit (sliding 1h window over started_at)
        if record.rate_limit_per_hour is not None:
            threshold = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
            count = await self._runs.count_started_since(record.id, threshold)
            if count >= record.rate_limit_per_hour:
                return DenialReason.RATE_LIMIT
        # 4. cooldown
        if record.cooldown_seconds is not None and record.cooldown_seconds > 0:
            last_ended = await self._runs.latest_ended_at(record.id)
            if last_ended is not None:
                elapsed = (
                    datetime.now(tz=UTC) - datetime.fromisoformat(last_ended)
                ).total_seconds()
                if elapsed < record.cooldown_seconds:
                    return DenialReason.COOLDOWN
        # 5. risky / dry-run gate
        if record.dry_run_required:
            return DenialReason.RISKY_BLOCKED
        return None

    def _gate_detail(self, record: RunbookRecord, reason: DenialReason) -> str:
        """Return a short human detail string for the denial reason."""
        if reason == DenialReason.RATE_LIMIT:
            return f"limit={record.rate_limit_per_hour}/h"
        if reason == DenialReason.COOLDOWN:
            return f"cooldown={record.cooldown_seconds}s"
        return reason.value

    async def _in_lock_gate(
        self,
        conn: AsyncConnection,
        *,
        record: RunbookRecord,
        stale_threshold_iso: str,
        rate_threshold_iso: str,
    ) -> DenialReason | None:
        """Authoritative in-lock, in-txn gate re-check (Critical #1, Important #5).

        Precedence: inflight (already_running) -> rate_limit -> cooldown. Returns
        the denial reason or None if the claim may proceed. An inflight run also
        counts as cooldown-not-satisfied; the inflight check subsumes it.
        """
        # 1. inflight (staleness-aware): a fresh open-ended claim blocks.
        inflight = await self._runs.count_inflight(
            conn, record.id, stale_threshold_iso=stale_threshold_iso
        )
        if inflight > 0:
            return DenialReason.ALREADY_RUNNING
        # 2. rate limit (sliding 1h window over started_at).
        if record.rate_limit_per_hour is not None:
            count = await self._runs.count_started_since_conn(conn, record.id, rate_threshold_iso)
            if count >= record.rate_limit_per_hour:
                return DenialReason.RATE_LIMIT
        # 3. cooldown.
        if record.cooldown_seconds is not None and record.cooldown_seconds > 0:
            last_ended = await self._runs.latest_ended_at_conn(conn, record.id)
            if last_ended is not None:
                elapsed = (
                    datetime.now(tz=UTC) - datetime.fromisoformat(last_ended)
                ).total_seconds()
                if elapsed < record.cooldown_seconds:
                    return DenialReason.COOLDOWN
        return None

    def _in_lock_detail(self, record: RunbookRecord, reason: DenialReason) -> str:
        """Human detail string for an in-lock denial reason."""
        if reason == DenialReason.ALREADY_RUNNING:
            return "an in-flight run exists"
        if reason == DenialReason.RATE_LIMIT:
            return f"limit={record.rate_limit_per_hour}/h"
        if reason == DenialReason.COOLDOWN:
            return f"cooldown={record.cooldown_seconds}s"
        return reason.value

    async def _claim_and_exec(self, *, alert: Alert, record: RunbookRecord) -> RunResult:
        """Durable claim, exec, and persist. Always returns a RunResult (ran=True or False)."""
        host = socket.gethostname()  # computed ONCE; threaded into row + audit (Minor #5)
        prompt = record.path  # claude -p <runbook.path>
        now = datetime.now(tz=UTC)
        # Stale threshold: a claim older than (exec_timeout + slack) is treated as
        # orphaned and NOT inflight (Important #1a).
        stale_threshold_iso = (
            now - timedelta(seconds=self._config.exec_timeout_seconds + _STALE_CLAIM_SLACK_SECONDS)
        ).isoformat()
        rate_threshold_iso = (now - timedelta(hours=1)).isoformat()

        async with self._lock_for(record.id):
            # --- Atomic claim: re-evaluate inflight + rate + cooldown, then insert,
            #     all in ONE txn under the per-runbook lock (Critical #1, Important #5). ---
            try:
                async with self._db.transaction() as conn:
                    # Authoritative gate precedence: inflight -> rate -> cooldown.
                    in_lock_denial = await self._in_lock_gate(
                        conn,
                        record=record,
                        stale_threshold_iso=stale_threshold_iso,
                        rate_threshold_iso=rate_threshold_iso,
                    )
                    if in_lock_denial is not None:
                        await insert_audit(
                            conn,
                            who="system:autofix",
                            what="autofix.denied",
                            after={
                                "runbook_id": record.id,
                                "alert_id": alert.id,
                                "gate": in_lock_denial.value,
                                "detail": self._in_lock_detail(record, in_lock_denial),
                            },
                        )
                        return RunResult(
                            ran=False,
                            outcome=RunOutcome.DENIED,
                            runbook_id=record.id,
                            run_id=None,
                            exit_code=None,
                            denial_reason=in_lock_denial,
                        )
                    # TODO(STAGE-009-007): startup stale-claim reaper — mark orphaned
                    # ended_at IS NULL runs ended; see Important #1. The kill-switch /
                    # mid-run-kill stage (STAGE-009-007) is the natural reconciler owner.
                    # Until then, staleness-aware count_inflight (above) self-heals.
                    run_id = await self._runs.insert_started(
                        conn,
                        runbook_id=record.id,
                        alert_id=alert.id,
                        prompt=prompt,
                        fixer_user=self._config.fixer_user,
                        host=host,
                        runbook_hash=record.content_hash,
                        mode=RunMode.REAL,
                    )
            except Exception as exc:  # claim/insert DB failure must be audited, not dropped
                # Critical #2: the attempt must NOT be silently dropped. Audit in a
                # FRESH txn and return a coherent DENIED/claim_error result.
                self._log.exception("autofix_claim_error", runbook_id=record.id, alert_id=alert.id)
                async with self._db.transaction() as conn:
                    await insert_audit(
                        conn,
                        who="system:autofix",
                        what="autofix.claim_error",
                        after={
                            "runbook_id": record.id,
                            "alert_id": alert.id,
                            "gate": DenialReason.CLAIM_ERROR.value,
                            "error": str(exc),
                        },
                    )
                return RunResult(
                    ran=False,
                    outcome=RunOutcome.DENIED,
                    runbook_id=record.id,
                    run_id=None,
                    exit_code=None,
                    denial_reason=DenialReason.CLAIM_ERROR,
                )

            # --- Exec. Serialized process-wide for transcript-dir attribution
            #     safety (Important #4). ---
            api_key = await self._secrets_repo.get("ANTHROPIC_API_KEY")
            env: dict[str, str] = {}
            if api_key is not None:
                env["ANTHROPIC_API_KEY"] = api_key

            transcript_dir = self._config.transcript_dir
            errored = False
            exec_result: ExecResult
            error_msg: str | None = None
            async with self._transcript_lock:
                before = self._snapshot_dir(transcript_dir)
                exec_started = datetime.now(tz=UTC)
                try:
                    async with self._maintenance_window(record, alert):
                        exec_result = await self._docker.exec_capture(
                            container_id=self._config.container,
                            cmd=["claude", "-p", record.path, "--dangerously-skip-permissions"],
                            timeout_seconds=self._config.exec_timeout_seconds,
                            user=self._config.fixer_user,
                            env=env or None,
                        )
                except DockerSocketError as exc:
                    # Expected docker exec failure (incl. timeout). Genuinely
                    # unexpected exceptions propagate to the dispatcher (Important #3b).
                    errored = True
                    error_msg = str(exc)
                    is_timeout = isinstance(exc, DockerExecTimeoutError)
                    exec_result = ExecResult(
                        exit_code=124 if is_timeout else 1,
                        stdout="",
                        stderr="",
                    )
                exec_ended = datetime.now(tz=UTC)
                transcript_path = self._resolve_transcript(
                    transcript_dir, before, started=exec_started, ended=exec_ended
                )

        # Lock(s) released. Persist completion + exec.log + outcome + audit.
        exec_log_path = self._write_exec_log(
            run_id=run_id,
            alert=alert,
            record=record,
            exec_result=exec_result,
            error=error_msg,
        )

        if errored:
            # Exec failed: completion + error audit in ONE txn (no outcome).
            async with self._db.transaction() as conn:
                await self._runs.mark_completed_conn(
                    conn,
                    run_id=run_id,
                    exit_code=exec_result.exit_code,
                    transcript_path=transcript_path,
                )
                await insert_audit(
                    conn,
                    who="system:autofix",
                    what="autofix.exec_error",
                    after={
                        "runbook_id": record.id,
                        "alert_id": alert.id,
                        "run_id": run_id,
                        "error": error_msg,
                        "exec_log_path": exec_log_path,
                    },
                )
        else:
            # Exec succeeded: completion + autofix.ran audit + (exit 0) outcome,
            # all ONE txn (Important #2).
            await self._persist_outcome(
                alert=alert,
                record=record,
                run_id=run_id,
                exec_result=exec_result,
                transcript_path=transcript_path,
                exec_log_path=exec_log_path,
                host=host,
            )

        return RunResult(
            ran=True,
            outcome=RunOutcome.RAN,
            runbook_id=record.id,
            run_id=run_id,
            exit_code=exec_result.exit_code,
            denial_reason=None,
        )

    def _snapshot_dir(self, path: str) -> set[str]:
        """Snapshot the directory listing (for transcript discovery)."""
        try:
            return set(os.listdir(path))
        except OSError:
            return set()

    def _resolve_transcript(
        self, path: str, before: set[str], *, started: datetime, ended: datetime
    ) -> str | None:
        """Resolve transcript path from dir-diff scan: newest NEW .transcript file
        whose mtime falls within the run's [started, ended] window.

        The mtime window is defense-in-depth on top of the process-wide
        transcript lock (Important #4): even a same-second race cannot attribute
        a file written outside this run's exec window.
        """
        try:
            after = set(os.listdir(path))
        except OSError:
            return None
        start_ts = started.timestamp()
        end_ts = ended.timestamp()
        candidates: list[str] = []
        for name in after - before:
            if not name.endswith(".transcript"):
                continue
            try:
                mtime = os.path.getmtime(os.path.join(path, name))
            except OSError:
                continue
            if start_ts <= mtime <= end_ts:
                candidates.append(name)
        if not candidates:
            return None
        newest = max(candidates, key=lambda n: os.path.getmtime(os.path.join(path, n)))
        return f"{path}/{newest}"

    def _write_exec_log(
        self,
        *,
        run_id: str,
        alert: Alert,
        record: RunbookRecord,
        exec_result: ExecResult,
        error: str | None = None,
    ) -> str:
        """Write exec.log to monitor-writable dir. Return the path."""
        os.makedirs(self._config.exec_log_dir, exist_ok=True)
        exec_log_path = f"{self._config.exec_log_dir}/{run_id}.exec.log"
        body = (
            f"run_id={run_id}\n"
            f"runbook_id={record.id}\n"
            f"alert_id={alert.id}\n"
            f"exit_code={exec_result.exit_code}\n"
            "--- stdout ---\n"
            f"{exec_result.stdout}\n"
            "--- stderr ---\n"
            f"{exec_result.stderr}\n"
        )
        if error is not None:
            body += f"--- error ---\n{error}\n"
        with open(exec_log_path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return exec_log_path

    async def _persist_outcome(  # noqa: PLR0913 -- keyword-only persist fields
        self,
        *,
        alert: Alert,
        record: RunbookRecord,
        run_id: str,
        exec_result: ExecResult,
        transcript_path: str | None,
        exec_log_path: str,
        host: str,
    ) -> None:
        """Persist completion + audit + (exit 0) outcome in ONE txn (Important #2)."""
        async with self._db.transaction() as conn:
            await self._runs.mark_completed_conn(
                conn,
                run_id=run_id,
                exit_code=exec_result.exit_code,
                transcript_path=transcript_path,
            )
            await insert_audit(
                conn,
                who="system:autofix",
                what="autofix.ran",
                after={
                    "runbook_id": record.id,
                    "runbook_path": record.path,
                    "alert_id": alert.id,
                    "run_id": run_id,
                    "prompt": record.path,
                    "transcript_path": transcript_path,
                    "exec_log_path": exec_log_path,
                    "exit_code": exec_result.exit_code,
                    "runbook_hash": record.content_hash,
                    "fixer_user": self._config.fixer_user,
                    "host": host,
                },
            )
            # Record the auto_fixed outcome only on a clean exit, INLINE in this txn
            # so completion + audit + outcome are atomic (Important #2). SQL idiom
            # copied from AlertRepository.insert_outcome.
            if exec_result.exit_code == 0:
                await self._insert_outcome_conn(
                    conn, alert_id=alert.id, outcome=AlertOutcome.AUTO_FIXED
                )

    async def _insert_outcome_conn(
        self, conn: AsyncConnection, *, alert_id: str, outcome: AlertOutcome
    ) -> None:
        """Inline alert_outcomes INSERT mirroring AlertRepository.insert_outcome,
        on the supplied connection (so it shares the completion txn).

        SQL and column set replicated from AlertRepository.insert_outcome exactly.
        """
        outcome_id = uuid7()
        now = utc_now_iso()
        await conn.execute(
            text(
                "INSERT INTO alert_outcomes "
                "(id, alert_id, outcome, decided_at, decided_by, created_at) "
                "VALUES (:id, :aid, :outcome, :dt, :db, :created)"
            ),
            {
                "id": outcome_id,
                "aid": alert_id,
                "outcome": outcome.value,
                "dt": now,
                "db": None,
                "created": now,
            },
        )

    async def _deny(
        self,
        *,
        alert: Alert,
        runbook_id: str | None,
        reason: DenialReason,
        detail: str,
        extra: dict[str, object] | None = None,
    ) -> RunResult:
        """Record a denial in audit and return a RunResult."""
        after: dict[str, object] = {
            "runbook_id": runbook_id,
            "alert_id": alert.id,
            "gate": reason.value,
            "detail": detail,
        }
        if extra is not None:
            after.update(extra)
        async with self._db.transaction() as conn:
            await insert_audit(
                conn,
                who="system:autofix",
                what="autofix.denied",
                after=after,
            )
        return RunResult(
            ran=False,
            outcome=RunOutcome.DENIED,
            runbook_id=runbook_id,
            run_id=None,
            exit_code=None,
            denial_reason=reason,
        )

    @asynccontextmanager
    async def _maintenance_window(
        self, _runbook: RunbookRecord, _alert: Alert
    ) -> AsyncGenerator[None, None]:
        """SEAM (EPIC-012): open/close maintenance window around the fix; currently pass-through."""
        yield
