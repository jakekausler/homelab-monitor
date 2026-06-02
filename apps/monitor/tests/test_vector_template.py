"""Tests for deploy/vector/vector.toml.template (STAGE-002-012).

Validates structural requirements of the Vector config template without needing
the `vector` binary. Uses tomllib to parse (after substituting the one token
placeholder) and asserts that the new hmrun branch is present and that the
drop_noise condition excludes hmrun lines.

Limitation: this is a structural/TOML-parse check only. Full Vector semantic
validation (type-correct inputs/outputs, routing correctness) is performed at
prod-rig refinement (3b) with a live `vector validate` run.
"""

from __future__ import annotations

import re as _re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import regex as _regex

from homelab_monitor.kernel.logs.models import (
    _CANONICAL_SEVERITIES,  # pyright: ignore[reportPrivateUsage]
)

_TEMPLATE_PATH = Path(__file__).parents[3] / "deploy" / "vector" / "vector.toml.template"
_MULTILINE_TIMEOUT_MS = 1000
_EXPECTED_SEVERITY_PATTERNS = 7  # emergency, alert, critical, error, warn, notice, debug


def _render_template(docker_exclude: str = "[]") -> str:
    """Substitute template placeholders with dummy values."""
    from homelab_monitor.kernel.config import DEFAULT_REDACT_PATTERNS  # noqa: PLC0415
    from homelab_monitor.kernel.cron.render import (  # noqa: PLC0415
        build_redact_metric_entries,
        build_redact_strip_markers,
        build_redact_vrl,
    )

    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    text = text.replace("${CRON_EVENTS_INGEST_TOKEN}", "dummy-token-for-test")
    text = text.replace("${VECTOR_DOCKER_EXCLUDE}", docker_exclude)
    pats = list(DEFAULT_REDACT_PATTERNS)
    text = text.replace("${VECTOR_REDACT_TRANSFORMS}", build_redact_vrl(pats))
    text = text.replace("${VECTOR_REDACT_STRIP_MARKERS}", build_redact_strip_markers(pats))
    text = text.replace("${VECTOR_REDACT_METRICS}", build_redact_metric_entries(pats))
    return text


def _assert_no_lookarounds(pattern: str) -> None:
    """Vector's Rust `regex` crate doesn't support lookarounds. Verify pattern has none."""
    forbidden = ["(?!", "(?=", "(?<!", "(?<="]
    for token in forbidden:
        assert token not in pattern, (
            f"Pattern uses lookaround '{token}' which Vector's regex crate rejects: {pattern!r}"
        )


@pytest.fixture(scope="module")
def parsed_config() -> dict[str, Any]:
    """Render + parse the vector.toml.template once for the module."""
    rendered = _render_template()
    return tomllib.loads(rendered)


def test_template_is_valid_toml(parsed_config: dict[str, Any]) -> None:
    """The rendered template must be valid TOML (tomllib.loads succeeds)."""
    assert isinstance(parsed_config, dict)


def test_hmrun_filter_transform_present(parsed_config: dict[str, Any]) -> None:
    """[transforms.hmrun_filter] table must exist."""
    transforms = parsed_config.get("transforms", {})
    assert "hmrun_filter" in transforms, (
        f"hmrun_filter missing from transforms; keys: {list(transforms.keys())}"
    )


def test_hmrun_shaped_transform_present(parsed_config: dict[str, Any]) -> None:
    """[transforms.hmrun_shaped] table must exist."""
    transforms = parsed_config.get("transforms", {})
    assert "hmrun_shaped" in transforms, (
        f"hmrun_shaped missing from transforms; keys: {list(transforms.keys())}"
    )


def test_sinks_vl_inputs_rewired(parsed_config: dict[str, Any]) -> None:
    """[sinks.vl].inputs must be ['throttle', 'strip_markers_hmrun'] (STAGE-004-006 rewired)."""
    sinks = parsed_config.get("sinks", {})
    vl = sinks.get("vl", {})
    inputs = vl.get("inputs", [])
    assert inputs == ["throttle", "strip_markers_hmrun"], (
        f"sinks.vl.inputs should be ['throttle', 'strip_markers_hmrun'] "
        f"after redaction rewire, got: {inputs}"
    )


def test_drop_noise_excludes_hmrun(parsed_config: dict[str, Any]) -> None:
    """[transforms.drop_noise].condition must exclude hmrun lines."""
    transforms = parsed_config.get("transforms", {})
    drop_noise = transforms.get("drop_noise", {})
    condition = drop_noise.get("condition", "")
    assert 'SYSLOG_IDENTIFIER != "hmrun"' in condition, (
        f"drop_noise condition does not exclude hmrun:\n{condition}"
    )


def test_hmrun_shaped_source_contains_parse_regex(parsed_config: dict[str, Any]) -> None:
    """hmrun_shaped source VRL must contain parse_regex and run_id extraction."""
    transforms = parsed_config.get("transforms", {})
    hmrun_shaped = transforms.get("hmrun_shaped", {})
    source = hmrun_shaped.get("source", "")
    assert "parse_regex" in source, "parse_regex not found in hmrun_shaped source"
    assert "run_id" in source, "run_id not found in hmrun_shaped source"


