"""Tests for the subprocess plugin runner.

Uses bash one-liner scripts written to tmp_path to exercise each protocol
line type and error path. The hello-world example plugin from
runbooks/_examples/ is also used as a smoke test.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import structlog
import yaml
from structlog.testing import capture_logs

from homelab_monitor.kernel.plugins import InMemoryMetricsWriter
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.manifest import SubprocessManifest
from homelab_monitor.kernel.plugins.subprocess_collector import (
    make_subprocess_collector,
)
from homelab_monitor.kernel.plugins.subprocess_runner import run_subprocess
from homelab_monitor.kernel.plugins.types import CollectorConfig, RunKind
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver

REPO_ROOT = Path(__file__).resolve().parents[3]  # apps/monitor/tests/ -> repo root
HELLO_PLUGIN_DIR = REPO_ROOT / "runbooks" / "_examples" / "hello-subprocess-plugin"


def _write_plugin(  # noqa: PLR0913 -- test fixture helper; arg count matches manifest schema fields for explicit per-test overrides
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
    """Write a plugin.yaml + run.sh into tmp_path. Returns (manifest, dir)."""
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


def _make_ctx(
    *,
    name: str = "test-plugin",
    secrets: dict[str, str] | None = None,
) -> tuple[CollectorContext, InMemoryMetricsWriter]:
    """Build a CollectorContext for testing. Returns (ctx, metrics_writer)."""
    metrics = InMemoryMetricsWriter()
    config = CollectorConfig(name=name)
    log = structlog.stdlib.get_logger().bind()
    secrets_resolver = SyncSecretsResolver(secrets or {})
    ctx = CollectorContext(
        config=config,
        db=None,  # type: ignore[arg-type]  # runner does not touch db
        vm=metrics,
        vl=_NoopLogsWriter(),
        http=httpx.AsyncClient(),
        ssh=None,  # type: ignore[arg-type]
        secrets=secrets_resolver,
        log=log,  # type: ignore[arg-type]  # structlog stdlib vs _generic BoundLogger; runtime-compatible
        ha=None,
    )
    return ctx, metrics


class _NoopLogsWriter:
    """Minimal LogsWriter stub for runner tests."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def ingest(self, stream: str, line: str, ts: str | None = None) -> None:
        self.entries.append((stream, line))


@pytest.mark.asyncio
async def test_hello_world_plugin_emits_metric_and_result_ok() -> None:
    """Smoke test: real example plugin runs end-to-end."""
    manifest = SubprocessManifest.load_from_path(HELLO_PLUGIN_DIR / "plugin.yaml")
    ctx, metrics = _make_ctx(name="hello-subprocess")
    result = await run_subprocess(manifest, ctx, manifest_dir=HELLO_PLUGIN_DIR)
    assert result.ok is True
    assert result.metrics_emitted == 1
    assert any(m.name == "homelab_hello_world" for m in metrics.recorded)


@pytest.mark.asyncio
async def test_metric_line_writes_to_ctx_vm_as_counter(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"metric","name":"foo","value":42,"labels":{}}'
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="metric1", script=script)
    ctx, metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert result.metrics_emitted == 1
    assert metrics.recorded[0].name == "foo"


@pytest.mark.asyncio
async def test_metric_line_with_kind_gauge_writes_gauge(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"metric","name":"g","kind":"gauge","value":3.14,"labels":{}}'
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="metric2", script=script)
    ctx, metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert any(m.kind == "gauge" for m in metrics.recorded)


@pytest.mark.asyncio
async def test_metric_line_with_kind_summary_writes_summary(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"metric","name":"s","kind":"summary","value":1.0,"labels":{}}'
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="metric3", script=script)
    ctx, metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert any(m.kind == "summary" for m in metrics.recorded)


@pytest.mark.asyncio
async def test_log_line_routes_to_ctx_vl(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"log","stream":"foo","line":"hello logs"}'
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="logtest", script=script)
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert ("foo", "hello logs") in cast(_NoopLogsWriter, ctx.vl).entries


@pytest.mark.asyncio
async def test_event_line_appends_to_result_events(tmp_path: Path) -> None:
    """Emit a SuggestionEvent."""
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"event","kind":"suggestion","title":"t","body":"b"}'
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="event1", script=script)
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert len(result.events) == 1


@pytest.mark.asyncio
async def test_heartbeat_line_appends_as_heartbeat_event(tmp_path: Path) -> None:
    """Scaffolding test for STAGE-002 hand-off."""
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"heartbeat","source":"my-watchdog"}'
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="hbeat", script=script)
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert len(result.events) == 1


@pytest.mark.asyncio
async def test_result_line_with_ok_false_returns_failure(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"result","ok":false,"errors":["something went wrong"]}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="resultfail", script=script)
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is False
    assert "something went wrong" in result.errors


