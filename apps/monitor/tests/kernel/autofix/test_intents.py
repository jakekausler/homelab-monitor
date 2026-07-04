"""Unit tests for docker intent schema, parser, validator (STAGE-009-014)."""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_monitor.kernel.autofix.intents import (
    DockerIntent,
    IntentAccept,
    IntentDeny,
    MalformedIntentFileError,
    parse_intents,
    validate_intent,
)
from homelab_monitor.kernel.autofix.types import ResolvedGrants

EXPECTED_INTENT_COUNT = 3


def _grants(
    *,
    container: str | None = "pihole-unbound",
    actions: tuple[str, ...] = ("restart",),
) -> ResolvedGrants:
    return ResolvedGrants(
        docker_container=container,
        docker_allowed_actions=actions,
        ssh_target_id=None,
        egress=(),
    )


def test_parse_intents_missing_file_returns_empty_list(tmp_path: Path) -> None:
    """parse_intents(missing file) returns []."""
    result = parse_intents(tmp_path / "missing.json")
    assert result == []


def test_parse_intents_valid_single_returns_list(tmp_path: Path) -> None:
    """parse_intents with single valid entry returns list with one element."""
    intent_file = tmp_path / "intents.json"
    intent_file.write_text('[{"container":"foo","action":"restart"}]')
    result = parse_intents(intent_file)
    assert len(result) == 1
    assert result[0].container == "foo"
    assert result[0].action == "restart"


def test_parse_intents_valid_multiple_returns_list(tmp_path: Path) -> None:
    """parse_intents with multiple entries returns all in order."""
    intent_file = tmp_path / "intents.json"
    intent_file.write_text(
        '[{"container":"foo","action":"restart"},'
        '{"container":"bar","action":"restart"},'
        '{"container":"baz","action":"restart"}]'
    )
    result = parse_intents(intent_file)
    assert len(result) == EXPECTED_INTENT_COUNT
    assert result[0].container == "foo"
    assert result[1].container == "bar"
    assert result[2].container == "baz"


def test_parse_intents_invalid_json_raises_not_json(tmp_path: Path) -> None:
    """parse_intents with invalid JSON raises MalformedIntentFileError."""
    intent_file = tmp_path / "intents.json"
    intent_file.write_text("not json{")
    with pytest.raises(MalformedIntentFileError) as excinfo:
        parse_intents(intent_file)
    assert excinfo.value.reason == "not_json"


def test_parse_intents_not_a_list_raises_not_a_list(tmp_path: Path) -> None:
    """parse_intents with object (not list) raises not_a_list."""
    intent_file = tmp_path / "intents.json"
    intent_file.write_text('{"container":"foo","action":"restart"}')
    with pytest.raises(MalformedIntentFileError) as excinfo:
        parse_intents(intent_file)
    assert excinfo.value.reason == "not_a_list"


def test_parse_intents_invalid_entry_raises_invalid_entry(tmp_path: Path) -> None:
    """parse_intents with min_length=1 violation raises invalid_entry."""
    intent_file = tmp_path / "intents.json"
    intent_file.write_text('[{"container":"","action":"restart"}]')
    with pytest.raises(MalformedIntentFileError) as excinfo:
        parse_intents(intent_file)
    assert excinfo.value.reason == "invalid_entry"
    assert "entry 0" in excinfo.value.detail


def test_parse_intents_action_not_restart_raises_invalid_entry(
    tmp_path: Path,
) -> None:
    """parse_intents with non-restart action raises invalid_entry."""
    intent_file = tmp_path / "intents.json"
    intent_file.write_text('[{"container":"foo","action":"stop"}]')
    with pytest.raises(MalformedIntentFileError) as excinfo:
        parse_intents(intent_file)
    assert excinfo.value.reason == "invalid_entry"


def test_parse_intents_extra_field_raises_invalid_entry(tmp_path: Path) -> None:
    """parse_intents with extra field (extra=forbid) raises invalid_entry."""
    intent_file = tmp_path / "intents.json"
    intent_file.write_text('[{"container":"foo","action":"restart","extra":"x"}]')
    with pytest.raises(MalformedIntentFileError) as excinfo:
        parse_intents(intent_file)
    assert excinfo.value.reason == "invalid_entry"