def test_hmrun_shaped_source_has_uuid_pattern(parsed_config: dict[str, Any]) -> None:
    """hmrun_shaped VRL regex must have a 36-char UUID capture group."""
    transforms = parsed_config.get("transforms", {})
    hmrun_shaped = transforms.get("hmrun_shaped", {})
    source = hmrun_shaped.get("source", "")
    assert "{36}" in source, "36-char uuid pattern not found in hmrun_shaped source"


def test_docker_logs_source_has_no_include_containers(parsed_config: dict[str, Any]) -> None:
    """[sources.docker_logs] must NOT have include_containers (we removed it)."""
    sources = parsed_config.get("sources", {})
    docker_logs = sources.get("docker_logs", {})
    assert "include_containers" not in docker_logs, (
        "include_containers should have been removed from docker_logs source"
    )


def test_docker_logs_source_exclude_containers_is_list(parsed_config: dict[str, Any]) -> None:
    """[sources.docker_logs].exclude_containers must parse as a list."""
    sources = parsed_config.get("sources", {})
    docker_logs = sources.get("docker_logs", {})
    assert "exclude_containers" in docker_logs, "exclude_containers missing from docker_logs"
    assert isinstance(docker_logs["exclude_containers"], list), (
        f"exclude_containers must be a list, got {type(docker_logs['exclude_containers'])}"
    )


def test_docker_logs_source_exclude_containers_with_values() -> None:
    """exclude_containers substitution receives a populated list correctly."""
    rendered = _render_template(docker_exclude='["foo", "bar"]')
    cfg = tomllib.loads(rendered)
    assert cfg["sources"]["docker_logs"]["exclude_containers"] == ["foo", "bar"]


# ---------------------------------------------------------------------------
# STAGE-004-001: multiline codec structural checks
# ---------------------------------------------------------------------------


def test_docker_logs_has_multiline_block(parsed_config: dict[str, Any]) -> None:
    """[sources.docker_logs.multiline] must exist with required keys."""
    ml = parsed_config.get("sources", {}).get("docker_logs", {}).get("multiline", {})
    assert ml, "sources.docker_logs.multiline block missing"
    for key in ("start_pattern", "condition_pattern", "mode", "timeout_ms"):
        assert key in ml, f"multiline key {key!r} missing from docker_logs.multiline"


def test_journald_has_no_multiline_block(parsed_config: dict[str, Any]) -> None:
    """Vector's journald source type does NOT support a multiline sub-table
    (it rejects the entire config). Multiline applies only to docker_logs."""
    journald = parsed_config.get("sources", {}).get("journald", {})
    assert "multiline" not in journald, (
        "journald.multiline must NOT exist — Vector's journald source rejects it"
    )


def test_multiline_mode_is_continue_through(parsed_config: dict[str, Any]) -> None:
    """The docker_logs multiline block must use continue_through mode."""
    ml = parsed_config.get("sources", {}).get("docker_logs", {}).get("multiline", {})
    assert ml.get("mode") == "continue_through", (
        f"docker_logs.multiline.mode must be 'continue_through', got {ml.get('mode')!r}"
    )


def test_multiline_timeout_is_1000ms(parsed_config: dict[str, Any]) -> None:
    """The docker_logs multiline block must have timeout_ms = 1000."""
    ml = parsed_config.get("sources", {}).get("docker_logs", {}).get("multiline", {})
    timeout_val = ml.get("timeout_ms")
    assert timeout_val == _MULTILINE_TIMEOUT_MS, (
        f"docker_logs.multiline.timeout_ms must be {_MULTILINE_TIMEOUT_MS}, got {timeout_val!r}"
    )


def test_multiline_start_pattern_is_valid_regex(parsed_config: dict[str, Any]) -> None:
    """start_pattern must compile with the `regex` package and contain no lookarounds."""
    pattern = (
        parsed_config.get("sources", {})
        .get("docker_logs", {})
        .get("multiline", {})
        .get("start_pattern", "")
    )
    assert pattern, "docker_logs.multiline.start_pattern is empty"
    _assert_no_lookarounds(pattern)
    _regex.compile(pattern)


def test_multiline_condition_pattern_is_valid_regex(parsed_config: dict[str, Any]) -> None:
    """condition_pattern must compile with the `regex` package and contain no lookarounds."""
    pattern = (
        parsed_config.get("sources", {})
        .get("docker_logs", {})
        .get("multiline", {})
        .get("condition_pattern", "")
    )
    assert pattern, "docker_logs.multiline.condition_pattern is empty"
    _assert_no_lookarounds(pattern)
    _regex.compile(pattern)


def test_multiline_condition_pattern_matches_indented_file_line(
    parsed_config: dict[str, Any],
) -> None:
    """condition_pattern must match '  File ...' (Python traceback continuation)."""
    pattern = (
        parsed_config.get("sources", {})
        .get("docker_logs", {})
        .get("multiline", {})
        .get("condition_pattern", "")
    )
    assert _re.match(pattern, '  File "/x.py", line 1') is not None, (
        "condition_pattern should match indented File line"
    )


