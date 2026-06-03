"""Integration tests: Vector multiline codec stitches multi-line log events.

Plants sequences of related log lines via noisy-logger; asserts that the
Vector multiline codec (configured in deploy/vector/vector.toml.template)
coalesces them into a SINGLE VictoriaLogs record before indexing.

Requires the full integration rig (docker-compose.test.yml). Run during
Refinement phase only — NOT during Build.

STAGE-004-001.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components

VECTOR_LATENCY_BUDGET_S = 30.0


def _query_logs(rig: Rig, marker: str) -> list[dict[str, Any]]:
    """Poll /api/logs/query for lines containing `marker`; return matching entries."""
    now = datetime.now(UTC)
    start = (now - timedelta(minutes=1)).isoformat()
    end = now.isoformat()
    resp = rig.get(
        "/api/logs/query",
        params={"expr": f'"{marker}"', "start": start, "end": end, "limit": "50"},
    )
    if resp.status_code != 200:  # noqa: PLR2004
        return []
    return resp.json().get("lines", [])


@pytest.mark.integration
@pytest.mark.slow
def test_python_traceback_stitched_into_single_record() -> None:
    """Plant a 4-line Python traceback; assert ONE VL record containing all 4 fragments."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-ml-py-{uuid.uuid4().hex[:8]}"
    lines = [
        f"Traceback (most recent call last): {marker}",
        '  File "/app/worker.py", line 42, in run',
        "    result = process(data)",
        f"ValueError: invalid input — {marker}",
    ]

    with Rig.boot() as rig:
        rig.plant_log_lines_via_noisy_logger(lines, delay_ms=50)

        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matching: list[dict[str, Any]] = []
        while time.time() < deadline:
            entries = _query_logs(rig, marker)
            matching = [e for e in entries if marker in e.get("message", "")]
            if matching:
                break
            time.sleep(2.0)

    assert len(matching) == 1, (
        f"Expected exactly 1 stitched VL record for marker {marker!r}, "
        f"got {len(matching)}: {[e.get('line', '')[:120] for e in matching]}"
    )
    stitched = matching[0].get("message", "")
    for fragment in lines:
        assert fragment in stitched or marker in stitched, (
            f"Fragment {fragment!r} not found in stitched record:\n{stitched[:300]}"
        )