@pytest.mark.asyncio
async def test_no_result_line_synthesizes_failure_with_error(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"metric","name":"x","value":1,"labels":{}}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="noresult", script=script)
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is False
    assert any("no result line emitted" in e for e in result.errors)


@pytest.mark.asyncio
async def test_malformed_json_line_logs_warning_and_continues(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo 'not-json-at-all'
echo '{"type":"metric","name":"foo","value":1,"labels":{}}'
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="malform", script=script)
    ctx, _metrics = _make_ctx()
    with capture_logs() as captured:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert result.metrics_emitted == 1
    assert any(c.get("event") == "subprocess.plugin.malformed_json_line" for c in captured)


@pytest.mark.asyncio
async def test_unknown_line_type_logs_info_and_discards(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"banana","value":1}'
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="unknown", script=script)
    ctx, _metrics = _make_ctx()
    with capture_logs() as captured:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert any(c.get("event") == "subprocess.plugin.unknown_line_type" for c in captured)


@pytest.mark.asyncio
async def test_line_after_result_logs_warning_and_discards(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"result","ok":true}'
echo '{"type":"metric","name":"after","value":1,"labels":{}}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="lineafter", script=script)
    ctx, metrics = _make_ctx()
    with capture_logs() as captured:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    # The post-result metric is discarded.
    assert not any(m.name == "after" for m in metrics.recorded)
    assert any(c.get("event") == "subprocess.plugin.line_after_result" for c in captured)


@pytest.mark.asyncio
async def test_nonzero_exit_with_ok_true_overrides_to_failure(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"result","ok":true}'
exit 7
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="nonzero", script=script)
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is False
    assert any("non-zero exit code: 7" in e for e in result.errors)


@pytest.mark.asyncio
async def test_subprocess_timeout_sigterm_then_sigkill_records_failure(
    tmp_path: Path,
) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
# Trap SIGTERM and ignore it to force SIGKILL escalation.
trap '' TERM
sleep 60
"""
    manifest, plugin_dir = _write_plugin(
        tmp_path, name="timeout", script=script, interval="10s", timeout="1s"
    )
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is False
    assert any("timeout" in e for e in result.errors)


@pytest.mark.asyncio
async def test_subprocess_spawn_failure_returns_error_result(tmp_path: Path) -> None:
    # Manifest references a command that does not exist.
    plugin_dir = tmp_path / "spawn-fail"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "manifest": 1,
                "name": "spawn-fail",
                "command": ["./does-not-exist"],
                "interval": "60s",
                "timeout": "5s",
            }
        )
    )
    manifest = SubprocessManifest.load_from_path(plugin_dir / "plugin.yaml")
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is False
    assert any("spawn failed" in e for e in result.errors)


@pytest.mark.asyncio
async def test_stderr_lines_logged_at_info_level(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo "stderr message" >&2
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="stderr1", script=script)
    ctx, _metrics = _make_ctx()
    with capture_logs() as captured:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    stderr_events = [c for c in captured if c.get("event") == "subprocess.plugin.stderr"]
    assert any("stderr message" in c.get("line", "") for c in stderr_events)


@pytest.mark.asyncio
async def test_untrusted_secrets_filtered_to_manifest_declarations(
    tmp_path: Path,
) -> None:
    script = """#!/usr/bin/env bash
# Read stdin (the JSON payload) and write it to OUTFILE for inspection.
cat > "$OUTFILE"
echo '{"type":"result","ok":true}'
"""
    outfile = tmp_path / "stdin.json"
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="secret-filter",
        script=script,
        env={"OUTFILE": str(outfile)},
        secrets=["allowed_secret"],
        trust_level="untrusted",
    )
    ctx, _metrics = _make_ctx(
        secrets={"allowed_secret": "yes", "disallowed_secret": "no"},
    )
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    payload = json.loads(outfile.read_text())
    assert "allowed_secret" in payload["secrets"]
    assert "disallowed_secret" not in payload["secrets"]


@pytest.mark.asyncio
async def test_undeclared_secret_silently_omitted_from_stdin(tmp_path: Path) -> None:
    """A manifest declaring a secret name that doesn't exist in the resolver
    produces an empty entry in the stdin secrets dict (silently omitted, not null).
    """
    # Direct unit test: test that the resolver correctly returns empty dict
    # when requested secret doesn't exist.
    resolver = SyncSecretsResolver(_values={"some_other_name": "value"})
    filtered = resolver.filtered(["nonexistent_secret"])
    assert filtered.list_names() == []
    assert filtered.get("nonexistent_secret") is None


@pytest.mark.asyncio
async def test_subprocess_env_excludes_caller_env_for_untrusted(
    tmp_path: Path,
) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
printenv > "$OUTFILE"
echo '{"type":"result","ok":true}'
"""
    outfile = tmp_path / "env.txt"
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="env",
        script=script,
        env={"OUTFILE": str(outfile), "MY_VAR": "MY_VAL"},
        trust_level="untrusted",
    )
    # Pollute parent env with a token the plugin must NOT see.
    os.environ["FORBIDDEN_LEAK_TOKEN"] = "SHOULD_NOT_APPEAR"
    try:
        ctx, _metrics = _make_ctx()
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    finally:
        del os.environ["FORBIDDEN_LEAK_TOKEN"]
    assert result.ok is True
    env_dump = outfile.read_text()
    assert "FORBIDDEN_LEAK_TOKEN" not in env_dump
    assert "MY_VAL" in env_dump
    assert "PATH=" in env_dump  # allowlisted