def test_multiline_does_not_affect_existing_hmrun_block(
    parsed_config: dict[str, Any],
) -> None:
    """Sanity: hmrun_shaped transform must still be present (multiline changes nothing there)."""
    transforms = parsed_config.get("transforms", {})
    assert "hmrun_shaped" in transforms, (
        "hmrun_shaped transform unexpectedly missing after multiline changes"
    )


# ---------------------------------------------------------------------------
# halt_before positive enumeration: start_pattern MATCHES (new group begins)
# ---------------------------------------------------------------------------


def _start_pattern(parsed_config: dict[str, Any]) -> str:
    return (
        parsed_config.get("sources", {})
        .get("docker_logs", {})
        .get("multiline", {})
        .get("start_pattern", "")
    )


def _condition_pattern(parsed_config: dict[str, Any]) -> str:
    return (
        parsed_config.get("sources", {})
        .get("docker_logs", {})
        .get("multiline", {})
        .get("condition_pattern", "")
    )


def test_multiline_start_pattern_matches_traceback(parsed_config: dict[str, Any]) -> None:
    """start_pattern must match 'Traceback (most recent call last):'."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "Traceback (most recent call last):") is not None


def test_multiline_start_pattern_matches_iso_timestamp(parsed_config: dict[str, Any]) -> None:
    """start_pattern must match an ISO-8601 timestamp prefix."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "2026-05-28T19:30:00Z worker started") is not None


def test_multiline_start_pattern_matches_syslog_date(parsed_config: dict[str, Any]) -> None:
    """start_pattern must match a syslog-style date prefix (e.g. 'May 28 ...')."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "May 28 19:30:00 host service: msg") is not None


def test_multiline_start_pattern_matches_pid_bracket(parsed_config: dict[str, Any]) -> None:
    """start_pattern must match 'word[pid]:' prefix."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "sshd[1234]: msg") is not None


def test_multiline_start_pattern_matches_go_panic(parsed_config: dict[str, Any]) -> None:
    """start_pattern must match a Go panic line."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "panic: runtime error: index out of range") is not None


def test_multiline_start_pattern_does_not_match_go_goroutine(
    parsed_config: dict[str, Any],
) -> None:
    """'goroutine N [running]:' is a CONTINUATION of a Go 'panic:' event, not a
    new event. start_pattern must NOT match it, else every Go panic splits."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "goroutine 1 [running]:") is None


def test_multiline_start_pattern_matches_kernel(parsed_config: dict[str, Any]) -> None:
    """start_pattern must match a kernel: prefix."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "kernel: page allocation failure") is not None


# ---------------------------------------------------------------------------
# halt_before: start_pattern must NOT match continuation lines
# ---------------------------------------------------------------------------


def test_multiline_start_pattern_does_not_match_indented_file_line(
    parsed_config: dict[str, Any],
) -> None:
    """'  File ...' is a continuation — start_pattern must NOT match it."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, '  File "/x.py", line 1') is None


def test_multiline_start_pattern_matches_exception_class(
    parsed_config: dict[str, Any],
) -> None:
    """In continue_through, an exception-class line is a valid event start
    (the leading line of a Java/Ruby/Node/.NET stack trace)."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "RuntimeError: bad input") is not None
    assert _re.match(pattern, "java.lang.NullPointerException: x") is not None
    assert _re.match(pattern, "Error: bad") is not None


def test_multiline_start_pattern_does_not_match_indented_java_at(
    parsed_config: dict[str, Any],
) -> None:
    r"""'\tat ...' is a Java stack frame continuation."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "\tat com.example.Foo.bar(Foo.java:42)") is None


def test_multiline_start_pattern_does_not_match_at_node(
    parsed_config: dict[str, Any],
) -> None:
    """'    at Object.run ...' is a Node.js continuation."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "    at Object.run (file.js:1:1)") is None


def test_multiline_start_pattern_does_not_match_at_dotnet(
    parsed_config: dict[str, Any],
) -> None:
    """'   at System.IO...' is a .NET continuation."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "   at System.IO.File.Read (file.cs:1)") is None


def test_multiline_start_pattern_does_not_match_indented_continuation(
    parsed_config: dict[str, Any],
) -> None:
    """Generic indented text is a continuation."""
    pattern = _start_pattern(parsed_config)
    assert _re.match(pattern, "    some random indented text") is None


# ---------------------------------------------------------------------------
# condition_pattern: must match continuation lines
# ---------------------------------------------------------------------------


def test_multiline_condition_pattern_matches_indented(
    parsed_config: dict[str, Any],
) -> None:
    """condition_pattern must match a generic indented continuation line."""
    pattern = _condition_pattern(parsed_config)
    assert _re.match(pattern, "  random indented") is not None


def test_multiline_condition_pattern_matches_java_caused_by(
    parsed_config: dict[str, Any],
) -> None:
    """condition_pattern must match 'Caused by:' (Java continuation)."""
    pattern = _condition_pattern(parsed_config)
    assert _re.match(pattern, "Caused by: java.lang.RuntimeException: bad") is not None


