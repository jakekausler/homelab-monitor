"""Transcript-file rotator for the auto-fix subsystem (STAGE-009-012).

Enforces §10.2 of the design spec: keep the last N transcripts per runbook
(default 100; configurable) and delete any transcript older than the
configured max age (default 365 days).

The monitor CANNOT delete transcript files directly — its
``/data/runbook-transcripts`` mount is ``:ro`` (STAGE-009-002 non-negotiable
#4, four independent barriers). The rotator therefore delegates FILE
deletion to the fixer-runner container via ``docker exec rm``, matching the
same identity split the orchestrator uses: monitor SCHEDULES, fixer-runner
(as ``homelab-fixer``, the low-priv user with the RW mount) EXECUTES.

The rotator NEVER deletes ``runbook_runs`` or ``audit_log`` rows
(non-negotiable #4 — audit immutability). It only unlinks transcript FILES
and writes the ``transcript_pruned_at`` marker column + a
``autofix.transcript_pruned`` audit-log row per successful prune.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.config import FixerRunnerConfig
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.socket_client import (
    DockerSocketClient,
    DockerSocketConnectionError,
    DockerSocketProtocolError,
)

PruneReason = Literal["count_limit", "age_limit", "both"]


@dataclass(frozen=True, slots=True)
class RotationOutcome:
    """Result of one rotation pass.

    ``files_pruned`` — number of transcript FILES successfully unlinked +
    marked. ``runs_marked`` is equal to ``files_pruned`` (each unlink is
    followed by exactly one marker write; if the marker write fails the
    unlink is not counted).

    ``runbooks_scanned`` — the number of DISTINCT ``runbook_id``s present in
    ``runbook_runs`` at the moment the rotator sampled the baseline. This is
    a "population" figure, NOT a per-pass work counter: it counts runbooks
    that have EVER produced a run (whether or not any of their transcripts
    were candidates this pass), because that is what makes it a stable
    denominator for the operator-visible "did we look at anything?" sanity
    check. A runbook with all transcripts already pruned still contributes.

    ``skipped_reason`` is non-None when the pass was aborted (e.g.,
    fixer-runner container not running).
    """

    files_pruned: int
    runs_marked: int
    runbooks_scanned: int
    skipped_reason: str | None


class TranscriptRotator:
    """Deletes stale/beyond-N transcript FILES via fixer-runner docker-exec.

    Idempotent: re-running against an already-tidy state is a no-op.
    Never touches ``runbook_runs`` or ``audit_log`` rows.
    """

    def __init__(
        self,
        *,
        db: SqliteRepository,
        runs_repo: RunbookRunsRepository,
        config: FixerRunnerConfig,
        docker_client: DockerSocketClient,
        log: BoundLogger,
    ) -> None:
        self._db = db
        self._runs = runs_repo
        self._config = config
        self._docker = docker_client
        self._log = log
        # Serialize rotation passes: the daily scheduler and the manual
        # /rotate-transcripts endpoint can race; without this lock both
        # would race the same candidate rows and write duplicate audit
        # entries.
        self._lock = asyncio.Lock()

    async def rotate(self) -> RotationOutcome:
        """Execute one rotation pass. See module docstring for behavior.

        Serialized by ``self._lock`` so the scheduled daily pass and a
        manual ``/rotate-transcripts`` call cannot double-process the same
        candidates.
        """
        async with self._lock:
            return await self._rotate_locked()

    async def _rotate_locked(self) -> RotationOutcome:
        # 1. Fixer-runner running? If not, emit skipped audit + return.
        container_id = await self._resolve_running_fixer_container_id()
        if container_id is None:
            reason = "fixer_runner_not_running"
            async with self._db.transaction() as conn:
                await insert_audit(
                    conn,
                    who="system:autofix",
                    what="autofix.transcript_rotation_skipped_fixer_disabled",
                    after={"fixer_container": self._config.container},
                )
            self._log.warning(
                "autofix.transcript_rotation.skipped",
                reason=reason,
                fixer_container=self._config.container,
            )
            return RotationOutcome(
                files_pruned=0,
                runs_marked=0,
                runbooks_scanned=0,
                skipped_reason=reason,
            )

        # 2. Compute the age cutoff. ``older_than_iso`` is the ISO threshold
        # for the AGE-limit rule; runs with ``started_at`` strictly older are
        # candidates regardless of N.
        older_than_iso = self._compute_older_than_iso()

        # 3. Enumerate candidates + baseline runbook count.
        runbooks_scanned = await self._runs.count_distinct_runbooks_with_runs()
        candidates = await self._runs.list_prune_candidates(
            keep_last_n=self._config.transcript_rotation_max_count,
            older_than_iso=older_than_iso,
        )

        # 4. Delete each candidate file via fixer-runner exec; on success,
        # write the marker + audit row inside ONE txn.
        base_dir = Path(self._config.transcript_dir).resolve()
        files_pruned = 0
        runs_marked = 0
        for run_id, runbook_id, transcript_path, count_exceeded, age_exceeded in candidates:
            if not self._is_within_base_dir(transcript_path, base_dir):
                # Defense in depth: skip any path that escapes the configured
                # base. Log once per bad row; keep going.
                self._log.warning(
                    "autofix.transcript_rotation.path_outside_base",
                    run_id=run_id,
                    runbook_id=runbook_id,
                    transcript_path=transcript_path,
                    base_dir=str(base_dir),
                )
                continue

            deleted = await self._delete_transcript_file(
                container_id=container_id, transcript_path=transcript_path
            )
            if not deleted:
                # Failure already logged in _delete_transcript_file; skip.
                continue

            reason = self._classify_reason(
                count_exceeded=count_exceeded,
                age_exceeded=age_exceeded,
            )
            pruned_at = utc_now_iso()
            async with self._db.transaction() as conn:
                await self._runs.mark_transcript_pruned_conn(
                    conn, run_id=run_id, pruned_at=pruned_at
                )
                await insert_audit(
                    conn,
                    who="system:autofix",
                    what="autofix.transcript_pruned",
                    after={
                        "run_id": run_id,
                        "runbook_id": runbook_id,
                        "transcript_path": transcript_path,
                        "reason": reason,
                    },
                    when=pruned_at,
                )
            files_pruned += 1
            runs_marked += 1

        self._log.info(
            "autofix.transcript_rotation.done",
            files_pruned=files_pruned,
            runs_marked=runs_marked,
            runbooks_scanned=runbooks_scanned,
        )
        return RotationOutcome(
            files_pruned=files_pruned,
            runs_marked=runs_marked,
            runbooks_scanned=runbooks_scanned,
            skipped_reason=None,
        )

    # -- helpers --------------------------------------------------------

    async def _resolve_running_fixer_container_id(self) -> str | None:
        """Return the container Id of the fixer-runner IF it exists AND is running.

        Returns None (no exception) when the container is absent, exited, or
        the docker socket is unreachable — rotation is best-effort housekeeping
        and MUST NOT crash the periodic loop.
        """
        target_name = self._config.container
        try:
            entries = await self._docker.list_containers()
        except (DockerSocketConnectionError, DockerSocketProtocolError) as exc:
            self._log.warning(
                "autofix.transcript_rotation.docker_unreachable",
                error=str(exc),
            )
            return None
        for entry in entries:
            names = entry.get("Names") or []
            # Docker returns names as "/homelab-fixer-runner"; match either form.
            if any(n.lstrip("/") == target_name for n in names):
                if entry.get("State") == "running":
                    return entry["Id"]
                return None
        return None

    def _compute_older_than_iso(self) -> str:
        """Return the ISO UTC cutoff below which a transcript is age-eligible."""
        cutoff = _dt.datetime.now(_dt.UTC) - _dt.timedelta(
            days=self._config.transcript_rotation_max_age_days
        )
        return cutoff.isoformat().replace("+00:00", "Z")

    def _is_within_base_dir(self, transcript_path: str, base_dir: Path) -> bool:
        """True iff ``transcript_path`` resolves inside ``base_dir``.

        Uses ``Path.is_relative_to`` on the RESOLVED path. NEVER touches the
        filesystem for existence (the container's FS, not the monitor's,
        holds the file — the monitor's ``resolve()`` is a syntactic check).

        Rejects RELATIVE paths outright: DB rows are expected to hold
        absolute paths (the writer stores absolute paths); a relative row
        signals corruption or a bug and must not be silently rescued by
        joining against the monitor's cwd or ``base_dir``.
        """
        try:
            p = Path(transcript_path)
            if not p.is_absolute():
                return False
            return p.resolve().is_relative_to(base_dir)
        except (OSError, ValueError):  # pragma: no cover -- defensive
            return False

    async def _delete_transcript_file(self, *, container_id: str, transcript_path: str) -> bool:
        """docker-exec ``rm -f -- <path>`` inside the fixer-runner as
        ``fixer_user``.

        Returns True on exit code 0; False (with a WARN log) otherwise.

        Error handling: catches ``DockerSocketConnectionError`` and
        ``DockerSocketProtocolError`` from ``exec_capture`` and returns
        False. ``DockerExecTimeoutError`` is a subclass of
        ``DockerSocketConnectionError`` (see ``docker/socket_client.py``),
        so exec-timeout is covered by the ``DockerSocketConnectionError``
        arm without needing a separate ``except``.
        """
        try:
            result = await self._docker.exec_capture(
                container_id=container_id,
                cmd=["rm", "-f", "--", transcript_path],
                user=self._config.fixer_user,
                timeout_seconds=15.0,
            )
        except (DockerSocketConnectionError, DockerSocketProtocolError) as exc:
            self._log.warning(
                "autofix.transcript_rotation.exec_error",
                transcript_path=transcript_path,
                error=str(exc),
            )
            return False
        if result.exit_code != 0:
            self._log.warning(
                "autofix.transcript_rotation.rm_failed",
                transcript_path=transcript_path,
                exit_code=result.exit_code,
                stderr=result.stderr[:200],
            )
            return False
        return True

    def _classify_reason(
        self,
        *,
        count_exceeded: bool,
        age_exceeded: bool,
    ) -> PruneReason:
        """Classify a prune reason for the audit row.

        Precondition: at least one of ``count_exceeded`` / ``age_exceeded`` is
        True (candidates are only produced by ``list_prune_candidates`` when
        one of the two rules picked them). The rotator passes the two booleans
        it received from that method's return tuple.

        Returns:
          - ``"both"`` when both count and age triggered.
          - ``"age_limit"`` when only age triggered.
          - ``"count_limit"`` when only count triggered.
        """
        if count_exceeded and age_exceeded:
            return "both"
        if age_exceeded:
            return "age_limit"
        return "count_limit"


__all__ = ["RotationOutcome", "TranscriptRotator"]