def test_parse_intents_read_failure_raises_not_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """parse_intents raises 'not_json' if the file exists but read_text raises OSError."""
    intent_file = tmp_path / "intents.json"
    intent_file.write_text("[]")  # File must exist so intent_path.exists() returns True

    def raise_oserror(self: Path, encoding: str = "utf-8") -> str:
        raise PermissionError("simulated read failure")

    monkeypatch.setattr(Path, "read_text", raise_oserror)

    with pytest.raises(MalformedIntentFileError) as excinfo:
        parse_intents(intent_file)
    assert excinfo.value.reason == "not_json"
    assert "read failed" in excinfo.value.detail


def test_parse_intents_read_failure_truncates_long_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long OSError message on read failure is truncated to <=1024+3 chars."""
    intent_file = tmp_path / "intents.json"
    intent_file.write_text("[]")  # File must exist so intent_path.exists() returns True

    long_message = "x" * 2000

    def raise_permission_error(self: Path, encoding: str = "utf-8") -> str:
        raise PermissionError(long_message)

    monkeypatch.setattr(Path, "read_text", raise_permission_error)

    with pytest.raises(MalformedIntentFileError) as excinfo:
        parse_intents(intent_file)
    assert excinfo.value.reason == "not_json"
    assert len(excinfo.value.detail) <= 1024 + 3
    assert excinfo.value.detail.endswith("...")


def test_parse_intents_invalid_entry_truncates_long_detail(tmp_path: Path) -> None:
    """A long pydantic validation error message is truncated to <=1024+3 chars.

    A single oversized field value does not reliably produce a long pydantic
    error message: pydantic truncates the ``input_value`` repr in messages
    like ``string_too_long`` regardless of the actual input length. Instead,
    this uses many disallowed extra fields (``extra="forbid"``), each of
    which contributes its own "Extra inputs are not permitted" error line,
    reliably producing a message well over 1024 chars.
    """
    intent_file = tmp_path / "intents.json"
    extra_fields = ",".join(f'"extra{i}":"x"' for i in range(50))
    intent_file.write_text(f'[{{"container":"foo","action":"restart",{extra_fields}}}]')

    with pytest.raises(MalformedIntentFileError) as excinfo:
        parse_intents(intent_file)
    assert excinfo.value.reason == "invalid_entry"
    assert len(excinfo.value.detail) <= 1024 + 3
    assert excinfo.value.detail.endswith("...")


def test_validate_intent_accept_when_container_and_action_match() -> None:
    """validate_intent with matching container/action returns IntentAccept."""
    intent = DockerIntent(container="pihole-unbound", action="restart")
    result = validate_intent(intent, _grants())
    assert isinstance(result, IntentAccept)


def test_validate_intent_denies_when_docker_capability_absent() -> None:
    """validate_intent denies when docker_container is None."""
    intent = DockerIntent(container="pihole-unbound", action="restart")
    result = validate_intent(intent, _grants(container=None))
    assert isinstance(result, IntentDeny)
    assert "docker not in scoped_capabilities" in result.reason


def test_validate_intent_denies_on_container_mismatch() -> None:
    """validate_intent denies when container doesn't match."""
    intent = DockerIntent(container="wrong", action="restart")
    result = validate_intent(intent, _grants())
    assert isinstance(result, IntentDeny)
    assert "container 'wrong' not in envelope" in result.reason


def test_validate_intent_denies_on_action_not_allowed() -> None:
    """validate_intent denies when action not in allowed_actions."""
    intent = DockerIntent(container="pihole-unbound", action="restart")
    result = validate_intent(intent, _grants(actions=()))
    assert isinstance(result, IntentDeny)
    assert "action 'restart' not in allowed_actions" in result.reason


def test_parse_intents_container_too_long_raises_invalid_entry(tmp_path: Path) -> None:
    """parse_intents rejects a container name over 256 chars."""
    intent_file = tmp_path / "intents.json"
    long_name = "a" * 257
    intent_file.write_text(f'[{{"container":"{long_name}","action":"restart"}}]')
    with pytest.raises(MalformedIntentFileError) as excinfo:
        parse_intents(intent_file)
    assert excinfo.value.reason == "invalid_entry"


def test_parse_intents_container_invalid_chars_raises_invalid_entry(tmp_path: Path) -> None:
    """parse_intents rejects a container name with disallowed characters.

    E.g. slash for path traversal prevention.
    """
    intent_file = tmp_path / "intents.json"
    intent_file.write_text('[{"container":"foo/bar","action":"restart"}]')
    with pytest.raises(MalformedIntentFileError) as excinfo:
        parse_intents(intent_file)
    assert excinfo.value.reason == "invalid_entry"