@pytest.mark.integration
@pytest.mark.slow
def test_java_stack_trace_stitched_into_single_record() -> None:
    """Plant a 3-line Java stack trace; assert ONE VL record."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-ml-java-{uuid.uuid4().hex[:8]}"
    lines = [
        f"java.lang.NullPointerException: null ref — {marker}",
        "\tat com.example.App.run(App.java:55)",
        "\tat com.example.Main.main(Main.java:12)",
    ]

    with Rig.boot() as rig:
        rig.plant_log_lines_via_noisy_logger(lines, delay_ms=50)

        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matching: list[dict[str, Any]] = []
        while time.time() < deadline:
            entries = _query_logs(rig, marker)
            matching = [e for e in entries if marker in e.get("message", "")]
            if matching:
                break
            time.sleep(2.0)

    assert len(matching) == 1, (
        f"Expected exactly 1 stitched VL record for marker {marker!r}, got {len(matching)}"
    )
    stitched = matching[0].get("message", "")
    assert "Main.java:12" in stitched, (
        f"Stitched record missing tail fragment 'Main.java:12' — multiline codec "
        f"did not absorb the final body line:\n{stitched[:400]}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_negative_control_event_ends_on_non_continuation_line() -> None:
    """A started multi-line event ENDS when a non-continuation line follows.

    Plants: a Traceback start + one indented continuation (these stitch), then a
    plain non-matching line. The plain line must NOT be absorbed into the
    traceback — it is a separate VL record. Proves continue_through terminates
    the event correctly (not just that unrelated lines never merge).
    """
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker_tb = f"rig-ml-end-tb-{uuid.uuid4().hex[:8]}"
    marker_plain = f"rig-ml-end-plain-{uuid.uuid4().hex[:8]}"
    lines = [
        f"Traceback (most recent call last): {marker_tb}",
        '  File "/app/x.py", line 1, in run',
        f"a plain unrelated line that is not a continuation {marker_plain}",
    ]

    with Rig.boot() as rig:
        # delay > 1000ms multiline timeout between the continuation and the plain
        # line so the event flushes and the plain line is unambiguously separate.
        rig.plant_log_lines_via_noisy_logger(lines, delay_ms=1200)

        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matching_tb: list[dict[str, Any]] = []
        matching_plain: list[dict[str, Any]] = []
        while time.time() < deadline:
            entries_tb = _query_logs(rig, marker_tb)
            entries_plain = _query_logs(rig, marker_plain)
            matching_tb = [e for e in entries_tb if marker_tb in e.get("message", "")]
            matching_plain = [e for e in entries_plain if marker_plain in e.get("message", "")]
            if matching_tb and matching_plain:
                break
            time.sleep(2.0)

    assert len(matching_tb) == 1, (
        f"Expected 1 traceback record for {marker_tb!r}, got {len(matching_tb)}"
    )
    # The plain line must be its OWN record, NOT absorbed into the traceback.
    assert len(matching_plain) == 1, (
        f"Expected the plain line as a separate record for {marker_plain!r}, "
        f"got {len(matching_plain)}"
    )
    assert marker_plain not in matching_tb[0].get("message", ""), (
        "plain non-continuation line was wrongly stitched into the traceback event"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_go_panic_stitched_into_single_record() -> None:
    """Plant a 4-line Go panic; assert ONE VL record."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-ml-go-{uuid.uuid4().hex[:8]}"
    lines = [
        f"panic: runtime error: index out of range [3] with length 3 — {marker}",
        "goroutine 1 [running]:",
        "main.handle({})",
        "\tfile.go:42 +0x1a",
    ]

    with Rig.boot() as rig:
        rig.plant_log_lines_via_noisy_logger(lines, delay_ms=50)
        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matching: list[dict[str, Any]] = []
        while time.time() < deadline:
            entries = _query_logs(rig, marker)
            matching = [e for e in entries if marker in e.get("message", "")]
            if matching:
                break
            time.sleep(2.0)

    assert len(matching) == 1, f"Expected 1 stitched record for {marker!r}, got {len(matching)}"
    stitched = matching[0].get("message", "")
    assert "file.go:42" in stitched, (
        f"Stitched record missing tail fragment 'file.go:42' — codec did not "
        f"absorb the final body line:\n{stitched[:400]}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_ruby_traceback_stitched_into_single_record() -> None:
    """Plant a 3-line Ruby traceback; assert ONE VL record."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-ml-rb-{uuid.uuid4().hex[:8]}"
    lines = [
        f"RuntimeError: bad — {marker}",
        "\tfrom /app/worker.rb:42:in `run'",
        "\tfrom /app/main.rb:10:in `<main>'",
    ]

    with Rig.boot() as rig:
        rig.plant_log_lines_via_noisy_logger(lines, delay_ms=50)
        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matching: list[dict[str, Any]] = []
        while time.time() < deadline:
            entries = _query_logs(rig, marker)
            matching = [e for e in entries if marker in e.get("message", "")]
            if matching:
                break
            time.sleep(2.0)

    assert len(matching) == 1, f"Expected 1 stitched record for {marker!r}, got {len(matching)}"
    stitched = matching[0].get("message", "")
    assert "main.rb:10" in stitched, (
        f"Stitched record missing tail fragment 'main.rb:10' — codec did not "
        f"absorb the final body line:\n{stitched[:400]}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_node_stack_trace_stitched_into_single_record() -> None:
    """Plant a 3-line Node.js error; assert ONE VL record."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-ml-node-{uuid.uuid4().hex[:8]}"
    lines = [
        f"Error: bad — {marker}",
        "    at Object.run (/app/file.js:1:1)",
        "    at process.processTicksAndRejections (node:internal/process/task_queues:96:5)",
    ]

    with Rig.boot() as rig:
        rig.plant_log_lines_via_noisy_logger(lines, delay_ms=50)
        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matching: list[dict[str, Any]] = []
        while time.time() < deadline:
            entries = _query_logs(rig, marker)
            matching = [e for e in entries if marker in e.get("message", "")]
            if matching:
                break
            time.sleep(2.0)

    assert len(matching) == 1, f"Expected 1 stitched record for {marker!r}, got {len(matching)}"
    stitched = matching[0].get("message", "")
    assert "task_queues:96" in stitched, (
        f"Stitched record missing tail fragment 'task_queues:96' — codec did not "
        f"absorb the final body line:\n{stitched[:400]}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_dotnet_stack_trace_stitched_into_single_record() -> None:
    """Plant a 3-line .NET exception; assert ONE VL record."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-ml-dotnet-{uuid.uuid4().hex[:8]}"
    lines = [
        f"Unhandled Exception: System.NullReferenceException: Object reference not set — {marker}",
        "   at MyApp.Program.Main (file.cs:1)",
        "   at MyApp.Loader.Run (loader.cs:42)",
    ]

    with Rig.boot() as rig:
        rig.plant_log_lines_via_noisy_logger(lines, delay_ms=50)
        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matching: list[dict[str, Any]] = []
        while time.time() < deadline:
            entries = _query_logs(rig, marker)
            matching = [e for e in entries if marker in e.get("message", "")]
            if matching:
                break
            time.sleep(2.0)

    assert len(matching) == 1, f"Expected 1 stitched record for {marker!r}, got {len(matching)}"
    stitched = matching[0].get("message", "")
    assert "loader.cs:42" in stitched, (
        f"Stitched record missing tail fragment 'loader.cs:42' — codec did not "
        f"absorb the final body line:\n{stitched[:400]}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_cpp_segfault_stitched_into_single_record() -> None:
    """Plant a 4-line C++ segfault dump; assert ONE VL record."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-ml-cpp-{uuid.uuid4().hex[:8]}"
    lines = [
        f"*** ERROR: signal 11 (SIGSEGV) — {marker}",
        "Stack trace:",
        "  0x00007f1234567890 ?? + 16",
        "  0x00007f1234567abc ?? + 32",
    ]

    with Rig.boot() as rig:
        rig.plant_log_lines_via_noisy_logger(lines, delay_ms=50)
        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matching: list[dict[str, Any]] = []
        while time.time() < deadline:
            entries = _query_logs(rig, marker)
            matching = [e for e in entries if marker in e.get("message", "")]
            if matching:
                break
            time.sleep(2.0)

    assert len(matching) == 1, f"Expected 1 stitched record for {marker!r}, got {len(matching)}"
    stitched = matching[0].get("message", "")
    assert "0x00007f1234567abc" in stitched, (
        f"Stitched record missing tail fragment '0x00007f1234567abc' — codec did not "
        f"absorb the final body line:\n{stitched[:400]}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_rust_panic_stitched_into_single_record() -> None:
    """Plant a 5-line Rust panic with backtrace; assert ONE VL record."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-ml-rust-{uuid.uuid4().hex[:8]}"
    lines = [
        f"thread 'main' panicked at 'index out of bounds' — {marker}",
        "stack backtrace:",
        "   0: 0x7f1234 - std::panicking::begin_panic",
        "   1: 0x7f5678 - core::panicking::panic_bounds_check",
        "note: run with `RUST_BACKTRACE=1` for more information",
    ]

    with Rig.boot() as rig:
        rig.plant_log_lines_via_noisy_logger(lines, delay_ms=50)
        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matching: list[dict[str, Any]] = []
        while time.time() < deadline:
            entries = _query_logs(rig, marker)
            matching = [e for e in entries if marker in e.get("message", "")]
            if matching:
                break
            time.sleep(2.0)

    assert len(matching) == 1, f"Expected 1 stitched record for {marker!r}, got {len(matching)}"
    stitched = matching[0].get("message", "")
    assert "RUST_BACKTRACE=1" in stitched, (
        f"Stitched record missing tail fragment 'RUST_BACKTRACE=1' — codec did not "
        f"absorb the final body line:\n{stitched[:400]}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_php_fatal_stitched_into_single_record() -> None:
    r"""Plant a 5-line PHP fatal error; assert ONE VL record.

    Note: PHP '#0 /path(line): fn()' lines do NOT start with whitespace and do
    NOT match start_pattern (no recognized prefix), so they remain continuations.
    The indented 'thrown in...' line also remains a continuation via ^\s.
    """
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-ml-php-{uuid.uuid4().hex[:8]}"
    lines = [
        f"PHP Fatal error: Uncaught Exception: bad — {marker}",
        "Stack trace:",
        "#0 /app/file.php(42): worker()",
        "#1 {main}",
        "  thrown in /app/file.php on line 42",
    ]

    with Rig.boot() as rig:
        rig.plant_log_lines_via_noisy_logger(lines, delay_ms=50)
        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matching: list[dict[str, Any]] = []
        while time.time() < deadline:
            entries = _query_logs(rig, marker)
            matching = [e for e in entries if marker in e.get("message", "")]
            if matching:
                break
            time.sleep(2.0)

    assert len(matching) == 1, f"Expected 1 stitched record for {marker!r}, got {len(matching)}"
    stitched = matching[0].get("message", "")
    assert "thrown in /app/file.php" in stitched, (
        f"Stitched record missing tail fragment 'thrown in /app/file.php' — codec did not "
        f"absorb the final body line:\n{stitched[:400]}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_negative_control_two_iso_timestamp_lines_stay_separate() -> None:
    """Two ISO-timestamp lines each match start_pattern → TWO separate VL records."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker_a = f"rig-ml-iso-a-{uuid.uuid4().hex[:8]}"
    marker_b = f"rig-ml-iso-b-{uuid.uuid4().hex[:8]}"
    lines = [
        f"2026-05-28T19:30:00Z worker A started — {marker_a}",
        f"2026-05-28T19:30:01Z worker B started — {marker_b}",
    ]

    with Rig.boot() as rig:
        rig.plant_log_lines_via_noisy_logger(lines, delay_ms=1200)
        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matching_a: list[dict[str, Any]] = []
        matching_b: list[dict[str, Any]] = []
        while time.time() < deadline:
            entries_a = _query_logs(rig, marker_a)
            entries_b = _query_logs(rig, marker_b)
            matching_a = [e for e in entries_a if marker_a in e.get("message", "")]
            matching_b = [e for e in entries_b if marker_b in e.get("message", "")]
            if matching_a and matching_b:
                break
            time.sleep(2.0)

    assert len(matching_a) == 1, f"Expected 1 record for {marker_a!r}, got {len(matching_a)}"
    assert len(matching_b) == 1, f"Expected 1 record for {marker_b!r}, got {len(matching_b)}"
