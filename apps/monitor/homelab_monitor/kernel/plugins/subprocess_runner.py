"""Subprocess plugin runner.

Spawns a subprocess for the given manifest, streams stdout JSON lines,
captures stderr to structured logs, enforces timeout via SIGTERM/SIGKILL,
and returns a CollectorResult.

Spec: docs/superpowers/specs/2026-05-04-homelab-monitor-design.md §5.3.

Design selections (STAGE-009):
  D2: DB writes blocked architecturally — protocol has no DB-write line types.
  D3: Hybrid context delivery — manifest.env -> environment vars; secrets +
      runtime metadata -> JSON object on stdin (write-and-close-EOF).
  D4: Strict line-by-line stdout parsing; malformed/unknown lines are
      logged-and-discarded uniformly.
  D5: Concurrent stderr drain task; each line at info level. Single
      summary warning on non-zero exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from pathlib import Path
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.manifest import SubprocessManifest
from homelab_monitor.kernel.plugins.types import (
    CollectorEvent,
    CollectorResult,
    HeartbeatEvent,
)

SIGTERM_GRACE_SECONDS = 2.0

_PROTOCOL_LINE_TYPES = frozenset({"metric", "event", "log", "heartbeat", "result"})

# Allowlisted env vars passed through to all subprocess plugins regardless
# of trust tier. Anything else from the parent process is scrubbed.
_ENV_ALLOWLIST = ("PATH", "TZ")


def _build_subprocess_env(manifest: SubprocessManifest) -> dict[str, str]:
    """Build a clean environment dict for the subprocess.

    Both trust tiers (TRUSTED + UNTRUSTED) get the same scrubbing for
    STAGE-009: only the manifest's declared env keys + the allowlist
    (PATH, TZ) + a synthesized HOME=/tmp/<plugin-name>. If divergence
    between tiers becomes necessary later, change here only.
    """
    env: dict[str, str] = {}
    parent = os.environ
    for key in _ENV_ALLOWLIST:
        if key in parent:
            env[key] = parent[key]
    env["HOME"] = f"/tmp/{manifest.name}"
    # Manifest-declared env keys (these win over the allowlist on conflict).
    for k, v in manifest.env.items():
        env[k] = v
    return env


def _build_stdin_payload(
    *,
    config_name: str,
    deadline_unix: float,
    secrets_dict: dict[str, str],
) -> str:
    """Build the JSON config blob written to subprocess stdin.

    Schema:
      {
        "collector_name": "<config name>",
        "deadline_unix": <float>,
        "secrets": {<name>: <value>, ...}   # already filtered to manifest declarations
      }
    """
    payload = {
        "collector_name": config_name,
        "deadline_unix": deadline_unix,
        "secrets": secrets_dict,
    }
    return json.dumps(payload, separators=(",", ":"))


async def _drain_stderr(
    proc: asyncio.subprocess.Process,
    log: BoundLogger,
    collector_name: str,
) -> None:
    """Concurrent task: read stderr line-by-line and log at info level."""
    assert proc.stderr is not None  # PIPE configured in create_subprocess_exec
    while True:
        line = await proc.stderr.readline()
        if not line:
            return
        log.info(
            "subprocess.plugin.stderr",
            collector=collector_name,
            line=line.decode("utf-8", errors="replace").rstrip("\n"),
        )


def _write_metric(
    ctx: CollectorContext,
    payload: dict[str, Any],
    log: BoundLogger,
    collector_name: str,
    line_number: int,
) -> bool:
    """Dispatch a metric-line payload to ctx.vm. Returns True on success."""
    name = payload.get("name")
    value = payload.get("value")
    labels_raw = payload.get("labels", {})
    kind = payload.get("kind", "counter")
    if not isinstance(name, str) or not isinstance(value, (int, float)):
        log.warning(
            "subprocess.plugin.invalid_metric_line",
            collector=collector_name,
            line_number=line_number,
            reason="name must be str and value must be number",
        )
        return False
    if not isinstance(labels_raw, dict):
        log.warning(
            "subprocess.plugin.invalid_metric_line",
            collector=collector_name,
            line_number=line_number,
            reason="labels must be a dict",
        )
        return False
    labels: dict[str, str] = {
        str(k): str(v) for k, v in cast(dict[str, object], labels_raw).items()
    }
    # SCAFFOLDING: STAGE-010+ may add Prometheus-style ^[a-zA-Z_:][a-zA-Z0-9_:]*$
    # enforcement on metric names to prevent untrusted plugins from emitting malformed
    # names that confuse vmalert. Currently any non-empty string is accepted.
    if kind == "counter":
        ctx.vm.write_counter(name, float(value), labels=labels)
    elif kind == "gauge":
        ctx.vm.write_gauge(name, float(value), labels=labels)
    elif kind == "summary":
        ctx.vm.write_summary(name, float(value), labels=labels)
    else:
        log.warning(
            "subprocess.plugin.invalid_metric_line",
            collector=collector_name,
            line_number=line_number,
            reason=f"unknown metric kind: {kind!r}",
        )
        return False
    return True


def _write_log(
    ctx: CollectorContext,
    payload: dict[str, Any],
    log: BoundLogger,
    collector_name: str,
    line_number: int,
) -> None:
    """Dispatch a log-line payload to ctx.vl."""
    stream = payload.get("stream", collector_name)
    line = payload.get("line", "")
    if not isinstance(stream, str) or not isinstance(line, str):
        log.warning(
            "subprocess.plugin.invalid_log_line",
            collector=collector_name,
            line_number=line_number,
            reason="stream and line must be strings",
        )
        return
    ctx.vl.ingest(stream=stream, line=line)


_EVENT_ADAPTER: TypeAdapter[CollectorEvent] = TypeAdapter(CollectorEvent)


def _parse_event(
    payload: dict[str, Any],
    log: BoundLogger,
    collector_name: str,
    line_number: int,
) -> CollectorEvent | None:
    """Validate an event-line payload via the CollectorEvent discriminated union."""
    # Strip the protocol "type" wrapper, the event payload IS the event dict.
    event_payload = {k: v for k, v in payload.items() if k != "type"}
    # The CollectorEvent union uses its own internal "kind" discriminator;
    # the protocol's outer "type" was just "event".
    try:
        return _EVENT_ADAPTER.validate_python(event_payload)
    except ValidationError as e:
        log.warning(
            "subprocess.plugin.invalid_event",
            collector=collector_name,
            line_number=line_number,
            error=str(e),
        )
        return None


def _parse_heartbeat(
    payload: dict[str, Any],
    log: BoundLogger,
    collector_name: str,
    line_number: int,
) -> HeartbeatEvent | None:
    """Convert a heartbeat-line payload into a HeartbeatEvent.

    SCAFFOLDING: STAGE-002-* will route heartbeats to the real heartbeat
    receiver. For STAGE-009, append as a HeartbeatEvent so it flows through
    the existing CollectorResult.events list.
    """
    name = payload.get("source")
    if not isinstance(name, str):
        log.warning(
            "subprocess.plugin.invalid_heartbeat",
            collector=collector_name,
            line_number=line_number,
            reason="source must be a string",
        )
        return None
    try:
        return HeartbeatEvent(name=name, state="ok")
    except ValidationError as e:  # pragma: no cover
        # name already str-validated; hard-coded state="ok" cannot fail validation
        log.warning(
            "subprocess.plugin.invalid_heartbeat",
            collector=collector_name,
            line_number=line_number,
            error=str(e),
        )
        return None


async def _drain_stdout(  # noqa: PLR0912, PLR0915 -- 5 protocol line types + 4 error paths (malformed JSON, unknown type, line-after-result, EOF) require parallel branches; splitting hurts readability
    proc: asyncio.subprocess.Process,
    ctx: CollectorContext,
    log: BoundLogger,
    collector_name: str,
) -> CollectorResult:
    """Concurrent task: parse stdout JSON lines, dispatch to writers.

    Returns the CollectorResult derived from the `result` line, OR a
    synthesized failure result if the subprocess exited without emitting one.
    """
    assert proc.stdout is not None  # PIPE configured in create_subprocess_exec
    metrics_emitted = 0
    events: list[CollectorEvent] = []
    result: CollectorResult | None = None
    line_number = 0

    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line_number += 1
        line_str = raw.decode("utf-8", errors="replace").rstrip("\n")
        if not line_str.strip():
            continue  # silent skip on blank lines

        try:
            payload = json.loads(line_str)
        except json.JSONDecodeError as e:
            log.warning(
                "subprocess.plugin.malformed_json_line",
                collector=collector_name,
                line_number=line_number,
                raw_line=line_str[:200],
                parse_error=str(e),
            )
            continue

        if not isinstance(payload, dict):
            log.warning(
                "subprocess.plugin.malformed_json_line",
                collector=collector_name,
                line_number=line_number,
                raw_line=line_str[:200],
                parse_error="line is not a JSON object",
            )
            continue

        payload = cast(dict[str, Any], payload)

        line_type = payload.get("type")
        if result is not None:
            log.warning(
                "subprocess.plugin.line_after_result",
                collector=collector_name,
                line_number=line_number,
                line_type=line_type,
            )
            continue

        if line_type not in _PROTOCOL_LINE_TYPES:
            log.info(
                "subprocess.plugin.unknown_line_type",
                collector=collector_name,
                line_number=line_number,
                line_type=line_type,
            )
            continue

        if line_type == "metric":
            if _write_metric(ctx, payload, log, collector_name, line_number):
                metrics_emitted += 1
        elif line_type == "log":
            _write_log(ctx, payload, log, collector_name, line_number)
        elif line_type == "event":
            event = _parse_event(payload, log, collector_name, line_number)
            if event is not None:
                events.append(event)
        elif line_type == "heartbeat":
            heartbeat = _parse_heartbeat(payload, log, collector_name, line_number)
            if heartbeat is not None:
                events.append(heartbeat)
        elif line_type == "result":  # pragma: no branch
            ok = bool(payload.get("ok", False))
            errors_raw = payload.get("errors", [])
            if not isinstance(errors_raw, list):
                errors_raw = []
            errors_list: list[str] = [str(e) for e in cast(list[object], errors_raw)]
            result = CollectorResult(
                ok=ok,
                metrics_emitted=metrics_emitted,
                errors=errors_list,
                events=events,
                duration_seconds=0.0,  # filled in by caller
            )

    if result is None:
        return CollectorResult(
            ok=False,
            metrics_emitted=metrics_emitted,
            errors=["no result line emitted"],
            events=events,
            duration_seconds=0.0,
        )
    return result


async def run_subprocess(  # noqa: PLR0915 -- spawn + stdin write + concurrent stdout/stderr drain + timeout escalation + exception handling all need to live in one async flow; splitting reduces clarity of cleanup ordering
    manifest: SubprocessManifest,
    ctx: CollectorContext,
    *,
    manifest_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> CollectorResult:
    """Spawn the subprocess, supervise it, return CollectorResult.

    Args:
        manifest: Validated plugin manifest.
        ctx: Per-tick CollectorContext (provides vm, vl, log, secrets).
        manifest_dir: Directory the manifest lives in (used as default workdir).

    Returns:
        CollectorResult derived from the subprocess's `result` line, OR a
        failure result if the subprocess died, timed out, or exited without
        emitting a result.
    """
    log: BoundLogger = cast(BoundLogger, ctx.log.bind(collector=manifest.name))
    cwd = Path(manifest.workdir) if manifest.workdir else manifest_dir
    env = _build_subprocess_env(manifest)
    if extra_env:
        env.update(extra_env)
    secrets_dict = ctx.secrets.filtered(manifest.secrets).as_dict()
    stdin_payload = _build_stdin_payload(
        config_name=ctx.config.name,
        deadline_unix=time.time() + manifest.timeout.total_seconds(),
        secrets_dict=secrets_dict,
    )

    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *manifest.command,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # POSIX process group for signal targeting
        )
    except (FileNotFoundError, PermissionError, OSError) as e:
        log.error("subprocess.plugin.spawn_failed", error=str(e))
        return CollectorResult(
            ok=False,
            metrics_emitted=0,
            errors=[f"subprocess spawn failed: {e}"],
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    # Write stdin payload + close (signals EOF to plugin).
    assert proc.stdin is not None  # PIPE configured in create_subprocess_exec
    try:
        proc.stdin.write(stdin_payload.encode("utf-8") + b"\n")
        await proc.stdin.drain()
        proc.stdin.close()
    except (
        BrokenPipeError,
        ConnectionResetError,
    ) as e:  # pragma: no cover
        # race: subprocess closes stdin before parent write completes; not reliably reproducible
        log.warning("subprocess.plugin.stdin_write_failed", error=str(e))

    stdout_task: asyncio.Task[CollectorResult] = asyncio.create_task(
        _drain_stdout(proc, ctx, log, manifest.name)
    )
    stderr_task: asyncio.Task[None] = asyncio.create_task(_drain_stderr(proc, log, manifest.name))

    timed_out = False
    try:
        async with asyncio.timeout(manifest.timeout.total_seconds()):
            exit_code = await proc.wait()
    except TimeoutError:
        timed_out = True
        log.warning(
            "subprocess.plugin.timeout",
            timeout=manifest.timeout.total_seconds(),
        )
        # SIGTERM the process group; wait grace; SIGKILL.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            async with asyncio.timeout(SIGTERM_GRACE_SECONDS):
                exit_code = await proc.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            exit_code = await proc.wait()

    # Wait for stdout_task with timeout; cancel if hung; convert any exception
    # to a synthesized failure result rather than re-raising to caller.
    try:
        async with asyncio.timeout(1.0):
            result = await stdout_task
    except (
        TimeoutError
    ):  # pragma: no cover -- stdout drain should never hang past 1s grace; defensive cancel path
        log.warning("subprocess.plugin.stdout_drain_timeout", collector=manifest.name)
        stdout_task.cancel()
        with contextlib.suppress(BaseException):
            await stdout_task
        result = CollectorResult(
            ok=False,
            metrics_emitted=0,
            errors=["stdout drain timeout"],
            events=[],
            duration_seconds=0.0,
        )
    except Exception as e:  # pragma: no cover
        # Defensive: drain task should not raise; convert to failure
        # rather than crash kernel.
        log.exception(
            "subprocess.plugin.stdout_drain_crashed", collector=manifest.name, error=str(e)
        )
        result = CollectorResult(
            ok=False,
            metrics_emitted=0,
            errors=[f"stdout drain crashed: {e}"],
            events=[],
            duration_seconds=0.0,
        )

    # Drain stderr with timeout; warn if truncated.
    try:
        async with asyncio.timeout(1.0):
            await stderr_task
    except (
        TimeoutError
    ):  # pragma: no cover -- stderr drain should never hang past 1s grace; defensive cancel path
        log.warning("subprocess.plugin.stderr_drain_timeout", collector=manifest.name)
        stderr_task.cancel()
        with contextlib.suppress(BaseException):
            await stderr_task

    if timed_out:
        return CollectorResult(
            ok=False,
            metrics_emitted=0,
            errors=[f"timeout after {manifest.timeout.total_seconds()}s"],
            events=[],
            duration_seconds=time.monotonic() - start,
        )

    if exit_code != 0:
        log.warning(
            "subprocess.plugin.nonzero_exit",
            exit_code=exit_code,
            duration_seconds=time.monotonic() - start,
        )
        if result.ok:
            result = CollectorResult(
                ok=False,
                metrics_emitted=result.metrics_emitted,
                errors=[*result.errors, f"non-zero exit code: {exit_code}"],
                events=result.events,
                duration_seconds=time.monotonic() - start,
            )

    return CollectorResult(
        ok=result.ok,
        metrics_emitted=result.metrics_emitted,
        errors=result.errors,
        events=result.events,
        duration_seconds=time.monotonic() - start,
    )


__all__ = ["SIGTERM_GRACE_SECONDS", "run_subprocess"]
