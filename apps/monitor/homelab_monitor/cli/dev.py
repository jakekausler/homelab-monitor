"""``hm dev`` subcommand group — DEV-ONLY helpers.

These commands write deterministic synthetic data for manual Refinement-phase
testing of the UI. Each subcommand refuses to run against a non-local target
unless ``--force`` is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import socket
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text

from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.cron.run_repository import CronRunRepository
from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.saved_queries_repo import SavedQueriesRepository
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

# ---------------------------------------------------------------------------
# seed-container-metrics constants
# ---------------------------------------------------------------------------

# Dev-CLI default: host-published port (not docker-internal victoriametrics:8428)
# because this CLI runs directly on the host, not inside the compose network.
_DEFAULT_VM_URL = "http://127.0.0.1:18428"

# Label that tags every synthetic series so --clear can target ONLY them.
_SYNTH_LABEL_KEY = "homelab_synthetic"
_SYNTH_LABEL_VAL = "true"

# Default container count for seed-container-metrics.
_DEFAULT_CONTAINER_COUNT = 5

# Five distinct shape generators — idx % 5 selects which a container uses.
# Each shape returns a (cpu_seconds, mem_bytes, net_rx_bytes, net_tx_bytes)
# 4-tuple given the deterministic seed value.
_SHAPE_NAMES: tuple[str, ...] = (
    "cpu-sawtooth",
    "memory-step",
    "network-bursts",
    "idle",
    "spiky",
)


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

    p_seed_containers = sub.add_parser(
        "seed-container-metrics",
        help=(
            "DEV-ONLY: inject synthetic container_* metrics into VictoriaMetrics. "
            "Mirrors the cadvisor exposition surface for UI Refinement."
        ),
    )
    p_seed_containers.add_argument(
        "--containers",
        type=int,
        default=_DEFAULT_CONTAINER_COUNT,
        help=f"Number of fake containers to seed (default {_DEFAULT_CONTAINER_COUNT}).",
    )
    p_seed_containers.add_argument(
        "--clear",
        action="store_true",
        help='Delete all synthetic series (homelab_synthetic="true") and exit.',
    )
    p_seed_containers.add_argument(
        "--force",
        action="store_true",
        help="Allow running against a non-local hostname (use with caution).",
    )
    p_seed_containers.add_argument(
        "--vm-url",
        default=None,
        help=(
            "Override VictoriaMetrics URL "
            "(flag > env HOMELAB_MONITOR_VM_URL > default http://127.0.0.1:18428)."
        ),
    )
    p_seed_containers.set_defaults(func=_handle)

    p_seed_sq = sub.add_parser(
        "seed-saved-queries",
        help="DEV-ONLY: insert 3 deterministic log_saved_queries rows for Refinement.",
    )
    p_seed_sq.add_argument(
        "--clear",
        action="store_true",
        help="Delete the 3 seed rows (by name) and exit.",
    )
    p_seed_sq.set_defaults(func=_handle)

    dev.set_defaults(func=_handle)


def _handle(args: argparse.Namespace) -> int:
    sub = getattr(args, "dev_cmd", None)
    if sub == "seed-cron-runs":
        return asyncio.run(_cmd_seed_cron_runs(args.fingerprint, force=bool(args.force)))
    if sub == "clear-cron-runs":
        return asyncio.run(_cmd_clear_cron_runs(args.fingerprint, force=bool(args.force)))
    if sub == "seed-container-metrics":
        return asyncio.run(
            _cmd_seed_container_metrics(
                containers=int(args.containers),
                clear=bool(args.clear),
                force=bool(args.force),
                vm_url=args.vm_url,
            )
        )
    if sub == "seed-saved-queries":
        return asyncio.run(_cmd_seed_saved_queries(clear=bool(args.clear)))
    print(
        "usage: hm dev {seed-cron-runs,clear-cron-runs,seed-container-metrics,seed-saved-queries}",
        file=sys.stderr,
    )
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


# ---------------------------------------------------------------------------
# seed-container-metrics implementation
# ---------------------------------------------------------------------------


def _resolve_vm_url(flag_value: str | None) -> str:
    """Resolve VictoriaMetrics URL with flag > env > default precedence."""
    if flag_value:
        return flag_value.rstrip("/")
    env_value = os.environ.get("HOMELAB_MONITOR_VM_URL")
    if env_value:
        return env_value.rstrip("/")
    return _DEFAULT_VM_URL.rstrip("/")


def _validate_local_hostname(force: bool) -> int:
    """Refuse to run on a non-local host unless --force is passed."""
    local = socket.gethostname()
    host_env = os.environ.get("HM_HOST_HOSTNAME", "").strip()
    # If HM_HOST_HOSTNAME is set AND differs from socket.gethostname(), refuse
    # (the operator overrode the hostname; the running shell is somewhere else).
    # If HM_HOST_HOSTNAME is unset, accept — the CLI talks to 127.0.0.1 by default
    # and the only "remote" risk is a misconfigured HOMELAB_MONITOR_VM_URL, not
    # a hostname mismatch.
    if host_env and host_env != local and not force:
        print(
            f"ERROR: hostname mismatch — process is on {local!r}, "
            f"HM_HOST_HOSTNAME={host_env!r}. Pass --force to override (DEV-ONLY).",
            file=sys.stderr,
        )
        return 1
    return 0


def _shape_values(
    shape: str, rng: random.Random, idx: int, now_epoch: int | None = None
) -> tuple[float, float, float, float]:
    """Generate a deterministic 4-tuple of (cpu_s, mem_bytes, net_rx_b, net_tx_b)
    for the given shape and rng. The rng is already seeded by the caller so
    repeated calls within a run cycle yield identical output.

    If now_epoch is provided, use it for time-based calculations. Otherwise,
    use the current time (for live CLI usage).
    """
    if now_epoch is None:
        now_epoch = int(time.time())

    if shape == "cpu-sawtooth":
        cpu = float((idx * 17 + int(now_epoch // 60)) % 60) + rng.uniform(0, 0.5)
        mem = 64.0 * 1024 * 1024 + rng.uniform(0, 4 * 1024 * 1024)
        net_rx = 10_000.0 + rng.uniform(0, 1_000)
        net_tx = 5_000.0 + rng.uniform(0, 500)
    elif shape == "memory-step":
        cpu = 1.0 + rng.uniform(0, 0.2)
        # Step up every minute, capped at ~512 MiB.
        mem = min(
            512.0 * 1024 * 1024,
            64.0 * 1024 * 1024 + (int(now_epoch // 60) % 16) * 16 * 1024 * 1024,
        )
        net_rx = 2_000.0
        net_tx = 1_000.0
    elif shape == "network-bursts":
        cpu = 2.0
        mem = 128.0 * 1024 * 1024
        burst = 1.0 if (int(now_epoch // 10) % 3 == 0) else 0.05
        net_rx = 100_000.0 * burst + rng.uniform(0, 1_000)
        net_tx = 100_000.0 * burst + rng.uniform(0, 1_000)
    elif shape == "idle":
        cpu = 0.01
        mem = 16.0 * 1024 * 1024
        net_rx = 100.0
        net_tx = 50.0
    elif shape == "spiky":
        # Use minute bucket for determinism within the same minute.
        minute_bucket = now_epoch // 60
        spike = math.sin(minute_bucket / 30.0)
        cpu = 5.0 + 4.0 * spike + rng.uniform(0, 0.1)
        mem = 256.0 * 1024 * 1024 + 32 * 1024 * 1024 * spike
        net_rx = 50_000.0 + 25_000 * spike
        net_tx = 25_000.0 + 12_500 * spike
    else:  # pragma: no cover  -- guarded by _SHAPE_NAMES tuple
        msg = f"unknown shape: {shape!r}"
        raise ValueError(msg)
    return cpu, mem, net_rx, net_tx


def _build_exposition(containers: int, now_epoch: int) -> str:
    """Build a Prometheus exposition-format payload for ``containers`` synthetic
    containers. Deterministic on ``(containers, now_epoch // 60)`` — two runs in
    the same minute with the same container count yield identical payloads.
    """
    # Seed the RNG on (containers, minute) so a same-minute re-run is identical.
    minute_bucket = now_epoch // 60
    rng = random.Random(f"hm-synth-{containers}-{minute_bucket}")

    lines: list[str] = []
    for idx in range(containers):
        name = f"hm-synth-{idx}"
        shape = _SHAPE_NAMES[idx % len(_SHAPE_NAMES)]
        cpu, mem, net_rx, net_tx = _shape_values(shape, rng, idx, now_epoch)
        labels = f'name="{name}",{_SYNTH_LABEL_KEY}="{_SYNTH_LABEL_VAL}",shape="{shape}"'
        lines.append(f"container_cpu_usage_seconds_total{{{labels}}} {cpu}")
        lines.append(f"container_memory_working_set_bytes{{{labels}}} {mem}")
        lines.append(f"container_network_receive_bytes_total{{{labels}}} {net_rx}")
        lines.append(f"container_network_transmit_bytes_total{{{labels}}} {net_tx}")
        lines.append(f"container_last_seen{{{labels}}} {now_epoch}")
    # Prometheus exposition requires a trailing newline.
    return "\n".join(lines) + "\n"


async def _cmd_seed_container_metrics(
    *,
    containers: int,
    clear: bool,
    force: bool,
    vm_url: str | None,
) -> int:
    """Implementation for ``hm dev seed-container-metrics``.

    With ``clear=True``: deletes only series labeled ``homelab_synthetic="true"``.
    Otherwise: emits ``containers`` synthetic series in 5 distinct shapes.
    """
    rc = _validate_local_hostname(force)
    if rc != 0:
        return rc

    resolved_url = _resolve_vm_url(vm_url)

    if containers <= 0:
        print("ERROR: --containers must be >= 1", file=sys.stderr)
        return 1

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        if clear:
            return await _clear_synthetic_series(client, resolved_url)
        return await _post_synthetic_series(client, resolved_url, containers=containers)


async def _post_synthetic_series(
    client: httpx.AsyncClient,
    vm_url: str,
    *,
    containers: int,
) -> int:
    """POST synthetic exposition data to VM /api/v1/import/prometheus."""
    now_epoch = int(time.time())
    payload = _build_exposition(containers, now_epoch)
    url = f"{vm_url}/api/v1/import/prometheus"
    try:
        resp = await client.post(
            url,
            content=payload,
            headers={"Content-Type": "text/plain"},
        )
    except httpx.HTTPError as exc:
        print(f"ERROR: VM unreachable at {url}: {exc}", file=sys.stderr)
        return 1
    if resp.status_code >= 400:  # noqa: PLR2004
        print(
            f"ERROR: VM rejected import (status={resp.status_code}): {resp.text}",
            file=sys.stderr,
        )
        return 1
    print(f"seeded {containers} synthetic containers via {url}")
    return 0


async def _clear_synthetic_series(client: httpx.AsyncClient, vm_url: str) -> int:
    """DELETE all series carrying homelab_synthetic="true" via VM admin API."""
    url = f"{vm_url}/api/v1/admin/tsdb/delete_series"
    params = {"match[]": f'{{{_SYNTH_LABEL_KEY}="{_SYNTH_LABEL_VAL}"}}'}
    try:
        resp = await client.post(url, params=params)
    except httpx.HTTPError as exc:
        print(f"ERROR: VM unreachable at {url}: {exc}", file=sys.stderr)
        return 1
    if resp.status_code >= 400:  # noqa: PLR2004
        print(
            f"ERROR: VM rejected delete_series (status={resp.status_code}): {resp.text}",
            file=sys.stderr,
        )
        return 1
    print(f"cleared all synthetic series ({_SYNTH_LABEL_KEY}={_SYNTH_LABEL_VAL}) via {url}")
    return 0


async def _cmd_seed_saved_queries(*, clear: bool) -> int:
    engine = get_engine()
    repo = SqliteRepository(engine)
    sq = SavedQueriesRepository(repo)

    seed_names = [
        "nginx errors (last hour)",
        "auth failures (custom range)",
        "infra services overview",
    ]
    # Idempotent: remove any existing rows with these names first.
    async with repo.transaction() as conn:
        for nm in seed_names:
            await conn.execute(text("DELETE FROM log_saved_queries WHERE name = :n"), {"n": nm})
    if clear:
        print(f"cleared {len(seed_names)} seed saved queries")
        return 0

    # 1. Plain mode + preset (nginx errors, since=1h)
    await sq.create(
        name=seed_names[0],
        logs_ql="error",
        selected_services=[{"service": "nginx", "source_type": "docker"}],
        since_preset="1h",
        range_start_iso=None,
        range_end_iso=None,
        advanced_mode=False,
    )
    # 2. Advanced LogsQL + custom start/end range
    await sq.create(
        name=seed_names[1],
        logs_ql='_msg:"authentication failure"',
        selected_services=[],
        since_preset=None,
        range_start_iso="2026-05-01T00:00:00.000Z",
        range_end_iso="2026-05-02T00:00:00.000Z",
        advanced_mode=True,
    )
    # 3. Plain mode + multi-service selection
    await sq.create(
        name=seed_names[2],
        logs_ql="",
        selected_services=[
            {"service": "grafana", "source_type": "docker"},
            {"service": "victorialogs", "source_type": "docker"},
            {"service": "alertmanager", "source_type": "docker"},
        ],
        since_preset="6h",
        range_start_iso=None,
        range_end_iso=None,
        advanced_mode=False,
    )
    print("seeded 3 saved queries")
    return 0
