"""E2E / integration tests for STAGE-001-009: subprocess plugin runner + JSON line protocol.

Unit tests (test_subprocess_runner.py) cover all protocol line types and error
paths using small synthetic bash scripts. These tests fill the gaps under
closer-to-production conditions:

  1. Real hello-subprocess-plugin (shipped in runbooks/_examples/) routed through
     make_subprocess_collector → class factory → CollectorContext → result.
  2. All 5 protocol line types (metric + log + event + heartbeat + result) emitted in
     one bash run; every piece verified in the CollectorResult and side-writers.
  3. Timeout escalation wall-clock: SIGTERM-ignoring plugin, 1s timeout + 2s grace
     = ~3s total; wall-time verified within realistic CI bounds.
  4. Untrusted plugin secret filtering: resolver has 3 secrets, manifest declares 1;
     stdin JSON must carry only the declared secret.
  5. loader.persist_to_db end-to-end with real SQLite DB + INSERT OR IGNORE idempotency.

Wall-clock note: test 3 takes ~3s; total suite ~10s.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from homelab_monitor.kernel.db.engine import get_engine
from homelab_monitor.kernel.db.migrations import alembic_upgrade_head
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
)
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.manifest import SubprocessManifest
from homelab_monitor.kernel.plugins.subprocess_collector import make_subprocess_collector
from homelab_monitor.kernel.plugins.subprocess_runner import run_subprocess
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]  # apps/monitor/tests/ -> repo root
HELLO_PLUGIN_DIR = REPO_ROOT / "runbooks" / "_examples" / "hello-subprocess-plugin"

# ---------------------------------------------------------------------------
# Fixtures — real kernel types (mirrors test_scheduler_quarantine_e2e.py style)
# ---------------------------------------------------------------------------


@pytest.fixture
def _tmp_db_path() -> Path:  # type: ignore[return]
    fd, raw = tempfile.mkstemp(prefix="hm-sp-e2e-", suffix=".db")
    os.close(fd)
    path = Path(raw)
    path.unlink(missing_ok=True)
    yield path  # type: ignore[misc]
    for suffix in ("", "-wal", "-shm"):
        (path.parent / (path.name + suffix)).unlink(missing_ok=True)


@pytest.fixture
async def real_engine(_tmp_db_path: Path) -> AsyncEngine:  # type: ignore[return]
    url = f"sqlite+aiosqlite:///{_tmp_db_path}"
    alembic_upgrade_head(url)
    engine = get_engine(url=url)
    yield engine  # type: ignore[misc]
    await engine.dispose()


@pytest.fixture
def real_repo(real_engine: AsyncEngine) -> SqliteRepository:
    return SqliteRepository(engine=real_engine)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    *,
    name: str = "test-plugin",
    secrets: dict[str, str] | None = None,
    logs_writer: InMemoryLogsWriter | None = None,
) -> tuple[CollectorContext, InMemoryMetricsWriter, InMemoryLogsWriter]:
    """Build a real CollectorContext. Returns (ctx, metrics_writer, logs_writer)."""
    metrics = InMemoryMetricsWriter()
    vl = logs_writer if logs_writer is not None else InMemoryLogsWriter()
    config = CollectorConfig(name=name)
    log = structlog.stdlib.get_logger().bind()
    secrets_resolver = SyncSecretsResolver(secrets or {})
    ctx = CollectorContext(
        config=config,
        db=None,  # type: ignore[arg-type]  # subprocess runner does not touch DB
        vm=metrics,
        vl=vl,
        http=httpx.AsyncClient(),
        ssh=None,  # type: ignore[arg-type]
        secrets=secrets_resolver,
        log=log,  # type: ignore[arg-type]
        ha=None,
    )
    return ctx, metrics, vl


def _write_plugin(  # noqa: PLR0913
    tmp_path: Path,
    *,
    name: str,
    script: str,
    interval: str = "60s",
    timeout: str = "5s",
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    trust_level: str = "trusted",
) -> tuple[SubprocessManifest, Path]:
    """Write a plugin.yaml + run.sh into tmp_path. Returns (manifest, plugin_dir)."""
    plugin_dir = tmp_path / name
    plugin_dir.mkdir()
    run_sh = plugin_dir / "run.sh"
    run_sh.write_text(script)
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    manifest_data: dict[str, Any] = {
        "manifest": 1,
        "name": name,
        "command": ["./run.sh"],
        "interval": interval,
        "timeout": timeout,
        "trust_level": trust_level,
        "env": env or {},
        "secrets": secrets or [],
    }
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(manifest_data))
    manifest = SubprocessManifest.load_from_path(plugin_dir / "plugin.yaml")
    return manifest, plugin_dir


# ---------------------------------------------------------------------------
# Scenario 1 — Real hello-subprocess-plugin via make_subprocess_collector
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_hello_world_plugin_via_collector_class_factory() -> None:
    """Load the shipped hello-subprocess-plugin via make_subprocess_collector.

    Verifies the full chain: manifest file → class factory → collector instance
    → CollectorContext → run() → result. Asserts result.ok=True and the
    homelab_hello_world counter metric with language="bash" label appears in the
    real InMemoryMetricsWriter.
    """
    manifest = SubprocessManifest.load_from_path(HELLO_PLUGIN_DIR / "plugin.yaml")
    cls = make_subprocess_collector(manifest, HELLO_PLUGIN_DIR)

    # ClassVar attributes must be wired correctly from the manifest
    assert cls.name == "hello-subprocess"

    instance = cls()
    ctx, metrics, _vl = _make_ctx(name="hello-subprocess")

    result = await instance.run(ctx)

    assert result.ok is True, f"hello-world plugin must succeed; errors: {result.errors}"
    assert result.metrics_emitted == 1, f"expected 1 metric emitted, got {result.metrics_emitted}"

    metric_names = [m.name for m in metrics.recorded]
    assert "homelab_hello_world" in metric_names, (
        f"homelab_hello_world counter not in recorded metrics: {metric_names}"
    )
    hw_metrics = [m for m in metrics.recorded if m.name == "homelab_hello_world"]
    assert any(m.labels.get("language") == "bash" for m in hw_metrics), (
        f"language=bash label not found on homelab_hello_world: {[m.labels for m in hw_metrics]}"
    )


# ---------------------------------------------------------------------------
# Scenario 2 — All 5 protocol line types in one run
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_all_five_protocol_line_types_parsed_correctly(tmp_path: Path) -> None:
    """Bash plugin emits all 5 line types; every piece is verified in the result.

    Protocol lines: metric (counter), log, event (suggestion), heartbeat, result.
    Verifies: metrics.recorded has the metric, InMemoryLogsWriter.recorded has
    the log, result.events has 2 items (suggestion + heartbeat), result.ok=True.
    """
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"metric","name":"e2e_all_types","kind":"counter","value":7,"labels":{"src":"e2e"}}'
echo '{"type":"log","stream":"stdout","line":"e2e log line"}'
echo '{"type":"event","kind":"suggestion","title":"e2e title","body":"e2e body"}'
echo '{"type":"heartbeat","source":"e2e-watchdog"}'
echo '{"type":"result","ok":true,"summary":"all 5 types"}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="all-types", script=script)
    logs_writer = InMemoryLogsWriter()
    ctx, metrics, vl = _make_ctx(name="all-types", logs_writer=logs_writer)

    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)

    # Result line
    assert result.ok is True, f"expected ok=True; errors: {result.errors}"

    # Metric line
    assert result.metrics_emitted == 1, f"expected 1 metric; got {result.metrics_emitted}"
    recorded_names = [m.name for m in metrics.recorded]
    assert "e2e_all_types" in recorded_names, f"e2e_all_types not in {recorded_names}"
    e2e_metric = next(m for m in metrics.recorded if m.name == "e2e_all_types")
    assert e2e_metric.labels.get("src") == "e2e"
    assert e2e_metric.value == 7.0  # noqa: PLR2004

    # Log line — InMemoryLogsWriter.recorded stores LogEntry dataclass objects
    log_entries = vl.recorded
    assert any(e.stream == "stdout" and "e2e log line" in e.line for e in log_entries), (
        f"log line not found in writer: {log_entries}"
    )

    # Events: suggestion + heartbeat = 2
    assert len(result.events) == 2, (  # noqa: PLR2004
        f"expected 2 events (suggestion + heartbeat), got {len(result.events)}: {result.events}"
    )


# ---------------------------------------------------------------------------
# Scenario 3 — Timeout escalation: SIGTERM → SIGKILL wall-clock
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_timeout_sigterm_then_sigkill_wall_clock() -> None:
    """SIGTERM-ignoring plugin with 1s timeout; full kill takes ~3s.

    Uses the real hello-subprocess-plugin dir for a clean tmp-independent
    manifest_dir, but writes the actual script to tmp using _write_plugin.
    The plugin traps SIGTERM (ignores it) and sleeps 30s. With timeout=1s,
    the runner sends SIGTERM, waits 2s grace, then SIGKILLs.
    Wall-time must be between 2.5s and 8s (generous CI bound).
    result.ok must be False with timeout error message.
    """
    # Build via tmp_path manually since we can't use tmp_path fixture directly;
    # use a tempfile directory instead.
    with tempfile.TemporaryDirectory(prefix="hm-e2e-timeout-") as td:
        tmp = Path(td)
        script = """#!/usr/bin/env bash