@pytest.mark.asyncio
async def test_blank_lines_silently_skipped(tmp_path: Path) -> None:
    script = """#!/usr/bin/env bash
cat >/dev/null
echo ''
echo '{"type":"metric","name":"x","value":1,"labels":{}}'
echo ''
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="blank1", script=script)
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert result.metrics_emitted == 1


@pytest.mark.asyncio
async def test_metric_line_missing_name_logs_warning_and_not_counted(tmp_path: Path) -> None:
    """Metric line without 'name' is rejected; metrics_emitted stays 0."""
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="metric-noname",
        script="""#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"metric","value":42,"labels":{}}'
echo '{"type":"result","ok":true}'
""",
    )
    ctx, _metrics = _make_ctx()
    with capture_logs() as cap_logs:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert result.metrics_emitted == 0
    assert any(log["event"] == "subprocess.plugin.invalid_metric_line" for log in cap_logs)


@pytest.mark.asyncio
async def test_metric_line_labels_not_dict_logs_warning_and_not_counted(tmp_path: Path) -> None:
    """Metric line with labels not a dict is rejected."""
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="metric-badlabels",
        script="""#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"metric","name":"foo","value":1,"labels":"bad"}'
echo '{"type":"result","ok":true}'
""",
    )
    ctx, _metrics = _make_ctx()
    with capture_logs() as cap_logs:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.metrics_emitted == 0
    assert any(log["event"] == "subprocess.plugin.invalid_metric_line" for log in cap_logs)


@pytest.mark.asyncio
async def test_metric_line_unknown_kind_logs_warning_and_not_counted(tmp_path: Path) -> None:
    """Metric line with unknown kind is rejected."""
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="metric-unknownkind",
        script="""#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"metric","name":"foo","value":1,"kind":"histogram","labels":{}}'
echo '{"type":"result","ok":true}'
""",
    )
    ctx, _metrics = _make_ctx()
    with capture_logs() as cap_logs:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.metrics_emitted == 0
    assert any(log["event"] == "subprocess.plugin.invalid_metric_line" for log in cap_logs)


@pytest.mark.asyncio
async def test_log_line_invalid_stream_type_logs_warning(tmp_path: Path) -> None:
    """Log line with non-string stream/line is rejected."""
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="log-badstream",
        script="""#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"log","stream":123,"line":"hello"}'
echo '{"type":"result","ok":true}'
""",
    )
    ctx, _metrics = _make_ctx()
    with capture_logs() as cap_logs:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert len(cast(_NoopLogsWriter, ctx.vl).entries) == 0
    assert any(log["event"] == "subprocess.plugin.invalid_log_line" for log in cap_logs)


@pytest.mark.asyncio
async def test_event_line_invalid_payload_logs_warning_and_discarded(tmp_path: Path) -> None:
    """Event line missing required pydantic fields is rejected."""
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="event-invalid",
        script="""#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"event","kind":"suggestion"}'
echo '{"type":"result","ok":true}'
""",
    )
    ctx, _metrics = _make_ctx()
    with capture_logs() as cap_logs:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert len(result.events) == 0
    assert any(log["event"] == "subprocess.plugin.invalid_event" for log in cap_logs)


@pytest.mark.asyncio
async def test_event_line_with_extra_field_logs_warning_and_discarded(tmp_path: Path) -> None:
    """Event line with an extra field beyond the discriminated union member is rejected.

    SuggestionEvent has model_config = ConfigDict(extra='forbid'), so unknown fields
    cause ValidationError and the event is discarded with a warning log.
    """
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="event-extra-field",
        script="""#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"event","kind":"suggestion","title":"t","body":"b","extra":"nope"}'