def test_multiline_condition_pattern_matches_python_exception_terminator(
    parsed_config: dict[str, Any],
) -> None:
    """condition_pattern MUST match a trailing exception terminator like 'ValueError:'
    so a Python traceback's closing line merges into the event."""
    pattern = _condition_pattern(parsed_config)
    assert _re.match(pattern, "ValueError: bad") is not None


def test_multiline_condition_pattern_matches_indented_continuation(
    parsed_config: dict[str, Any],
) -> None:
    r"""condition_pattern must match an indented continuation line (^\s)."""
    pattern = _condition_pattern(parsed_config)
    assert _re.match(pattern, "    at Object.run (file.js:1:1)") is not None


def test_multiline_condition_differs_from_start_pattern(parsed_config: dict[str, Any]) -> None:
    """In continue_through mode, start_pattern (event starts) and condition_pattern
    (continuation lines) are intentionally different."""
    start = _start_pattern(parsed_config)
    cond = _condition_pattern(parsed_config)
    assert start != cond, "start_pattern and condition_pattern must differ in continue_through mode"


def test_test_fixture_multiline_matches_template(parsed_config: dict[str, Any]) -> None:
    """The integration-rig fixture config (deploy/compose/test-fixtures/vector.toml)
    must carry a docker_logs.multiline block byte-identical to the production
    template's, or the integration tests silently stop exercising stitching.
    """
    repo_root = Path(__file__).resolve().parents[3]
    fixture_path = repo_root / "deploy" / "compose" / "test-fixtures" / "vector.toml"
    fixture = tomllib.loads(fixture_path.read_text(encoding="utf-8"))
    fixture_ml = fixture["sources"]["docker_logs"]["multiline"]
    template_ml = parsed_config["sources"]["docker_logs"]["multiline"]
    assert fixture_ml["start_pattern"] == template_ml["start_pattern"], (
        "fixture start_pattern drifted from production template"
    )
    assert fixture_ml["condition_pattern"] == template_ml["condition_pattern"], (
        "fixture condition_pattern drifted from production template"
    )
    assert fixture_ml["mode"] == template_ml["mode"]
    assert fixture_ml["timeout_ms"] == template_ml["timeout_ms"]


# ---------------------------------------------------------------------------
# STAGE-004-004: docker_enrich container label enrichment
# ---------------------------------------------------------------------------


def test_docker_enrich_transform_present(parsed_config: dict[str, Any]) -> None:
    """[transforms.docker_enrich] table must exist."""
    transforms = parsed_config.get("transforms", {})
    assert "docker_enrich" in transforms, (
        f"docker_enrich missing from transforms; keys: {list(transforms.keys())}"
    )


def test_docker_enrich_inputs_is_parse_json(parsed_config: dict[str, Any]) -> None:
    """docker_enrich must read from parse_json (inserted after parse_json)."""
    transforms = parsed_config.get("transforms", {})
    docker_enrich = transforms.get("docker_enrich", {})
    inputs = docker_enrich.get("inputs", [])
    assert inputs == ["parse_json"], f"docker_enrich.inputs must be ['parse_json'], got {inputs}"


def test_drop_noise_inputs_is_docker_severity_extract(parsed_config: dict[str, Any]) -> None:
    """drop_noise must now read from docker_severity_extract (rewired from docker_enrich)."""
    transforms = parsed_config.get("transforms", {})
    drop_noise = transforms.get("drop_noise", {})
    inputs = drop_noise.get("inputs", [])
    assert inputs == ["docker_severity_extract"], (
        f"drop_noise.inputs must be ['docker_severity_extract'] after rewire, got {inputs}"
    )


def test_docker_enrich_source_contains_compose_fields(parsed_config: dict[str, Any]) -> None:
    """docker_enrich source VRL must contain compose_project and compose_service assignments."""
    transforms = parsed_config.get("transforms", {})
    source = transforms.get("docker_enrich", {}).get("source", "")
    assert ".compose_project" in source, ".compose_project not found in docker_enrich source"
    assert ".compose_service" in source, ".compose_service not found in docker_enrich source"


def test_docker_enrich_source_contains_image_fields(parsed_config: dict[str, Any]) -> None:
    """docker_enrich source VRL must contain image_name, image_tag, image_digest, image_revision."""
    transforms = parsed_config.get("transforms", {})
    source = transforms.get("docker_enrich", {}).get("source", "")
    assert ".image_name" in source, ".image_name not found in docker_enrich source"
    assert ".image_tag" in source, ".image_tag not found in docker_enrich source"
    assert ".image_digest" in source, ".image_digest not found in docker_enrich source"
    assert ".image_revision" in source, ".image_revision not found in docker_enrich source"


