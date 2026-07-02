"""Auto-fix orchestrator (STAGE-009-005, keystone)."""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import (
    Alert,
    AlertOutcome,
    AlertStatus,
    Severity,
)
from homelab_monitor.kernel.autofix.approvals_repository import (
    RunbookRunApprovalsRepository,
)
from homelab_monitor.kernel.autofix.feedback_parser import (
    ParsedFeedbackItem,
    parse_feedback_file,
    scan_transcript_dir_for_feedback,
)
from homelab_monitor.kernel.autofix.feedback_repository import (
    RunbookRunFeedbackRepository,
)
from homelab_monitor.kernel.autofix.matcher import matching_runbooks
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import (
    DenialReason,
    FeedbackKind,
    GrantResolutionError,
    ResolvedGrants,
    RunMode,
    RunOutcome,
    RunResult,
)
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
from homelab_monitor.kernel.runbooks.config import RunbookConfig
from homelab_monitor.kernel.runbooks.loader import RUNBOOK_CONFIG_FILENAME
from homelab_monitor.kernel.runbooks.repository import RunbookRecord, RunbookRepo
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

_TRUTHY = frozenset({"true", "1", "yes"})
_STALE_CLAIM_SLACK_SECONDS = 300  # margin past exec_timeout before a claim is treated as orphaned


def _is_truthy(value: str | None) -> bool:
    """Interpret an app_settings string flag as a bool."""
    return value is not None and value.strip().lower() in _TRUTHY


@dataclass(frozen=True, slots=True)
class DryPlan:
    """A stored dry-run plan (transcript contents)."""

    transcript_path: str
    plan_text: str
    exit_code: int | None


_KILL_UNWIND_DEADLINE_SECONDS: Final[float] = 5.0
_KILL_UNWIND_POLL_INTERVAL_SECONDS: Final[float] = 0.05


@dataclass(frozen=True, slots=True)
class _CurrentRun:
    """In-flight real-exec handle held by the orchestrator during _exec_claude."""

    run_id: str
    container: str
    started_at_monotonic: float


