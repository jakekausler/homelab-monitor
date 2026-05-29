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
import tomllib
from pathlib import Path
from typing import Any

import pytest
import regex as _regex

_TEMPLATE_PATH = Path(__file__).parents[3] / "deploy" / "vector" / "vector.toml.template"
_MULTILINE_TIMEOUT_MS = 1000


def _render_template(docker_exclude: str = "[]") -> str:
    """Substitute template placeholders with dummy values."""
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    text = text.replace("${CRON_EVENTS_INGEST_TOKEN}", "dummy-token-for-test")
    text = text.replace("${VECTOR_DOCKER_EXCLUDE}", docker_exclude)
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


def test_sinks_vl_inputs_includes_hmrun_shaped(parsed_config: dict[str, Any]) -> None:
    """[sinks.vl].inputs must include 'hmrun_shaped'."""
    sinks = parsed_config.get("sinks", {})
    vl = sinks.get("vl", {})
    inputs = vl.get("inputs", [])
    assert "hmrun_shaped" in inputs, f"hmrun_shaped not in sinks.vl.inputs: {inputs}"


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


def test_drop_noise_inputs_is_docker_enrich(parsed_config: dict[str, Any]) -> None:
    """drop_noise must now read from docker_enrich (rewired from parse_json)."""
    transforms = parsed_config.get("transforms", {})
    drop_noise = transforms.get("drop_noise", {})
    inputs = drop_noise.get("inputs", [])
    assert inputs == ["docker_enrich"], (
        f"drop_noise.inputs must be ['docker_enrich'] after rewire, got {inputs}"
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


def test_sinks_vl_inputs_unchanged(parsed_config: dict[str, Any]) -> None:
    """[sinks.vl].inputs must remain ['throttle', 'hmrun_shaped'].

    docker_enrich does not touch the sink wiring.
    """
    sinks = parsed_config.get("sinks", {})
    vl = sinks.get("vl", {})
    inputs = vl.get("inputs", [])
    assert inputs == ["throttle", "hmrun_shaped"], (
        f"sinks.vl.inputs must be ['throttle', 'hmrun_shaped'], got {inputs}"
    )
