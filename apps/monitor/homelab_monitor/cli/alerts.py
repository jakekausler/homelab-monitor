"""``hm alerts`` subcommand — alert-table operations (STAGE-010-001)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import structlog
from sqlalchemy import text
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.alerts.types import AlertOutcome
from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.logging import configure_logging
from homelab_monitor.kernel.vmalert.reload import VmalertReloader

_EXPECTED_CONFIRM_PHRASE = "delete signature-silent noise"
_WHO = "cli:stage-010-001-purge"
_WHAT = "alerts_bulk_purged"
_SOURCE_TOOL = "vmalert-metrics"
_ALERTGROUP = "signature_silent"

_EXPECTED_BACKFILL_CONFIRM_PHRASE = "backfill outcomes from historical alert state"
_BACKFILL_WHO = "cli:hm-alerts-backfill-outcomes"
_BACKFILL_WHAT = "alert_outcomes_backfilled"
_BACKFILL_DECIDED_BY = "backfill"
_BACKFILL_PROGRESS_INTERVAL = 10_000

_COUNT_SQL = text(
    "SELECT COUNT(*) FROM alerts "
    "WHERE json_extract(payload_json, '$.labels.alertname') = :alertname"
)
_DELETE_SQL = text(
    "DELETE FROM alerts WHERE json_extract(payload_json, '$.labels.alertname') = :alertname"
)

_BACKFILL_GUARD_SQL = text(
    "SELECT EXISTS(SELECT 1 FROM alert_outcomes WHERE decided_by = :decided_by LIMIT 1)"
)
_COUNT_ACKED_SQL = text("SELECT COUNT(*) FROM alerts WHERE ack_at IS NOT NULL")
_COUNT_AUTO_RESOLVED_SQL = text(
    "SELECT COUNT(*) FROM alerts WHERE resolved_at IS NOT NULL AND ack_at IS NULL"
)
_SELECT_ACKED_SQL = text("SELECT id, ack_at FROM alerts WHERE ack_at IS NOT NULL")
_SELECT_AUTO_RESOLVED_SQL = text(
    "SELECT id, resolved_at FROM alerts WHERE resolved_at IS NOT NULL AND ack_at IS NULL"
)
_INSERT_OUTCOME_SQL = text(
    "INSERT INTO alert_outcomes "
    "(id, alert_id, outcome, decided_at, decided_by, created_at) "
    "VALUES (:id, :aid, :outcome, :dt, :db, :created) "
    "ON CONFLICT (alert_id, outcome) DO NOTHING"
)


def add_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    alerts = subparsers.add_parser("alerts", help="Alert operations")
    sub = alerts.add_subparsers(dest="alerts_cmd")
    p_purge = sub.add_parser(
        "purge",
        help="Bulk-delete alerts by alertname (audit-logged, irreversible)",
    )
    p_purge.add_argument(
        "--alertname",
        required=True,
        help="alertname label value to match (payload_json.labels.alertname)",
    )
    p_purge.add_argument(
        "--dry-run",
        action="store_true",
        help="Report matching row count without deleting or writing audit",
    )
    p_purge.add_argument(
        "--confirm-phrase",
        default=None,
        help=f"Required for real run; must equal '{_EXPECTED_CONFIRM_PHRASE}'",
    )
    p_purge.set_defaults(func=_handle)

    p_backfill = sub.add_parser(
        "backfill-outcomes",
        help=(
            "One-shot backfill of alert_outcomes from existing alerts.ack_at / "
            "resolved_at column state (audit-logged, idempotent, guarded against re-run)"
        ),
    )
    p_backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts that WOULD be inserted without writing to the DB",
    )
    p_backfill.add_argument(
        "--confirm-phrase",
        default=None,
        help=(f"Required for real run; must equal '{_EXPECTED_BACKFILL_CONFIRM_PHRASE}'"),
    )
    p_backfill.set_defaults(func=_handle)

    alerts.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    sub = getattr(args, "alerts_cmd", None)
    if sub == "purge":
        return asyncio.run(_cmd_purge(args))
    if sub == "backfill-outcomes":
        return asyncio.run(_cmd_backfill_outcomes(args))
    print("usage: hm alerts {purge,backfill-outcomes}", file=sys.stderr)
    return 2


async def _cmd_purge(args: argparse.Namespace) -> int:
    """Bulk-purge alerts by alertname; idempotent (a 2nd run writes a row_count=0 audit row).

    Safety model (auto-fix parity): mode is dry-run OR real-run + case-fold confirm-phrase;
    every real run writes exactly one audit_log row (including zero-match runs) so the
    action is fully auditable and re-runnable without special-casing.
    """
    configure_logging()
    log: BoundLogger = structlog.get_logger()  # pyright: ignore[reportAssignmentType]

    alertname: str = args.alertname
    dry_run: bool = bool(getattr(args, "dry_run", False))
    confirm_phrase: str | None = getattr(args, "confirm_phrase", None)

    # 0) Reject empty / whitespace-only alertname (argparse's required=True accepts "").
    if not alertname or not alertname.strip():
        print("Error: --alertname must be a non-empty string", file=sys.stderr)
        return 1

    # 1) Mode validation — must pick dry-run OR real-run.
    if not dry_run and confirm_phrase is None:
        print(
            "Error: --dry-run or --confirm-phrase required",
            file=sys.stderr,
        )
        return 1

    # 2) Real-run: verify confirm-phrase (case-fold match, mirrors PhraseMatchMode.CASE_FOLD).
    if not dry_run:
        assert confirm_phrase is not None  # narrowed by branch above
        got = confirm_phrase.strip().casefold()
        expected = _EXPECTED_CONFIRM_PHRASE.strip().casefold()
        if got != expected:
            print(
                f"Error: confirm_phrase must equal '{_EXPECTED_CONFIRM_PHRASE}'",
                file=sys.stderr,
            )
            return 1

    # 3) Open engine + repo.
    engine = get_engine()
    repo = SqliteRepository(engine)

    # 4) Count + (real-run) audit + delete, all in one transaction.
    async with repo.transaction() as conn:
        count_result = await conn.execute(_COUNT_SQL, {"alertname": alertname})
        count = int(count_result.scalar_one())

        if dry_run:
            print(f"Dry run: {count} alerts with alertname={alertname} would be deleted.")
            return 0

        await insert_audit(
            conn,
            who=_WHO,
            what=_WHAT,
            before={
                "alertname": alertname,
                "row_count": count,
                "source_tool": _SOURCE_TOOL,
                "alertgroup": _ALERTGROUP,
            },
            after=None,
        )
        delete_result = await conn.execute(_DELETE_SQL, {"alertname": alertname})
        deleted = int(delete_result.rowcount or 0)

    print(f"Purged {deleted} alerts with alertname={alertname}. audit_log row written.")
    log.info(
        "alerts.purge.committed",
        alertname=alertname,
        row_count=deleted,
    )

    # 5) Post-commit: kick vmalert reload; failure is non-fatal.
    # HOMELAB_MONITOR_VMALERT_URL overrides the compose-DNS default for host-run CLI use.
    vmalert_url = os.getenv("HOMELAB_MONITOR_VMALERT_URL", "http://vmalert-metrics:8880")
    reloader = VmalertReloader(base_url=vmalert_url)
    reloaded = await reloader.reload()
    if not reloaded:
        print(
            "Warning: vmalert reload failed; changes will activate within ~30s via config poll.",
            file=sys.stderr,
        )

    return 0


async def _cmd_backfill_outcomes(args: argparse.Namespace) -> int:  # noqa: PLR0915  # guard+query+insert+audit dispatcher; extracting would fragment transaction
    """One-shot backfill of ``alert_outcomes`` from ``alerts.ack_at``/``resolved_at``.

    Deterministic inference: ``ack_at IS NOT NULL`` -> ACKED; ``resolved_at IS NOT NULL
    AND ack_at IS NULL`` -> AUTO_RESOLVED; still-firing alerts (both NULL) get no row.

    Guard (D6, locked): refuses to run if ANY row with ``decided_by='backfill'`` already
    exists (this backfill CLI has run before). Rows from other provenances (``'reconciler'``,
    ``'autofix'``, etc.) do NOT trigger the guard — those are legitimate ongoing writers.
    Overlap on ``(alert_id, outcome)`` between this backfill and prior reconciler writes is
    silently resolved by ``ON CONFLICT ... DO NOTHING`` (the earlier row wins).

    Writes exactly one summary ``audit_log`` row per real run (never for ``--dry-run``).
    """
    configure_logging()
    log: BoundLogger = structlog.get_logger()  # pyright: ignore[reportAssignmentType]

    dry_run: bool = bool(getattr(args, "dry_run", False))
    confirm_phrase: str | None = getattr(args, "confirm_phrase", None)

    # 1) Mode validation — must pick dry-run OR real-run.
    if not dry_run and confirm_phrase is None:
        print(
            "Error: --dry-run or --confirm-phrase required",
            file=sys.stderr,
        )
        return 1

    # 2) Real-run: verify confirm-phrase (case-fold match, mirrors _cmd_purge).
    if not dry_run:
        assert confirm_phrase is not None  # narrowed by branch above
        got = confirm_phrase.strip().casefold()
        expected = _EXPECTED_BACKFILL_CONFIRM_PHRASE.strip().casefold()
        if got != expected:
            print(
                f"Error: confirm_phrase must equal '{_EXPECTED_BACKFILL_CONFIRM_PHRASE}'",
                file=sys.stderr,
            )
            return 1

    # 3) Open engine + repo.
    engine = get_engine()
    repo = SqliteRepository(engine)

    # 4) GUARD + Query + (real-run) insert, all in ONE transaction.
    #    Guard check inside the same transaction as the writes eliminates
    #    the race where two concurrent invocations could both pass the guard
    #    and both write duplicate backfill rows.
    async with repo.transaction() as conn:
        # Guard check (real run only): refuse if this backfill has already run.
        if not dry_run:
            guard_result = await conn.execute(
                _BACKFILL_GUARD_SQL, {"decided_by": _BACKFILL_DECIDED_BY}
            )
            already_run = bool(guard_result.scalar_one())
            if already_run:
                print(
                    "Error: backfill-outcomes has already been run "
                    "(a decided_by='backfill' row already exists in alert_outcomes). "
                    "To re-run (only for tests or after intentional cleanup), "
                    "manually DELETE the backfill rows: "
                    "sqlite3 <db> \"DELETE FROM alert_outcomes WHERE decided_by='backfill'\".",
                    file=sys.stderr,
                )
                return 1

        # Query + (real-run) insert continues here in the same transaction.
        if dry_run:
            acked_count = int((await conn.execute(_COUNT_ACKED_SQL)).scalar_one())
            auto_resolved_count = int((await conn.execute(_COUNT_AUTO_RESOLVED_SQL)).scalar_one())
            total = acked_count + auto_resolved_count
            print(
                f"Dry run: would insert {acked_count} ACKED + "
                f"{auto_resolved_count} AUTO_RESOLVED = {total} alert_outcomes rows. "
                "No DB changes made."
            )
            return 0

        # Real run: materialize rows to build INSERT params (uuid7 per row is Python-side).
        acked_result = await conn.execute(_SELECT_ACKED_SQL)
        acked_rows = acked_result.all()
        auto_resolved_result = await conn.execute(_SELECT_AUTO_RESOLVED_SQL)
        auto_resolved_rows = auto_resolved_result.all()

        acked_count = len(acked_rows)
        auto_resolved_count = len(auto_resolved_rows)
        total = acked_count + auto_resolved_count

        now = utc_now_iso()

        acked_params: list[dict[str, str | None]] = []
        for i, row in enumerate(acked_rows, start=1):
            alert_id, ack_at = row[0], row[1]
            acked_params.append(
                {
                    "id": uuid7(),
                    "aid": alert_id,
                    "outcome": AlertOutcome.ACKED.value,
                    "dt": ack_at,
                    "db": _BACKFILL_DECIDED_BY,
                    "created": now,
                }
            )
            if i % _BACKFILL_PROGRESS_INTERVAL == 0 and i < acked_count:
                print(f"Progress: processed {i}/{acked_count} ACKED rows...")

        auto_resolved_params: list[dict[str, str | None]] = []
        for i, row in enumerate(auto_resolved_rows, start=1):
            alert_id, resolved_at = row[0], row[1]
            auto_resolved_params.append(
                {
                    "id": uuid7(),
                    "aid": alert_id,
                    "outcome": AlertOutcome.AUTO_RESOLVED.value,
                    "dt": resolved_at,
                    "db": _BACKFILL_DECIDED_BY,
                    "created": now,
                }
            )
            if i % _BACKFILL_PROGRESS_INTERVAL == 0 and i < auto_resolved_count:
                print(f"Progress: processed {i}/{auto_resolved_count} AUTO_RESOLVED rows...")

        if acked_params:
            await conn.execute(_INSERT_OUTCOME_SQL, acked_params)
        if auto_resolved_params:
            await conn.execute(_INSERT_OUTCOME_SQL, auto_resolved_params)

        await insert_audit(
            conn,
            who=_BACKFILL_WHO,
            what=_BACKFILL_WHAT,
            before=None,
            after={
                "acked_count": acked_count,
                "auto_resolved_count": auto_resolved_count,
                "total": total,
                "at": now,
                "backfill_source": "alerts_row_state",
            },
        )

    print(
        f"Backfilled {acked_count} ACKED + {auto_resolved_count} AUTO_RESOLVED "
        f"= {total} alert_outcomes rows. audit_log row written."
    )
    log.info(
        "alerts.backfill_outcomes.committed",
        acked_count=acked_count,
        auto_resolved_count=auto_resolved_count,
        total=total,
    )

    return 0


__all__ = ["add_subparser"]