def test_docker_enrich_source_assignment_targets_are_snake_case(
    parsed_config: dict[str, Any],
) -> None:
    """Every assignment target in docker_enrich source (LHS of `.foo =`) must be snake_case."""
    transforms = parsed_config.get("transforms", {})
    source = transforms.get("docker_enrich", {}).get("source", "")
    # Match patterns like `.compose_project =` or `.image_tag =`
    targets = _re.findall(r"(\.[a-zA-Z][a-zA-Z0-9_]*)\s*=(?!=)", source)
    bad = [t for t in targets if not _re.match(r"^\.[a-z][a-z0-9_]*$", t)]
    assert not bad, f"docker_enrich source has non-snake_case assignment targets: {bad}"


def test_docker_enrich_source_has_journald_guards(parsed_config: dict[str, Any]) -> None:
    """docker_enrich must gate label extraction on exists(.label) and exists(.image)
    so journald entries never carry null compose_*/image_* fields."""
    transforms = parsed_config.get("transforms", {})
    source = transforms.get("docker_enrich", {}).get("source", "")
    assert "exists(.label)" in source, (
        "exists(.label) journald guard missing from docker_enrich source"
    )
    assert "exists(.image)" in source, (
        "exists(.image) journald guard missing from docker_enrich source"
    )


def test_docker_enrich_source_does_not_delete_label_bag(parsed_config: dict[str, Any]) -> None:
    """docker_enrich must NOT contain del(.label) — raw label bag is kept (D-KEEP-RAW-LABEL-BAG)."""
    transforms = parsed_config.get("transforms", {})
    source = transforms.get("docker_enrich", {}).get("source", "")
    assert "del(.label)" not in source, (
        "docker_enrich source contains del(.label) but "
        "D-KEEP-RAW-LABEL-BAG requires raw bag to be kept"
    )


def test_docker_enrich_source_has_no_lookarounds(parsed_config: dict[str, Any]) -> None:
    """docker_enrich VRL source must not use regex lookarounds (Vector uses Rust regex crate)."""
    transforms = parsed_config.get("transforms", {})
    source = transforms.get("docker_enrich", {}).get("source", "")
    _assert_no_lookarounds(source)


# ---------------------------------------------------------------------------
# STAGE-004-004A: docker_severity_extract structural checks
# ---------------------------------------------------------------------------


def test_docker_severity_extract_transform_present(parsed_config: dict[str, Any]) -> None:
    """[transforms.docker_severity_extract] table must exist."""
    transforms = parsed_config.get("transforms", {})
    assert "docker_severity_extract" in transforms, (
        f"docker_severity_extract missing from transforms; keys: {list(transforms.keys())}"
    )


def test_docker_severity_extract_inputs_is_docker_enrich(parsed_config: dict[str, Any]) -> None:
    """docker_severity_extract must read from docker_enrich."""
    transforms = parsed_config.get("transforms", {})
    inputs = transforms.get("docker_severity_extract", {}).get("inputs", [])
    assert inputs == ["docker_enrich"], (
        f"docker_severity_extract.inputs must be ['docker_enrich'], got {inputs}"
    )


def test_throttle_inputs_rewired(parsed_config: dict[str, Any]) -> None:
    """throttle.inputs must be ['strip_markers_main'] (STAGE-004-006 rewired)."""
    transforms = parsed_config.get("transforms", {})
    inputs = transforms.get("throttle", {}).get("inputs", [])
    assert inputs == ["strip_markers_main"], (
        f"throttle.inputs should be ['strip_markers_main'] after redaction rewire, got {inputs}"
    )


def test_docker_severity_extract_source_has_guard(parsed_config: dict[str, Any]) -> None:
    """docker_severity_extract source must have the docker+info guard."""
    source = (
        parsed_config.get("transforms", {}).get("docker_severity_extract", {}).get("source", "")
    )
    assert '.severity == "info"' in source, (
        'guard .severity == "info" missing from docker_severity_extract source'
    )
    assert "is_null(.severity)" in source, (
        "guard is_null(.severity) missing from docker_severity_extract source"
    )
    assert "exists(.label)" in source, (
        "docker discriminator exists(.label) missing from docker_severity_extract source"
    )


def test_docker_severity_extract_source_assigns_canonical_severities(
    parsed_config: dict[str, Any],
) -> None:
    """Source must assign all 7 non-info canonical severities (emergency through debug).

    Checks LHS assignments only — source tokens like FATAL/PANIC/EMERG/CRIT/WARNING/ERR
    are inputs that map to canonical values; they must NOT appear as assigned values.
    """
    source = (
        parsed_config.get("transforms", {}).get("docker_severity_extract", {}).get("source", "")
    )
    for level in ("emergency", "alert", "critical", "error", "warn", "notice", "debug"):
        assert f'.severity = "{level}"' in source, (
            f'.severity = "{level}" assignment missing from docker_severity_extract source'
        )


def test_docker_severity_extract_source_has_no_lookarounds(
    parsed_config: dict[str, Any],
) -> None:
    """docker_severity_extract VRL source must not use regex lookarounds."""
    source = (
        parsed_config.get("transforms", {}).get("docker_severity_extract", {}).get("source", "")
    )
    _assert_no_lookarounds(source)


