"""``hm alerts`` subcommand — alert-table operations (STAGE-010-001)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import structlog
from sqlalchemy import text
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.db.audit import insert_audit
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logging import configure_logging
from homelab_monitor.kernel.vmalert.reload import VmalertReloader

_EXPECTED_CONFIRM_PHRASE = "delete signature-silent noise"
_WHO = "cli:stage-010-001-purge"
_WHAT = "alerts_bulk_purged"
_SOURCE_TOOL = "vmalert-metrics"
_ALERTGROUP = "signature_silent"

_COUNT_SQL = text(
    "SELECT COUNT(*) FROM alerts "
    "WHERE json_extract(payload_json, '$.labels.alertname') = :alertname"
)
_DELETE_SQL = text(
    "DELETE FROM alerts WHERE json_extract(payload_json, '$.labels.alertname') = :alertname"
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
    alerts.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    sub = getattr(args, "alerts_cmd", None)
    if sub == "purge":
        return asyncio.run(_cmd_purge(args))
    print("usage: hm alerts {purge}", file=sys.stderr)
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


__all__ = ["add_subparser"]
