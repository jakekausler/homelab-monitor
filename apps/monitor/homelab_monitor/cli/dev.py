"""``hm dev`` subcommand group — DEV-ONLY helpers (seed/clear cron runs).

These commands write deterministic synthetic data to the local DB for
manual Refinement-phase testing of the run-history UI. Refuses to run
against a non-local cron unless --force is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.cron.run_repository import CronRunRepository
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.plugins.discoverers.cron_discoverer import resolve_hostname

# Run mix specification — total = 60.
_OK_COUNT = 50
_FAIL_COUNT = 5
_UNKNOWN_COUNT = 3
_RUNNING_COUNT = 1
_OVERLAPPING_INDICES: list[int] = [1, 9]
_OVERLAPPING_COUNT = len(_OVERLAPPING_INDICES)
# anomaly_flags assignments: indices into the OK+FAIL pool (0-indexed of the
# inserted closed-run sequence).
_ANOMALY_ASSIGNMENTS: list[tuple[int, str]] = [
    (3, "duration_outlier"),
    (7, "exit_code_changed"),
    (12, "unexpected_empty"),
    (18, "duration_outlier,output_size_drop"),
    (25, "duration_outlier"),
]


def add_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    dev = subparsers.add_parser(
        "dev",
        help="DEV-ONLY helpers (seed/clear synthetic data). Refuses non-local fingerprints.",
    )
    sub = dev.add_subparsers(dest="dev_cmd")

    p_seed = sub.add_parser(
        "seed-cron-runs",
        help="DEV-ONLY: insert ~60 deterministic cron_runs rows for one cron.",
    )
    p_seed.add_argument("fingerprint", help="Target cron fingerprint (must be local)")
    p_seed.add_argument(
        "--force",
        action="store_true",
        help="Allow seeding against a non-local cron (use with caution).",
    )
    p_seed.set_defaults(func=_handle)

    p_clear = sub.add_parser(
        "clear-cron-runs",
        help="DEV-ONLY: delete all cron_runs rows for one cron (for clean re-seeding).",
    )
    p_clear.add_argument("fingerprint")
    p_clear.add_argument("--force", action="store_true")
    p_clear.set_defaults(func=_handle)

    dev.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    sub = getattr(args, "dev_cmd", None)
    if sub == "seed-cron-runs":
        return asyncio.run(_cmd_seed_cron_runs(args.fingerprint, force=bool(args.force)))
    if sub == "clear-cron-runs":
        return asyncio.run(_cmd_clear_cron_runs(args.fingerprint, force=bool(args.force)))
    print("usage: hm dev {seed-cron-runs,clear-cron-runs}", file=sys.stderr)
    return 2


def _seed_run_id(fingerprint: str, idx: int) -> str:
    """Deterministic run_id for idempotent re-seeding."""
    return f"seed-{fingerprint[:12]}-{idx:03d}"


async def _validate_local(cron_repo: CronRepo, fingerprint: str, force: bool) -> int:
    cron = await cron_repo.get_cron(fingerprint, include_hidden=True)
    if cron is None:
        print(f"ERROR: cron not found: {fingerprint}", file=sys.stderr)
        return 1
    local = resolve_hostname()
    if cron.host != local and not force:
        print(
            f"ERROR: cron is on host {cron.host!r}, not local {local!r}. "
            f"Pass --force to override (DEV-ONLY).",
            file=sys.stderr,
        )
        return 1
    return 0


async def _cmd_seed_cron_runs(fingerprint: str, *, force: bool) -> int:
    engine = get_engine()
    repo = SqliteRepository(engine)
    cron_repo = CronRepo(repo)
    run_repo = CronRunRepository(repo)

    rc = await _validate_local(cron_repo, fingerprint, force)
    if rc != 0:
        return rc

    # Started_at values: spread across the last 7 days, newest at idx=0.
    now = datetime.now(UTC)
    inserted = 0

    # Build the ordered list of closed runs (50 ok + 5 fail + 3 unknown = 58).
    # closed runs: tuples of (state, exit_code, duration, source)
    closed: list[tuple[str, int | None, float | None, str]] = []
    for i in range(_OK_COUNT):
        # First 45 wrapper, last 5 logscrape.
        src = "wrapper" if i < (_OK_COUNT - 5) else "logscrape"
        closed.append(("ok", 0, 30.0 + (i % 7) * 2.0, src))
    for i in range(_FAIL_COUNT):
        closed.append(("fail", 1 if i % 2 == 0 else 2, 12.0 + i * 4.0, "wrapper"))
    for _ in range(_UNKNOWN_COUNT):
        closed.append(("unknown", None, None, "logscrape"))

    # Assign started_at, oldest at the end. Spread evenly across 7 days.
    total = len(closed) + _RUNNING_COUNT  # 58 + 1 = 59 dated rows
    step = timedelta(days=7) / max(1, total)
    for idx, (state, exit_code, duration, source) in enumerate(closed):
        started_at = (now - step * idx).isoformat()
        ended_at = (now - step * idx + timedelta(seconds=duration or 0.0)).isoformat()
        run_id = _seed_run_id(fingerprint, idx)
        await run_repo.insert_run(
            run_id=run_id,
            cron_fingerprint=fingerprint,
            source=source,
            started_at=started_at,
            vl_window_start=started_at,
        )
        await run_repo.close_run(
            run_id=run_id,
            cron_fingerprint=fingerprint,
            source=source,
            state=state,
            ended_at=ended_at,
            duration_seconds=duration,
            exit_code=exit_code,
            vl_window_end=ended_at,
        )
        inserted += 1

    # Apply anomaly flags.
    for closed_idx, flags in _ANOMALY_ASSIGNMENTS:
        if closed_idx < len(closed):
            await run_repo.set_anomaly_flags(
                run_id=_seed_run_id(fingerprint, closed_idx),
                anomaly_flags=flags,
            )

    # Apply overlapping on 2 rows (indices 1 and 9).
    for idx in _OVERLAPPING_INDICES:
        await run_repo.set_overlapping(_seed_run_id(fingerprint, idx))

    # The running row (idx = len(closed)).
    running_idx = len(closed)
    running_started = (now - step * running_idx).isoformat()
    await run_repo.insert_run(
        run_id=_seed_run_id(fingerprint, running_idx),
        cron_fingerprint=fingerprint,
        source="wrapper",
        started_at=running_started,
        vl_window_start=running_started,
    )
    inserted += 1

    # Apply enrichment metadata to OK rows so line_count / byte_count render.
    # Iterate by position to keep run_id stable regardless of ordering.
    for i, (state, _exit, _dur, _src) in enumerate(closed):
        if state != "ok":
            continue
        await run_repo.set_enrichment(
            run_id=_seed_run_id(fingerprint, i),
            line_count=5 + (i % 16),
            byte_count=200 + (i * 17) % 1500,
            content_digest=f"sha256:seed{i:03d}",
            enriched_at=(now - step * i + timedelta(seconds=60)).isoformat(),
        )

    print(f"seeded {inserted} cron_runs rows for {fingerprint}")
    return 0


async def _cmd_clear_cron_runs(fingerprint: str, *, force: bool) -> int:
    engine = get_engine()
    repo = SqliteRepository(engine)
    cron_repo = CronRepo(repo)

    rc = await _validate_local(cron_repo, fingerprint, force)
    if rc != 0:
        return rc

    async with repo.transaction() as conn:
        result = await conn.execute(
            text("DELETE FROM cron_runs WHERE cron_fingerprint = :fp"),
            {"fp": fingerprint},
        )
    print(f"deleted {result.rowcount} cron_runs rows for {fingerprint}")
    return 0