def test_docker_severity_extract_patterns_compile(parsed_config: dict[str, Any]) -> None:
    """Every r'...' pattern literal in docker_severity_extract source must compile
    with Python re (smoke-check; authoritative check is the vector validate test).
    """
    source = (
        parsed_config.get("transforms", {}).get("docker_severity_extract", {}).get("source", "")
    )
    # Extract VRL raw-string literals r'...' (single-quote delimited).
    # The pattern captures content between r' and ' allowing escaped chars.
    raw_patterns = _re.findall(r"r'((?:\\.|[^'\\])*)'", source)
    assert raw_patterns, "No r'...' patterns found in docker_severity_extract source"
    for pat in raw_patterns:
        try:
            _re.compile(pat)
        except _re.error as exc:
            pytest.fail(
                f"Pattern failed to compile with Python re (VRL syntax smoke-check):\n"
                f"  pattern: {pat!r}\n"
                f"  error:   {exc}"
            )


def test_docker_severity_extract_patterns_match_canonical_forms(
    parsed_config: dict[str, Any],
) -> None:
    """ERROR pattern (index 3, emergency=0,alert=1,critical=2,error=3) must match
    the canonical forms: bare, bracketed, HA-timestamp, ANSI-HA, logfmt.
    Must NOT match mid-sentence prose or INFO lines.
    """
    source = (
        parsed_config.get("transforms", {}).get("docker_severity_extract", {}).get("source", "")
    )
    raw_patterns = _re.findall(r"r'((?:\\.|[^'\\])*)'", source)
    # Patterns are in order: emergency(0), alert(1), critical(2), error(3),
    # warn(4), notice(5), debug(6). Index 3 is the ERROR pattern.
    assert len(raw_patterns) == _EXPECTED_SEVERITY_PATTERNS, (
        f"Expected exactly {_EXPECTED_SEVERITY_PATTERNS} r'...' patterns, found {len(raw_patterns)}"
    )
    error_pat = _re.compile(raw_patterns[3], _re.IGNORECASE)

    should_match = [
        "ERROR foo",
        "[ERROR] foo",
        "2026-05-29 08:39:23.890 ERROR (MainThread) bar",
        "\x1b[31m2026-05-29T08:39:23Z ERROR baz",
        'time="2026-05-29T08:39:23Z" level=error msg="test"',
    ]
    for msg in should_match:
        assert error_pat.search(msg) is not None, f"ERROR pattern should match {msg!r} but did not"

    should_not_match = [
        "INFO doing thing",
        "This operation encountered an error but recovered.",
    ]
    for msg in should_not_match:
        assert error_pat.search(msg) is None, f"ERROR pattern should NOT match {msg!r} but did"

    # Test CRITICAL pattern (index 2)
    critical_pat = _re.compile(raw_patterns[2], _re.IGNORECASE)

    should_match_critical = [
        "CRITICAL: out of memory",
        "CRITICAL (Main) message",
        "2026-05-29 10:00:00.123 CRITICAL (Main) message",
        "CRIT failure",
        "[CRITICAL] system down",
    ]
    for msg in should_match_critical:
        assert critical_pat.search(msg) is not None, (
            f"CRITICAL pattern should match {msg!r} but did not"
        )

    should_not_match_critical = [
        "This is critical to fix",
        "CRITICAL_VALUE constant",
    ]
    for msg in should_not_match_critical:
        assert critical_pat.search(msg) is None, (
            f"CRITICAL pattern should NOT match {msg!r} but did"
        )

    # Test WARN pattern (index 4)
    warn_pat = _re.compile(raw_patterns[4], _re.IGNORECASE)

    should_match_warn = [
        "WARN something",
        "WARNING deprecation",
        "[WARN] foo",
        "2026-05-29 10:00:00.123 WARNING (X) msg",
        'time="2026-05-29T08:39:23Z" level=warn msg="test"',
    ]
    for msg in should_match_warn:
        assert warn_pat.search(msg) is not None, f"WARN pattern should match {msg!r} but did not"

    should_not_match_warn = [
        "The system will warn you later",
        "WARNING_LEVEL constant",
    ]
    for msg in should_not_match_warn:
        assert warn_pat.search(msg) is None, f"WARN pattern should NOT match {msg!r} but did"


def test_docker_severity_extract_emitted_values_are_canonical(
    parsed_config: dict[str, Any],
) -> None:
    """Every .severity = "..." assignment in docker_severity_extract source must
    be a member of _CANONICAL_SEVERITIES (catches typo regressions).
    """
    source = (
        parsed_config.get("transforms", {}).get("docker_severity_extract", {}).get("source", "")
    )
    assigned = _re.findall(r'\.severity\s*=\s*"([^"]+)"', source)
    assert assigned, 'No .severity = "..." assignments found in docker_severity_extract source'
    bad = [v for v in assigned if v not in _CANONICAL_SEVERITIES]
    assert not bad, (
        f"docker_severity_extract assigns non-canonical severity values: {bad}; "
        f"canonical set: {sorted(_CANONICAL_SEVERITIES)}"
    )