cat >/dev/null
trap '' TERM
sleep 30
"""
        manifest, plugin_dir = _write_plugin(
            tmp,
            name="timeout-test",
            script=script,
            interval="10s",
            timeout="1s",
        )

        ctx, _metrics, _vl = _make_ctx(name="timeout-test")

        t0 = time.monotonic()
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
        elapsed = time.monotonic() - t0

    assert result.ok is False, "SIGKILL-terminated plugin must produce ok=False"
    assert any("timeout" in e.lower() for e in result.errors), (
        f"expected 'timeout' in errors; got: {result.errors}"
    )
    # 1s timeout + 2s SIGKILL grace = ~3s; allow 2s-8s for CI jitter
    assert 2.0 <= elapsed <= 8.0, (  # noqa: PLR2004
        f"timeout+kill wall-time expected 2-8s, got {elapsed:.2f}s"
    )


# ---------------------------------------------------------------------------
# Scenario 4 — Untrusted plugin: secrets filtering via real SyncSecretsResolver
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_untrusted_plugin_secrets_filtered_to_manifest_declarations(
    tmp_path: Path,
) -> None:
    """Real SyncSecretsResolver with 3 secrets; manifest declares only 1.

    The plugin captures the stdin JSON blob (written by the runner) and emits
    its content as a log line. We then verify that only the declared secret
    appears in the stdin payload — the runner must filter the resolver's full
    secrets dict down to the manifest's `secrets` list.
    """
    # Plugin: drain stdin into OUTFILE, emit result
    script = """#!/usr/bin/env bash