@dataclass(frozen=True, slots=True)
class KillResult:
    """Return value of AutoFixOrchestrator.kill_inflight().

    ``killed`` is True only when the docker kill was issued AND stamped a
    killed_at row. All non-happy outcomes surface via ``error`` (a short
    machine-readable tag) and are still audited.
    """

    killed: bool
    run_id: str | None
    reason: str
    error: str | None
    unwind_warning: str | None


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
        approvals_repo: RunbookRunApprovalsRepository,
        config: FixerRunnerConfig,
        log: BoundLogger,
        ssh_target_ids_provider: Callable[[], frozenset[str]] = frozenset,
        feedback_repo: RunbookRunFeedbackRepository | None = None,
        feedback_id_provider: Callable[[], str] | None = None,
    ) -> None:
        self._runbook_repo = runbook_repo
        self._alert_repo = alert_repo
        self._app_settings_repo = app_settings_repo
        self._secrets_repo = secrets_repo
        self._docker = docker_client
        self._db = db
        self._runs = runs_repo
        self._approvals = approvals_repo
        self._config = config
        self._log = log
        self._ssh_target_ids_provider = ssh_target_ids_provider
        self._feedback_repo = feedback_repo
        self._feedback_id_provider: Callable[[], str] = (
            feedback_id_provider if feedback_id_provider is not None else uuid7
        )
        self._locks: dict[str, asyncio.Lock] = {}
        # Process-wide lock serializing the transcript snapshot->exec->resolve
        # critical section across ALL runbooks. The per-runbook lock cannot
        # protect transcript-dir attribution because transcript_dir is a single
        # shared mount; two different runbooks executing concurrently would diff
        # the same dir and misattribute each other's transcript (Important #4).
        # Trade-off: this serializes ALL fixes process-wide. Acceptable for a
        # conservative single-user safety subsystem at this scale.
        self._transcript_lock = asyncio.Lock()
        # STAGE-009-007: in-flight real-exec handle + kill mutex. Set/cleared
        # under _kill_lock inside _exec_claude; read/snapshotted under the same
        # lock by kill_inflight. Only tracks REAL exec; dry-run plans are not
        # killable (they run inside the fixer-runner too, but the safety story
        # is that dry plans do not modify state).
        self._current_run: _CurrentRun | None = None
        self._kill_lock: asyncio.Lock = asyncio.Lock()

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

        # Phase 2: operational gates (strict order). Dry-run is NOT here.
        denial = await self._check_operational_gates(record)
        if denial is not None:
            return await self._deny(
                alert=alert,
                runbook_id=record.id,
                reason=denial,
                detail=self._gate_detail(record, denial),
            )

        # Phase 3: risky → dry-run + approval (HALT); safe → real exec.
        if record.dry_run_required:
            return await self._claim_and_store_dry(alert=alert, record=record)
        return await self._claim_and_exec(alert=alert, record=record)

    async def _check_operational_gates(self, record: RunbookRecord) -> DenialReason | None:
        """Check operational gates in strict order (kill-switch, allow-list,
        rate-limit, cooldown). Returns denial reason or None if all pass.

        The dry-run/risky decision is NOT an operational gate; handle_alert and
        execute_approved branch on record.dry_run_required themselves.
        """
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

    @staticmethod
    def _build_claude_cmd(record: RunbookRecord, *, dry: bool) -> list[str]:
        """Construct the claude argv. Dry = plan-only (NO skip-permissions)."""
        if dry:
            return ["claude", "-p", record.path, "--permission-mode", "plan"]
        return ["claude", "-p", record.path, "--dangerously-skip-permissions"]

    async def _resolve_grants(self, *, record: RunbookRecord) -> ResolvedGrants:
        """Re-read the runbook's YAML config fresh at exec-start and resolve
        its declared scoped capabilities (STAGE-009-008, Decision 1B).

        Raises:
            GrantResolutionError: config file missing/unreadable, malformed
                YAML, fails model validation, or declares an SSH target_id
                that is not present in the current known SSH target ids
                (fail-closed per Decision 4D).
        """
        config_path = Path(record.path) / RUNBOOK_CONFIG_FILENAME
        try:
            cfg = RunbookConfig.load_from_path(config_path)
        except OSError as exc:
            raise GrantResolutionError(
                reason="scoped_capabilities_unavailable",
                detail=f"runbook config unavailable at {config_path}: {exc}",
            ) from exc
        except yaml.YAMLError as exc:
            raise GrantResolutionError(
                reason="scoped_capabilities_unavailable",
                detail=f"runbook config at {config_path} is malformed YAML: {exc}",
            ) from exc
        except ValueError as exc:
            # RunbookConfig.load_from_path wraps ValidationError (and the
            # non-mapping-root case) in ValueError with file-path context.
            raise GrantResolutionError(
                reason="scoped_capabilities_unavailable",
                detail=str(exc),
            ) from exc

        scoped = cfg.scoped_capabilities

        docker_container: str | None = None
        docker_allowed_actions: tuple[str, ...] = ()
        if scoped.docker is not None:
            docker_container = scoped.docker.container
            docker_allowed_actions = tuple(scoped.docker.allowed_actions)

        ssh_target_id: str | None = None
        if scoped.ssh is not None:
            ssh_target_id = scoped.ssh.target_id
            try:
                known_ids = self._ssh_target_ids_provider()
            except Exception as exc:
                raise GrantResolutionError(
                    reason="scoped_capabilities_unavailable",
                    detail=f"ssh target registry unreadable: {exc}",
                ) from exc
            if ssh_target_id not in known_ids:
                raise GrantResolutionError(
                    reason="unknown_ssh_target_id",
                    detail=(
                        f"runbook {record.id} declares ssh target_id "
                        f"{ssh_target_id!r} which is not a known SSH target"
                    ),
                )

        return ResolvedGrants(
            docker_container=docker_container,
            docker_allowed_actions=docker_allowed_actions,
            ssh_target_id=ssh_target_id,
            egress=tuple(scoped.egress),
        )

    async def _exec_claude(
        self, *, record: RunbookRecord, alert: Alert, run_id: str, dry: bool
    ) -> tuple[ExecResult, str | None, str | None, bool, list[ParsedFeedbackItem] | None]:
        """Run claude (real or dry) under the process-wide transcript lock.

        Returns (exec_result, transcript_path, error_msg, errored,
        feedback_items). Mirrors the exec critical section of _claim_and_exec
        exactly, differing ONLY by the argv (Decision A). Persists
        grant-resolution audit rows (``autofix.grant_resolved`` /
        ``autofix.egress_unenforced`` on success; ``autofix.grant_failed`` on
        failure) BEFORE exec. Does NOT persist run completion or exec outcome
        — caller persists those. Feedback sentinel scan + parse happens HERE,
        inside self._transcript_lock, to prevent cross-run misattribution.
        """
        api_key = await self._secrets_repo.get("ANTHROPIC_API_KEY")
        env: dict[str, str] = {}
        if api_key is not None:
            env["ANTHROPIC_API_KEY"] = api_key

        transcript_dir = self._config.transcript_dir
        errored = False
        exec_result: ExecResult
        error_msg: str | None = None
        cmd = self._build_claude_cmd(record, dry=dry)
        async with self._transcript_lock:
            # STAGE-009-008: resolve scoped capability grants fresh (file-
            # authoritative) before any exec or kill-switch publish. A
            # failure here must never publish _current_run.
            try:
                grants = await self._resolve_grants(record=record)
            except GrantResolutionError as exc:
                try:
                    async with self._db.transaction() as conn:
                        await insert_audit(
                            conn,
                            who="system:autofix",
                            what="autofix.grant_failed",
                            after={
                                "runbook_id": record.id,
                                "alert_id": alert.id,
                                "run_id": run_id,
                                "reason": exc.reason,
                                "detail": exc.detail,
                            },
                        )
                except Exception:
                    self._log.exception(
                        "autofix_grant_failed_audit_failed",
                        runbook_id=record.id,
                        run_id=run_id,
                    )
                return (
                    ExecResult(exit_code=1, stdout="", stderr=""),
                    None,
                    exc.detail,
                    True,
                    None,
                )
            async with self._db.transaction() as conn:
                await insert_audit(
                    conn,
                    who="system:autofix",
                    what="autofix.grant_resolved",
                    after={
                        "runbook_id": record.id,
                        "alert_id": alert.id,
                        "run_id": run_id,
                        "docker_container": grants.docker_container,
                        "docker_allowed_actions": list(grants.docker_allowed_actions),
                        "ssh_target_id": grants.ssh_target_id,
                        "egress": list(grants.egress),
                    },
                )
                if not dry and grants.egress:
                    await insert_audit(
                        conn,
                        who="system:autofix",
                        what="autofix.egress_unenforced",
                        after={
                            "runbook_id": record.id,
                            "alert_id": alert.id,
                            "run_id": run_id,
                            "egress": list(grants.egress),
                            "detail": (
                                "egress declared but not enforced by the "
                                "fixer-runner network layer (STAGE-009-015)"
                            ),
                        },
                    )

            before = self._snapshot_dir(transcript_dir)
            exec_started = datetime.now(tz=UTC)
            # STAGE-009-007: publish the in-flight handle so kill_inflight can
            # target it. Only REAL runs are tracked (dry runs are plan-only and
            # not killable per safety-model §7.4).
            if not dry:
                async with self._kill_lock:
                    self._current_run = _CurrentRun(
                        run_id=run_id,
                        container=self._config.container,
                        started_at_monotonic=time.monotonic(),
                    )
            try:
                try:
                    async with self._maintenance_window(record, alert):
                        exec_result = await self._docker.exec_capture(
                            container_id=self._config.container,
                            cmd=cmd,
                            timeout_seconds=self._config.exec_timeout_seconds,
                            user=self._config.fixer_user,
                            env=env or None,
                        )
                except DockerSocketError as exc:
                    errored = True
                    error_msg = str(exc)
                    is_timeout = isinstance(exc, DockerExecTimeoutError)
                    exec_result = ExecResult(
                        exit_code=124 if is_timeout else 1,
                        stdout="",
                        stderr="",
                    )
            finally:
                # ALWAYS clear _current_run when the exec unwinds — success,
                # timeout, DockerSocketError, or otherwise — so kill_inflight's
                # unwind-deadline observer sees the transition to None.
                if not dry:
                    async with self._kill_lock:
                        self._current_run = None
            exec_ended = datetime.now(tz=UTC)
            transcript_path = self._resolve_transcript(
                transcript_dir, before, started=exec_started, ended=exec_ended
            )
            feedback_items: list[ParsedFeedbackItem] | None = None
            sentinel = scan_transcript_dir_for_feedback(transcript_dir, before)
            if sentinel is not None:
                feedback_items = parse_feedback_file(sentinel)
        return exec_result, transcript_path, error_msg, errored, feedback_items

    async def _claim_and_exec(
        self,
        *,
        alert: Alert,
        record: RunbookRecord,
        approving_principal: str | None = None,
    ) -> RunResult:
        """Durable claim, exec, and persist. Always returns a RunResult (ran=True or False).

        ``approving_principal`` — if set (execute_approved path), the username of
        the approving human. Threaded into the ``autofix.ran`` audit for forensic
        clarity so the ``autofix.approved by alice`` -> ``autofix.ran by
        system:autofix`` chain is not linked by ``approval_id`` alone. The
        auto-triggered ``handle_alert`` path leaves this None.
        """
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

            # --- Exec (real). Serialized process-wide for transcript-dir
            #     attribution safety (Important #4). ---
            (
                exec_result,
                transcript_path,
                error_msg,
                errored,
                feedback_items,
            ) = await self._exec_claude(record=record, alert=alert, run_id=run_id, dry=False)

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
                await self._process_feedback(
                    conn,
                    run_id=run_id,
                    alert=alert,
                    record=record,
                    feedback_items=feedback_items,
                    now_iso=utc_now_iso(),
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
                approving_principal=approving_principal,
                feedback_items=feedback_items,
            )

        return RunResult(
            ran=True,
            outcome=RunOutcome.RAN,
            runbook_id=record.id,
            run_id=run_id,
            exit_code=exec_result.exit_code,
            denial_reason=None,
        )

    async def _claim_and_store_dry(self, *, alert: Alert, record: RunbookRecord) -> RunResult:
        """Risky runbook: claim under lock, run claude PLAN-ONLY, store a
        DRY_RUN run + a PENDING approval, and HALT (no real exec, no auto_fixed).

        Models _claim_and_exec: same atomic in-lock operational re-check + claim,
        but exec is dry (Decision A) and instead of the auto_fixed outcome it
        inserts a PENDING approval (pinned to record.content_hash) in the SAME
        completion txn.
        """
        host = socket.gethostname()
        prompt = record.path
        now = datetime.now(tz=UTC)
        stale_threshold_iso = (
            now - timedelta(seconds=self._config.exec_timeout_seconds + _STALE_CLAIM_SLACK_SECONDS)
        ).isoformat()
        rate_threshold_iso = (now - timedelta(hours=1)).isoformat()

        async with self._lock_for(record.id):
            try:
                async with self._db.transaction() as conn:
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
                    run_id = await self._runs.insert_started(
                        conn,
                        runbook_id=record.id,
                        alert_id=alert.id,
                        prompt=prompt,
                        fixer_user=self._config.fixer_user,
                        host=host,
                        runbook_hash=record.content_hash,
                        mode=RunMode.DRY_RUN,
                    )
            except Exception as exc:
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

            # --- Dry exec (plan-only). ---
            (
                exec_result,
                transcript_path,
                error_msg,
                errored,
                feedback_items,
            ) = await self._exec_claude(record=record, alert=alert, run_id=run_id, dry=True)

        # Lock released. Persist completion + exec.log + PENDING approval + audit.
        exec_log_path = self._write_exec_log(
            run_id=run_id,
            alert=alert,
            record=record,
            exec_result=exec_result,
            error=error_msg,
        )

        approval_id: str | None = None
        async with self._db.transaction() as conn:
            await self._runs.mark_completed_conn(
                conn,
                run_id=run_id,
                exit_code=exec_result.exit_code,
                transcript_path=transcript_path,
            )
            if errored:
                # Dry exec failed: record error audit, NO approval (nothing to approve).
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
                        "mode": RunMode.DRY_RUN.value,
                    },
                )
            else:
                # Dry exec succeeded: create PENDING approval + audit, ONE txn.
                approval_id = await self._approvals.insert_pending(
                    conn,
                    dry_run_id=run_id,
                    runbook_id=record.id,
                    alert_id=alert.id,
                    pinned_runbook_hash=record.content_hash,
                )
                await insert_audit(
                    conn,
                    who="system:autofix",
                    what="autofix.dry_run_stored",
                    after={
                        "runbook_id": record.id,
                        "runbook_path": record.path,
                        "alert_id": alert.id,
                        "run_id": run_id,
                        "approval_id": approval_id,
                        "transcript_path": transcript_path,
                        "exec_log_path": exec_log_path,
                        "exit_code": exec_result.exit_code,
                        "runbook_hash": record.content_hash,
                        "host": host,
                    },
                )
            await self._process_feedback(
                conn,
                run_id=run_id,
                alert=alert,
                record=record,
                feedback_items=feedback_items,
                now_iso=utc_now_iso(),
            )

        # A dry run NEVER writes alert_outcomes('auto_fixed'): a plan fixed nothing.
        return RunResult(
            ran=True,
            outcome=RunOutcome.DRY_RUN_STORED,
            runbook_id=record.id,
            run_id=run_id,
            exit_code=exec_result.exit_code,
            denial_reason=None,
            approval_id=approval_id,
        )

    async def execute_approved(
        self, approval_id: str, *, principal: str, ip: str | None
    ) -> RunResult:
        """Execute the REAL run for a previously-approved dry-run plan.

        Authoritative sequence (each failure returns a denial RunResult, NO exec):
          1. load approval; if status != 'pending' -> APPROVAL_NOT_PENDING.
          2. load CURRENT runbook; drift (content_hash != pinned) -> reject + RUNBOOK_CHANGED.
          3. re-run operational gates -> if deny, audit + denial RunResult.
          4. mark approved (+ audit) in-txn, then REUSE _claim_and_exec (REAL),
             then set real_run_id on the approval.
        """
        approval = await self._approvals.get(approval_id)
        if approval is None or approval.status != "pending":
            reason = DenialReason.APPROVAL_NOT_PENDING
            async with self._db.transaction() as conn:
                await insert_audit(
                    conn,
                    who=principal,
                    what="autofix.denied",
                    after={
                        "approval_id": approval_id,
                        "gate": reason.value,
                        "detail": "approval missing or not pending",
                    },
                    ip=ip,
                )
            return RunResult(
                ran=False,
                outcome=RunOutcome.DENIED,
                runbook_id=None if approval is None else approval.runbook_id,
                run_id=None,
                exit_code=None,
                denial_reason=reason,
                approval_id=approval_id,
            )

        record = await self._runbook_repo.get_runbook(approval.runbook_id)
        if record is None:
            # Runbook deleted between plan and approve. Distinct denial from a
            # hash-mutation drift: forensic auditors need to tell "someone
            # deleted the runbook" from "someone edited the runbook".
            reason = DenialReason.RUNBOOK_MISSING
            async with self._db.transaction() as conn:
                await self._approvals.mark_rejected_conn(
                    conn,
                    approval_id=approval_id,
                    approved_by=principal,
                    when=utc_now_iso(),
                )
                await insert_audit(
                    conn,
                    who=principal,
                    what="autofix.rejected",
                    after={
                        "approval_id": approval_id,
                        "runbook_id": approval.runbook_id,
                        "gate": reason.value,
                        "pinned_runbook_hash": approval.pinned_runbook_hash,
                        "runbook_deleted": True,
                    },
                    ip=ip,
                )
            return RunResult(
                ran=False,
                outcome=RunOutcome.DENIED,
                runbook_id=approval.runbook_id,
                run_id=None,
                exit_code=None,
                denial_reason=reason,
                approval_id=approval_id,
            )
        if record.content_hash != approval.pinned_runbook_hash:
            reason = DenialReason.RUNBOOK_CHANGED
            async with self._db.transaction() as conn:
                await self._approvals.mark_rejected_conn(
                    conn,
                    approval_id=approval_id,
                    approved_by=principal,
                    when=utc_now_iso(),
                )
                await insert_audit(
                    conn,
                    who=principal,
                    what="autofix.rejected",
                    after={
                        "approval_id": approval_id,
                        "runbook_id": approval.runbook_id,
                        "gate": reason.value,
                        "pinned_runbook_hash": approval.pinned_runbook_hash,
                        "current_runbook_hash": record.content_hash,
                    },
                    ip=ip,
                )
            return RunResult(
                ran=False,
                outcome=RunOutcome.DENIED,
                runbook_id=approval.runbook_id,
                run_id=None,
                exit_code=None,
                denial_reason=reason,
                approval_id=approval_id,
            )

        op_denial = await self._check_operational_gates(record)
        if op_denial is not None:
            async with self._db.transaction() as conn:
                await insert_audit(
                    conn,
                    who=principal,
                    what="autofix.denied",
                    after={
                        "approval_id": approval_id,
                        "runbook_id": record.id,
                        "gate": op_denial.value,
                        "detail": self._gate_detail(record, op_denial),
                    },
                    ip=ip,
                )
            return RunResult(
                ran=False,
                outcome=RunOutcome.DENIED,
                runbook_id=record.id,
                run_id=None,
                exit_code=None,
                denial_reason=op_denial,
                approval_id=approval_id,
            )

        # Approve + audit in one txn BEFORE the real exec. The UPDATE has a
        # `AND status = 'pending'` guard: if a concurrent caller already decided
        # this approval, rowcount will be 0 and we must NOT exec (Fix I1 race
        # safety net; the earlier read-based pre-check is still useful for the
        # common case but is not race-safe on its own).
        async with self._db.transaction() as conn:
            approve_rowcount = await self._approvals.mark_approved_conn(
                conn,
                approval_id=approval_id,
                approved_by=principal,
                when=utc_now_iso(),
            )
            if approve_rowcount == 0:
                # Someone else won the race between our pre-check read and this
                # UPDATE. Audit a denial in the SAME txn and return DENIED, do
                # NOT exec.
                reason = DenialReason.APPROVAL_NOT_PENDING
                await insert_audit(
                    conn,
                    who=principal,
                    what="autofix.denied",
                    after={
                        "approval_id": approval_id,
                        "runbook_id": record.id,
                        "gate": "approval_not_pending",
                        "detail": "approval was decided by another caller (race)",
                    },
                    ip=ip,
                )
                return RunResult(
                    ran=False,
                    outcome=RunOutcome.DENIED,
                    runbook_id=record.id,
                    run_id=None,
                    exit_code=None,
                    denial_reason=reason,
                    approval_id=approval_id,
                )
            await insert_audit(
                conn,
                who=principal,
                what="autofix.approved",
                after={
                    "approval_id": approval_id,
                    "runbook_id": record.id,
                    "dry_run_id": approval.dry_run_id,
                    "runbook_hash": record.content_hash,
                },
                ip=ip,
            )

        # Load the alert the dry run recorded (exec path uses only alert.id).
        alert = await self._load_alert_for_exec(approval.alert_id)

        # REUSE the shared real-exec path (mode=REAL). No duplicated exec/persist.
        # Thread the approving principal so the autofix.ran audit records who
        # approved this human-approved run (Fix M1 — forensic clarity so the
        # `autofix.approved by <alice>` -> `autofix.ran by system:autofix` chain
        # is linked by more than approval_id alone).
        result = await self._claim_and_exec(
            alert=alert, record=record, approving_principal=principal
        )

        # Pin the resulting real run to the approval (if it actually ran/claimed).
        if result.run_id is not None:
            async with self._db.transaction() as conn:
                await self._approvals.set_real_run_id_conn(
                    conn, approval_id=approval_id, real_run_id=result.run_id
                )
        else:
            # Fix I2: real claim denied AFTER we already marked the approval
            # approved. Without a revert, the approval would be stuck in status
            # 'approved' forever with real_run_id NULL — an orphaned record the
            # user cannot retry. Revert to pending in a new txn so the user can
            # re-approve later. If the revert loses a race (rowcount 0), just
            # log a warning: something else already changed state, we let it be.
            denial_value = (
                result.denial_reason.value if result.denial_reason is not None else "unknown"
            )
            async with self._db.transaction() as conn:
                revert_rowcount = await self._approvals.revert_to_pending_conn(
                    conn, approval_id=approval_id
                )
                if revert_rowcount == 0:
                    self._log.warning(
                        "autofix_approval_revert_lost_race",
                        approval_id=approval_id,
                        denial_reason=denial_value,
                    )
                else:
                    await insert_audit(
                        conn,
                        who=principal,
                        what="autofix.approval_reverted",
                        after={
                            "approval_id": approval_id,
                            "reason": "claim_denied",
                            "denial_reason": denial_value,
                        },
                        ip=ip,
                    )

        return RunResult(
            ran=result.ran,
            outcome=result.outcome,
            runbook_id=result.runbook_id,
            run_id=result.run_id,
            exit_code=result.exit_code,
            denial_reason=result.denial_reason,
            approval_id=approval_id,
        )

    async def kill_inflight(
        self,
        *,
        reason: str,
        killed_by: str,
        ip: str | None = None,
    ) -> KillResult:
        """Kill the currently in-flight REAL run (if any) via SIGKILL to the
        fixer-runner container.

        Contract:
          - No in-flight run -> audit ``autofix.kill_no_inflight`` (fresh txn),
            return KillResult(killed=False, error="no_inflight_run").
          - Handle transitions to None between the initial snapshot and the
            actual kill (natural exec exit races us) -> same as no-inflight but
            error="exec_completed_before_kill".
          - docker.kill_container raises -> audit ``autofix.kill_failed`` in a
            fresh txn and RE-RAISE (router surfaces 502).
          - Success -> audit ``autofix.killed`` + stamp runbook_runs.killed_at
            in the SAME txn. Then observe self._current_run returning to None
            for up to _KILL_UNWIND_DEADLINE_SECONDS. On timeout, set
            unwind_warning="unwind_deadline_exceeded" on the result but keep
            killed=True.
        """
        # Snapshot under lock.
        async with self._kill_lock:
            snapshot = self._current_run

        if snapshot is None:
            # No in-flight run to kill. Still audit for the compliance trail.
            async with self._db.transaction() as conn:
                await insert_audit(
                    conn,
                    who=killed_by,
                    what="autofix.kill_no_inflight",
                    after={"reason": reason},
                    ip=ip,
                )
            return KillResult(
                killed=False,
                run_id=None,
                reason=reason,
                error="no_inflight_run",
                unwind_warning=None,
            )

        run_id_snapshot = snapshot.run_id
        container_snapshot = snapshot.container

        # Pre-kill audit in a FRESH txn (Important #3): commits BEFORE the
        # SIGKILL fires so a crash between the docker kill and the killed_at
        # stamp still leaves a forensic trail that a kill was attempted.
        async with self._db.transaction() as conn:
            await insert_audit(
                conn,
                who=killed_by,
                what="autofix.kill_attempted",
                after={
                    "run_id": run_id_snapshot,
                    "reason": reason,
                    "killed_by": killed_by,
                    "container": container_snapshot,
                },
                ip=ip,
            )

        # Issue the kill. Any DockerSocketError is audited + re-raised so the
        # caller (toggle endpoint) can return 502. Panic-path timeout is
        # deliberately short (5s): a stuck docker socket should not hold the
        # kill-switch endpoint for the default 30s write-timeout (Minor #3).
        try:
            await self._docker.kill_container(
                container_snapshot, signal="SIGKILL", timeout_seconds=5.0
            )
        except DockerSocketError as exc:
            async with self._db.transaction() as conn:
                await insert_audit(
                    conn,
                    who=killed_by,
                    what="autofix.kill_failed",
                    after={
                        "run_id": run_id_snapshot,
                        "reason": reason,
                        "error": str(exc),
                        "container": container_snapshot,
                    },
                    ip=ip,
                )
            raise

        # Between the snapshot and the successful kill, the exec may have
        # completed naturally AND a concurrent execute_approved may have
        # republished _current_run with a DIFFERENT run_id. Re-verify under
        # lock: if the current run_id differs from our snapshot, we killed a
        # run that has already unwound and a fresh run has taken its place.
        # Audit autofix.kill_wrong_run_race and DO NOT stamp killed_at on the
        # snapshot (we cannot vouch that our SIGKILL affected either run
        # deterministically — the safe move is to record the race and let the
        # operator retry the kill against the fresh run).
        async with self._kill_lock:
            post_snapshot = self._current_run
            wrong_run_race = post_snapshot is not None and post_snapshot.run_id != run_id_snapshot

        if wrong_run_race:
            async with self._db.transaction() as conn:
                await insert_audit(
                    conn,
                    who=killed_by,
                    what="autofix.kill_wrong_run_race",
                    after={
                        "snapshot_run_id": run_id_snapshot,
                        "current_run_id": (
                            post_snapshot.run_id if post_snapshot is not None else None
                        ),
                        "reason": reason,
                        "killed_by": killed_by,
                        "container": container_snapshot,
                    },
                    ip=ip,
                )
            return KillResult(
                killed=False,
                run_id=run_id_snapshot,
                reason=reason,
                error="wrong_run_race",
                unwind_warning=None,
            )

        # Stamp killed_at + audit success in ONE txn. Idempotency gate on
        # killed_at IS NULL lives in the SQL (Minor: _UPDATE_KILLED_SQL).
        killed_at_iso = utc_now_iso()
        async with self._db.transaction() as conn:
            await self._runs.mark_killed_conn(
                conn,
                run_id=run_id_snapshot,
                killed_at=killed_at_iso,
            )
            await insert_audit(
                conn,
                who=killed_by,
                what="autofix.killed",
                after={
                    "run_id": run_id_snapshot,
                    "reason": reason,
                    "killed_by": killed_by,
                    "container": container_snapshot,
                },
                ip=ip,
            )

        # Wait up to _KILL_UNWIND_DEADLINE_SECONDS for _exec_claude's finally
        # block to clear _current_run (evidence the exec actually unwound).
        unwind_warning: str | None = None
        deadline = time.monotonic() + _KILL_UNWIND_DEADLINE_SECONDS
        # If the exec already unwound (post_snapshot is None) we can skip the poll.
        if post_snapshot is not None:
            while time.monotonic() < deadline:
                async with self._kill_lock:
                    cleared = self._current_run is None
                if cleared:
                    break
                await asyncio.sleep(_KILL_UNWIND_POLL_INTERVAL_SECONDS)
            else:
                unwind_warning = "unwind_deadline_exceeded"

        return KillResult(
            killed=True,
            run_id=run_id_snapshot,
            reason=reason,
            error=None,
            unwind_warning=unwind_warning,
        )

    async def _load_alert_for_exec(self, alert_id: str | None) -> Alert:
        """Load the Alert for the real exec. The exec/persist path reads only
        alert.id, so a minimal Alert is sufficient if the row is gone/absent.
        """
        if alert_id is not None:
            loaded = await self._alert_repo.get_alert_by_id(alert_id)
            if loaded is not None:
                return loaded
        # Minimal placeholder: exec/persist only reads .id. Use empty/neutral
        # values for the other required Alert fields.
        placeholder_id = alert_id if alert_id is not None else "unknown"
        now = utc_now_iso()
        return Alert(
            id=placeholder_id,
            fingerprint=f"reconstructed-{placeholder_id}",
            source_tool="autofix-approval",
            severity=Severity.WARNING,
            status=AlertStatus.FIRING,
            opened_at=now,
            last_seen_at=now,
            payload={},
            labels={},
            annotations={},
        )

    async def read_dry_plan(self, dry_run_id: str) -> DryPlan | None:
        """Load a dry run's stored plan transcript. None if the run is missing,
        has no transcript_path, or the file is unreadable.
        """
        row = await self._runs.get(dry_run_id)
        if row is None:
            return None
        transcript_path = row.transcript_path
        if transcript_path is None:
            return None
        path_str = str(transcript_path)

        def _read(p: str) -> str:
            with open(p, encoding="utf-8") as fh:
                return fh.read()

        try:
            plan_text = await asyncio.to_thread(_read, path_str)
        except OSError:
            return None
        exit_code = None if row.exit_code is None else int(row.exit_code)
        return DryPlan(
            transcript_path=path_str,
            plan_text=plan_text,
            exit_code=exit_code,
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
        feedback_items: list[ParsedFeedbackItem] | None,
        approving_principal: str | None = None,
    ) -> None:
        """Persist completion + audit + (exit 0) outcome in ONE txn (Important #2).

        ``approving_principal`` is added to the ``autofix.ran`` after_json ONLY
        when non-None (human-approved runs). Auto-triggered runs omit the key.
        """
        async with self._db.transaction() as conn:
            await self._runs.mark_completed_conn(
                conn,
                run_id=run_id,
                exit_code=exec_result.exit_code,
                transcript_path=transcript_path,
            )
            after_json: dict[str, object] = {
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
            }
            if approving_principal is not None:
                after_json["approving_principal"] = approving_principal
            await insert_audit(
                conn,
                who="system:autofix",
                what="autofix.ran",
                after=after_json,
            )
            # Record the auto_fixed outcome only on a clean exit, INLINE in this txn
            # so completion + audit + outcome are atomic (Important #2). SQL idiom
            # copied from AlertRepository.insert_outcome. Deliberately BEFORE
            # _process_feedback: if feedback persistence raises and is swallowed,
            # the txn's SQLAlchemy connection can be left needing a rollback — any
            # subsequent await (e.g. this insert) would then raise
            # PendingRollbackError and revert mark_completed_conn + autofix.ran too.
            # Running the primary audit trail first means it survives even if
            # feedback (best-effort telemetry) fails.
            if exec_result.exit_code == 0:
                await self._insert_outcome_conn(
                    conn, alert_id=alert.id, outcome=AlertOutcome.AUTO_FIXED
                )
            await self._process_feedback(
                conn,
                run_id=run_id,
                alert=alert,
                record=record,
                feedback_items=feedback_items,
                now_iso=utc_now_iso(),
            )

    async def _process_feedback(  # noqa: PLR0913 -- keyword-only feedback context
        self,
        conn: AsyncConnection,
        *,
        run_id: str,
        alert: Alert,
        record: RunbookRecord,
        feedback_items: list[ParsedFeedbackItem] | None,
        now_iso: str,
    ) -> None:
        """Persist already-parsed feedback items (scan/parse happened in
        ``_exec_claude``, inside ``self._transcript_lock``, to prevent
        cross-run misattribution — see feedback_parser.py docstring).

        No-op if ``self._feedback_repo`` is None (production wiring will
        pass one; unit tests may omit it) or ``feedback_items`` is None/empty.
        Runs inside the caller's txn so completion + audit + feedback rows
        land atomically. Any exception from persist is logged and swallowed —
        feedback must never fail the parent txn (Decision 3D: feedback is
        best-effort telemetry).
        """
        if self._feedback_repo is None or not feedback_items:
            return
        try:
            for item in feedback_items:
                await self._feedback_repo.insert_conn(
                    conn,
                    runbook_run_id=run_id,
                    item=item,
                    feedback_id=self._feedback_id_provider(),
                    created_at=now_iso,
                )
                if item.kind is FeedbackKind.PARSE_ERROR:
                    await insert_audit(
                        conn,
                        who="system:autofix",
                        what="autofix.feedback_parse_error",
                        after={
                            "runbook_id": record.id,
                            "alert_id": alert.id,
                            "run_id": run_id,
                            "detail": item.suggestion_text[:512],
                        },
                    )
        except Exception:
            self._log.exception(
                "autofix_feedback_processing_failed",
                run_id=run_id,
                runbook_id=record.id,
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