def test_docker_severity_extract_fatal_maps_to_critical(
    parsed_config: dict[str, Any],
) -> None:
    """FATAL must map to critical (not error). Design decision D-FATAL-MAPS-TO-CRITICAL.

    Patterns are in order: emergency(0), alert(1), critical(2), error(3).
    Index 2 is the CRITICAL/CRIT/FATAL pattern; index 3 is ERROR/ERR.
    """
    source = (
        parsed_config.get("transforms", {}).get("docker_severity_extract", {}).get("source", "")
    )
    raw_patterns = _re.findall(r"r'((?:\\.|[^'\\])*)'", source)
    assert len(raw_patterns) == _EXPECTED_SEVERITY_PATTERNS, (
        f"Expected exactly {_EXPECTED_SEVERITY_PATTERNS} r'...' patterns, found {len(raw_patterns)}"
    )
    critical_pat = _re.compile(raw_patterns[2], _re.IGNORECASE)
    error_pat = _re.compile(raw_patterns[3], _re.IGNORECASE)

    assert critical_pat.search("FATAL: out of memory") is not None, (
        "FATAL: out of memory should match CRITICAL pattern (D-FATAL-MAPS-TO-CRITICAL)"
    )
    assert error_pat.search("FATAL: out of memory") is None, (
        "FATAL: out of memory must NOT match ERROR pattern (belongs in critical)"
    )


# ---------------------------------------------------------------------------
# STAGE-004-005: hmrun_shaped cron_fingerprint structured-field enrichment
# ---------------------------------------------------------------------------


def _hmrun_shaped_source(parsed_config: dict[str, Any]) -> str:
    return parsed_config.get("transforms", {}).get("hmrun_shaped", {}).get("source", "")


def test_hmrun_shaped_sets_cron_fingerprint_from_hm_fp(parsed_config: dict[str, Any]) -> None:
    """hmrun_shaped must assign .cron_fingerprint from the structured .HM_FP field."""
    source = _hmrun_shaped_source(parsed_config)
    assert ".cron_fingerprint = .HM_FP" in source, (
        "hmrun_shaped must set .cron_fingerprint = .HM_FP"
    )


def test_hmrun_shaped_guards_hm_fp_with_exists(parsed_config: dict[str, Any]) -> None:
    """HM_FP extraction must be guarded by exists(.HM_FP) (absent-field safe)."""
    source = _hmrun_shaped_source(parsed_config)
    assert "exists(.HM_FP)" in source, "hmrun_shaped must guard HM_FP with exists(.HM_FP)"


def test_hmrun_shaped_sets_run_id_from_hm_run(parsed_config: dict[str, Any]) -> None:
    """hmrun_shaped must assign .run_id from the structured .HM_RUN field
    (D-ENRICHMENT-IS-ADDITIVE: run_id independent of fingerprint)."""
    source = _hmrun_shaped_source(parsed_config)
    assert ".run_id = .HM_RUN" in source, "hmrun_shaped must set .run_id = .HM_RUN"
    assert "exists(.HM_RUN)" in source, "hmrun_shaped must guard HM_RUN with exists(.HM_RUN)"


def test_hmrun_shaped_retains_legacy_regex_fallback(parsed_config: dict[str, Any]) -> None:
    """The transitional fallback regex branch (HM_RUN=<uuid> prefix) must be retained
    so legacy/in-flight lines still parse run_id during rollout."""
    source = _hmrun_shaped_source(parsed_config)
    assert "parse_regex" in source, "legacy fallback parse_regex missing from hmrun_shaped"
    assert "{36}" in source, "legacy 36-char uuid fallback pattern missing from hmrun_shaped"


def test_hmrun_shaped_deletes_raw_journald_fields(parsed_config: dict[str, Any]) -> None:
    """Raw HM_RUN / HM_FP journald keys must be deleted after extraction (no double-ship)."""
    source = _hmrun_shaped_source(parsed_config)
    assert "del(.HM_RUN)" in source, "hmrun_shaped must del(.HM_RUN) after extraction"
    assert "del(.HM_FP)" in source, "hmrun_shaped must del(.HM_FP) after extraction"


def test_hmrun_shaped_source_has_no_lookarounds(parsed_config: dict[str, Any]) -> None:
    """hmrun_shaped VRL source must not use regex lookarounds (Rust regex crate)."""
    _assert_no_lookarounds(_hmrun_shaped_source(parsed_config))


def test_hmrun_filter_inputs_unchanged(parsed_config: dict[str, Any]) -> None:
    """Regression guard: hmrun_filter still reads from journald and matches hmrun."""
    transforms = parsed_config.get("transforms", {})
    hmrun_filter = transforms.get("hmrun_filter", {})
    assert hmrun_filter.get("inputs", []) == ["journald"], (
        "hmrun_filter.inputs must be ['journald']"
    )
    assert 'SYSLOG_IDENTIFIER == "hmrun"' in hmrun_filter.get("condition", ""), (
        "hmrun_filter must still filter SYSLOG_IDENTIFIER == 'hmrun'"
    )