cat > "$OUTFILE"
echo '{"type":"result","ok":true}'
"""
    outfile = tmp_path / "stdin.json"
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="secret-filter",
        script=script,
        env={"OUTFILE": str(outfile)},
        secrets=["secret_a"],  # only secret_a declared in manifest
        trust_level="untrusted",
    )

    # Resolver has 3 secrets; only secret_a should reach the plugin
    ctx, _metrics, _vl = _make_ctx(
        name="secret-filter",
        secrets={
            "secret_a": "value_a",
            "secret_b": "value_b",
            "secret_c": "value_c",
        },
    )

    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)

    assert result.ok is True, f"plugin must succeed; errors: {result.errors}"
    assert outfile.exists(), "plugin must have written stdin JSON to OUTFILE"
    payload = json.loads(outfile.read_text())

    secrets_in_payload = payload.get("secrets", {})
    assert "secret_a" in secrets_in_payload, (
        f"declared secret 'secret_a' must be in stdin payload; got keys: {list(secrets_in_payload)}"
    )
    assert secrets_in_payload["secret_a"] == "value_a"
    assert "secret_b" not in secrets_in_payload, (
        f"undeclared 'secret_b' must NOT appear in stdin payload; keys: {list(secrets_in_payload)}"
    )
    assert "secret_c" not in secrets_in_payload, (
        f"undeclared 'secret_c' must NOT appear in stdin payload; keys: {list(secrets_in_payload)}"
    )


# ---------------------------------------------------------------------------
# Scenario 5 — loader.persist_to_db end-to-end with real DB + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_loader_persist_to_db_inserts_and_is_idempotent(
    real_repo: SqliteRepository,
    real_engine: AsyncEngine,
) -> None:
    """Load hello-subprocess-plugin via loader; persist_to_db writes DB row.

    Steps:
      1. Create a PluginLoader; call load_subprocess_plugins(hello_plugin_dir).
      2. Call await loader.persist_to_db(repo) — must insert a row.
      3. Query collectors table; verify name="hello-subprocess" row exists.
      4. Call persist_to_db again — INSERT OR IGNORE must not raise or duplicate.
      5. Re-query; row count must still be 1.
    """
    loader = PluginLoader()
    n = loader.load_subprocess_plugins(HELLO_PLUGIN_DIR)
    assert n == 1, f"expected 1 plugin loaded from hello-subprocess-plugin dir, got {n}"

    loaded_all = loader.load_all()
    assert len(loaded_all) == 1
    assert loaded_all[0].config.name == "hello-subprocess"

    # First persist
    await loader.persist_to_db(real_repo)

    async with real_repo.transaction() as conn:
        row = (
            await conn.execute(
                text("SELECT id, name FROM collectors WHERE name = :name"),
                {"name": "hello-subprocess"},
            )
        ).fetchone()

    assert row is not None, "collectors row for 'hello-subprocess' must exist after persist_to_db"
    row_id = row[0]
    assert row[1] == "hello-subprocess"

    # Second persist — must be idempotent (INSERT OR IGNORE)
    await loader.persist_to_db(real_repo)

    async with real_repo.transaction() as conn:
        rows = (
            await conn.execute(
                text("SELECT id FROM collectors WHERE name = :name"),
                {"name": "hello-subprocess"},
            )
        ).fetchall()

    assert len(rows) == 1, (
        f"INSERT OR IGNORE must not duplicate; found {len(rows)} rows after second persist"
    )
    assert rows[0][0] == row_id, (
        f"row id must not change after idempotent insert; original={row_id}, current={rows[0][0]}"
    )