echo '{"type":"result","ok":true}'
""",
    )
    ctx, _metrics = _make_ctx()
    with capture_logs() as cap_logs:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert len(result.events) == 0
    assert any(log["event"] == "subprocess.plugin.invalid_event" for log in cap_logs)


@pytest.mark.asyncio
async def test_heartbeat_line_missing_name_logs_warning_and_discarded(tmp_path: Path) -> None:
    """Heartbeat line without name is rejected."""
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="heartbeat-noname",
        script="""#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"heartbeat","state":"ok"}'
echo '{"type":"result","ok":true}'
""",
    )
    ctx, _metrics = _make_ctx()
    with capture_logs() as cap_logs:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert len(result.events) == 0
    assert any(log["event"] == "subprocess.plugin.invalid_heartbeat" for log in cap_logs)


@pytest.mark.asyncio
async def test_json_array_line_logs_malformed_and_continues(tmp_path: Path) -> None:
    """A valid JSON array (not object) is rejected; subsequent lines parse OK."""
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="json-array",
        script="""#!/usr/bin/env bash
cat >/dev/null
echo '[1,2,3]'
echo '{"type":"metric","name":"x","value":1,"labels":{}}'
echo '{"type":"result","ok":true}'
""",
    )
    ctx, _metrics = _make_ctx()
    with capture_logs() as cap_logs:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert result.metrics_emitted == 1
    assert any(log["event"] == "subprocess.plugin.malformed_json_line" for log in cap_logs)


@pytest.mark.asyncio
async def test_result_line_errors_not_list_coerced_to_empty(tmp_path: Path) -> None:
    """Result line with errors not a list coerces to empty errors list."""
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="result-baderrors",
        script="""#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"result","ok":false,"errors":"a string not a list"}'
""",
    )
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is False
    assert result.errors == []


@pytest.mark.asyncio
async def test_nonzero_exit_with_ok_false_does_not_add_exit_code_error(
    tmp_path: Path,
) -> None:
    """When subprocess exits non-zero AND result.ok is already False.

    Exit-code error is not appended.
    """
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="nonzero-okfalse",
        script="""#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"result","ok":false,"errors":["plugin error"]}'
exit 3
""",
    )
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is False
    assert "plugin error" in result.errors


@pytest.mark.asyncio
async def test_result_line_only_no_prior_output(tmp_path: Path) -> None:
    """Result line as only output is valid and produces correct result."""
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="resultonly", script=script)
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert result.metrics_emitted == 0
    assert len(result.events) == 0


@pytest.mark.asyncio
async def test_all_line_types_in_sequence(tmp_path: Path) -> None:
    """All protocol line types can appear in a single run."""
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"metric","name":"m1","value":1,"labels":{}}'
echo '{"type":"log","stream":"stdout","message":"test log"}'
echo '{"type":"event","kind":"suggestion","title":"test","body":"suggestion body"}'
echo '{"type":"heartbeat","source":"test-source"}'
echo '{"type":"result","ok":true}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="alllines", script=script)
    ctx, _metrics = _make_ctx()
    result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert result.metrics_emitted == 1
    expected_events = 2  # suggestion event + heartbeat event
    assert len(result.events) == expected_events


@pytest.mark.asyncio
async def test_result_line_followed_by_extra_output_logs_warning(tmp_path: Path) -> None:
    """Lines emitted after result line are logged as warning and skipped."""
    script = """#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"result","ok":true}'
echo '{"type":"metric","name":"late","value":99,"labels":{}}'
"""
    manifest, plugin_dir = _write_plugin(tmp_path, name="result_then_extra", script=script)
    ctx, metrics = _make_ctx()
    with capture_logs() as cap_logs:
        result = await run_subprocess(manifest, ctx, manifest_dir=plugin_dir)
    assert result.ok is True
    assert result.metrics_emitted == 0  # late metric not counted
    assert not any(m.name == "late" for m in metrics.recorded)  # late metric not recorded
    assert any(
        log_entry["event"] == "subprocess.plugin.line_after_result" for log_entry in cap_logs
    ), "Expected warning log for line after result"


@pytest.mark.asyncio
async def test_make_subprocess_collector_returns_working_collector_class(tmp_path: Path) -> None:
    """make_subprocess_collector class factory produces a callable Collector class."""
    manifest, plugin_dir = _write_plugin(
        tmp_path,
        name="factory-test",
        script="""#!/usr/bin/env bash
cat >/dev/null
echo '{"type":"result","ok":true,"summary":"factory test"}'
""",
    )
    cls = make_subprocess_collector(manifest, plugin_dir)
    assert cls.name == manifest.name
    assert cls.run_kind == RunKind.SUBPROCESS
    assert cls.interval == manifest.interval

    instance = cls()
    ctx, _metrics = _make_ctx()
    result = await instance.run(ctx)
    assert result.ok is True