# ---------------------------------------------------------------------------
# STAGE-004-012A: source_type_classify transform and identity-qualified stream picker
# ---------------------------------------------------------------------------


def _source_type_classify_source(parsed_config: dict[str, Any]) -> str:
    return parsed_config.get("transforms", {}).get("source_type_classify", {}).get("source", "")


def test_source_type_classify_transform_present(parsed_config: dict[str, Any]) -> None:
    """[transforms.source_type_classify] table must exist."""
    transforms = parsed_config.get("transforms", {})
    assert "source_type_classify" in transforms, (
        f"source_type_classify missing from transforms; keys: {list(transforms.keys())}"
    )


def test_source_type_classify_inputs_is_drop_noise(parsed_config: dict[str, Any]) -> None:
    """[transforms.source_type_classify].inputs must be ['drop_noise']."""
    transforms = parsed_config.get("transforms", {})
    source_type_classify = transforms.get("source_type_classify", {})
    assert source_type_classify.get("inputs") == ["drop_noise"], (
        f"source_type_classify.inputs should be ['drop_noise'], got: "
        f"{source_type_classify.get('inputs')}"
    )


def test_redact_main_inputs_is_source_type_classify(parsed_config: dict[str, Any]) -> None:
    """[transforms.redact_main].inputs must be ['source_type_classify'] (chain rewired)."""
    transforms = parsed_config.get("transforms", {})
    redact_main = transforms.get("redact_main", {})
    assert redact_main.get("inputs") == ["source_type_classify"], (
        f"redact_main.inputs should be ['source_type_classify'], got: {redact_main.get('inputs')}"
    )


def test_source_type_classify_assigns_all_four_values(parsed_config: dict[str, Any]) -> None:
    """source_type_classify source must assign all four source_type values."""
    source = _source_type_classify_source(parsed_config)
    assert '.source_type = "docker"' in source, "docker assignment missing"
    assert '.source_type = "cron"' in source, "cron assignment missing"
    assert '.source_type = "systemd"' in source, "systemd assignment missing"
    assert '.source_type = "unknown"' in source, "unknown assignment missing"


def test_source_type_classify_cron_before_systemd(parsed_config: dict[str, Any]) -> None:
    """cron branch must come BEFORE systemd branch (precedence check)."""
    source = _source_type_classify_source(parsed_config)
    cron_idx = source.index('.source_type = "cron"')
    systemd_idx = source.index('.source_type = "systemd"')
    assert cron_idx < systemd_idx, (
        f"cron branch must come before systemd (cron at {cron_idx}, systemd at {systemd_idx})"
    )
    # Also verify cron branch checks the right identifiers
    assert '.SYSLOG_IDENTIFIER == "CRON"' in source, "CRON identifier check missing"
    assert '.SYSLOG_IDENTIFIER == "crond"' in source, "crond identifier check missing"
    assert 'includes(["cron.service", "crond.service"], ._SYSTEMD_UNIT)' in source, (
        "cron.service/crond.service includes() check missing"
    )
    # Verify systemd branch is a bare exists check
    assert "exists(._SYSTEMD_UNIT)" in source, "bare exists(._SYSTEMD_UNIT) check missing"


def test_source_type_classify_docker_discriminator(parsed_config: dict[str, Any]) -> None:
    """docker branch must use exists(.label) and exists(.image) discriminators."""
    source = _source_type_classify_source(parsed_config)
    assert "exists(.label)" in source, "exists(.label) check missing"
    assert "exists(.image)" in source, "exists(.image) check missing"


def test_source_type_classify_has_no_lookarounds(parsed_config: dict[str, Any]) -> None:
    """source_type_classify VRL source must not use regex lookarounds."""
    _assert_no_lookarounds(_source_type_classify_source(parsed_config))


def test_hmrun_shaped_sets_source_type_cron(parsed_config: dict[str, Any]) -> None:
    """hmrun_shaped must set .source_type = "cron"."""
    source = _hmrun_shaped_source(parsed_config)
    assert '.source_type = "cron"' in source, 'hmrun_shaped must set .source_type = "cron"'


@pytest.mark.slow
def test_rendered_template_passes_vector_validate(tmp_path: Path) -> None:
    """Authoritative VRL compile check via `vector validate`.

    Closes the STAGE-004-004 false-green gap: template-parse tests + `vector
    validate --no-environment` both passed on a config that crash-looped Vector
    with a VRL syntax error (segs[length-1]). Plain `vector validate` compiles
    every remap transform's VRL and catches such errors authoritatively.

    Skips when the vector binary is not on PATH so developer machines without
    Vector installed don't fail this test. CI installs Vector in the integration
    job; the test runs there.
    """
    if shutil.which("vector") is None:
        pytest.skip("vector binary not on PATH")
    # Render the template with placeholder substitutions sufficient for validate.
    rendered = _render_template()
    out = tmp_path / "vector.toml"
    out.write_text(rendered, encoding="utf-8")
    result = subprocess.run(
        ["vector", "validate", str(out)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(
            "vector validate failed:\nstdout:\n" + result.stdout + "\nstderr:\n" + result.stderr
        )
