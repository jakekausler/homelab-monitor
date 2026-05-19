"""Tests for kernel/cron/install.py (STAGE-002-009).

Covers:
- _resolve_container_path for all source_path types + unrecognized
- build_install_kit: produces correct wrapper + crontab diff (dry-run, no writes)
- AlreadyWrappedError, CronLineNotFoundError when appropriate
- _rewrite_line correctness
- _find_matching_line logic
- Discoverer round-trip: fingerprint of wrapped line == fingerprint of unwrapped
- PublicUrlNotConfiguredError (via get_public_url returning None)
- RemoteHostError for non-local cron
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_monitor.kernel.cron.discovery_types import CronSourceKind
from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint
from homelab_monitor.kernel.cron.install import (
    AlreadyWrappedError,
    CronLineNotFoundError,
    WrapperInstallError,
    _find_matching_line,  # pyright: ignore[reportPrivateUsage]
    _resolve_container_path,  # pyright: ignore[reportPrivateUsage]
    _rewrite_line,  # pyright: ignore[reportPrivateUsage]
    build_install_kit,
)
from homelab_monitor.kernel.cron.repository import CronRecord
from homelab_monitor.kernel.cron.wrapper_constants import (
    TOKEN_FILE_PATH,
    WRAPPER_PATH,
    WRAPPER_SEPARATOR,
    build_invocation_prefix,
    is_legacy_wrapped,
    is_wrapped,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOST = "monitor-host"
_SOURCE_PATH = "/etc/crontab"
_SCHEDULE = "* * * * *"
_COMMAND = "/usr/bin/backup.sh --full"
_FINGERPRINT = compute_fingerprint(
    host=_HOST, source_path=_SOURCE_PATH, schedule=_SCHEDULE, command=_COMMAND
)


def _make_cron_record(  # noqa: PLR0913 -- test factory mirrors every CronRecord field
    *,
    fingerprint: str = _FINGERPRINT,
    name: str = "backup",
    host: str = _HOST,
    command: str = _COMMAND,
    schedule: str = _SCHEDULE,
    schedule_canonical: str | None = _SCHEDULE,
    cadence_seconds: int = 60,
    expected_grace_seconds: int = 300,
    enabled: bool = True,
    last_seen_state: str = "unknown",
    created_at: str = "2024-01-01T00:00:00Z",
    updated_at: str = "2024-01-01T00:00:00Z",
    hidden_at: str | None = None,
    source_path: str | None = _SOURCE_PATH,
    wrapper_last_seen_at: str | None = None,
    last_discovered_at: str | None = None,
    soft_deleted_at: str | None = None,
    log_match_key: str | None = None,
    wrapper_installed: bool = False,
    wrapper_format_version: str | None = None,
) -> CronRecord:
    """Build a minimal CronRecord for testing."""
    return CronRecord(
        fingerprint=fingerprint,
        name=name,
        host=host,
        command=command,
        schedule=schedule,
        schedule_canonical=schedule_canonical,
        cadence_seconds=cadence_seconds,
        expected_grace_seconds=expected_grace_seconds,
        enabled=enabled,
        last_seen_state=last_seen_state,
        created_at=created_at,
        updated_at=updated_at,
        hidden_at=hidden_at,
        source_path=source_path,
        wrapper_last_seen_at=wrapper_last_seen_at,
        last_discovered_at=last_discovered_at,
        soft_deleted_at=soft_deleted_at,
        log_match_key=log_match_key,
        wrapper_installed=wrapper_installed,
        wrapper_format_version=wrapper_format_version,
    )


# ---------------------------------------------------------------------------
# _resolve_container_path
# ---------------------------------------------------------------------------


def test_resolve_etc_crontab() -> None:
    host_root = Path("/host")
    path = _resolve_container_path("/etc/crontab", host_root)
    assert path == Path("/host/etc/crontab")


def test_resolve_etc_cron_d() -> None:
    host_root = Path("/host")
    path = _resolve_container_path("/etc/cron.d/myjob", host_root)
    assert path == Path("/host/etc/cron.d/myjob")


def test_resolve_user_crontab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", "/snap")
    host_root = Path("/host")
    path = _resolve_container_path("crontab:alice", host_root)
    assert path == Path("/snap/alice")


def test_resolve_unrecognized_raises() -> None:
    host_root = Path("/host")
    with pytest.raises(WrapperInstallError, match="unrecognized source_path"):
        _resolve_container_path("/proc/cron/something", host_root)


# ---------------------------------------------------------------------------
# _find_matching_line
# ---------------------------------------------------------------------------


def test_find_matching_line_system_crontab() -> None:
    content = f"# comment\n{_SCHEDULE} root {_COMMAND}\n"
    idx, _raw_line, sched, inner, _line_is_wrapped, _raw_command = _find_matching_line(
        content=content,
        host=_HOST,
        source_path=_SOURCE_PATH,
        fingerprint=_FINGERPRINT,
    )
    assert idx == 1
    assert sched == _SCHEDULE
    assert inner == _COMMAND


def test_find_matching_line_user_crontab() -> None:
    source_path = "crontab:alice"
    fp = compute_fingerprint(
        host=_HOST,
        source_path=source_path,
        schedule=_SCHEDULE,
        command=_COMMAND,
    )
    content = f"# comment\n{_SCHEDULE} {_COMMAND}\n"
    idx, _raw_line, _sched, inner, _line_is_wrapped, _raw_command = _find_matching_line(
        content=content,
        host=_HOST,
        source_path=source_path,
        fingerprint=fp,
    )
    assert idx == 1
    assert inner == _COMMAND


def test_find_matching_line_iterates_past_non_matching() -> None:
    """_find_matching_line iterates past lines whose fingerprint doesn't match (145->131 branch)."""
    # Two valid job lines; only the second matches the fingerprint
    # The first line is parseable but fingerprint != _FINGERPRINT
    content = f"{_SCHEDULE} root /usr/bin/other\n{_SCHEDULE} root {_COMMAND}\n"
    idx, _raw_line, _sched, inner, _line_is_wrapped, _raw_command = _find_matching_line(
        content=content,
        host=_HOST,
        source_path=_SOURCE_PATH,
        fingerprint=_FINGERPRINT,
    )
    assert idx == 1
    assert inner == _COMMAND


def test_find_matching_line_not_found_raises() -> None:
    with pytest.raises(CronLineNotFoundError):
        _find_matching_line(
            content="# only comments\n",
            host=_HOST,
            source_path=_SOURCE_PATH,
            fingerprint=_FINGERPRINT,
        )


def test_find_matching_line_finds_wrapped_via_unwrap() -> None:
    """_find_matching_line finds a wrapped line by matching the inner fingerprint."""
    wrapped_cmd = build_invocation_prefix(_FINGERPRINT) + _COMMAND
    content = f"# comment\n{_SCHEDULE} root {wrapped_cmd}\n"
    _idx, raw_line, _sched, inner, _line_is_wrapped, _raw_command = _find_matching_line(
        content=content,
        host=_HOST,
        source_path=_SOURCE_PATH,
        fingerprint=_FINGERPRINT,
    )
    assert inner == _COMMAND
    assert is_wrapped(raw_line.split(None, 2)[-1]) or wrapped_cmd in raw_line


# ---------------------------------------------------------------------------
# _rewrite_line
# ---------------------------------------------------------------------------


def test_rewrite_line_system_crontab() -> None:
    """_rewrite_line inserts the fingerprint-prefixed invocation before the command."""
    raw = f"{_SCHEDULE} root {_COMMAND}"
    new = _rewrite_line(raw, _COMMAND, _FINGERPRINT)
    assert new.endswith(build_invocation_prefix(_FINGERPRINT) + _COMMAND)
    # Prefix of line (schedule + user) preserved
    assert new.startswith(f"{_SCHEDULE} root ")


def test_rewrite_line_user_crontab() -> None:
    raw = f"{_SCHEDULE} {_COMMAND}"
    new = _rewrite_line(raw, _COMMAND, _FINGERPRINT)
    assert new.endswith(build_invocation_prefix(_FINGERPRINT) + _COMMAND)
    assert new.startswith(f"{_SCHEDULE} ")


def test_rewrite_line_command_not_in_raw_raises() -> None:
    """If inner_command is not in raw_line, WrapperInstallError is raised."""
    with pytest.raises(WrapperInstallError, match="internal"):
        _rewrite_line(
            "* * * * * root /other/command",
            _COMMAND,  # _COMMAND is not in this raw line
            _FINGERPRINT,
        )


# ---------------------------------------------------------------------------
# build_install_kit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_install_kit_produces_correct_diff(tmp_path: Path) -> None:
    """build_install_kit returns kit with correct crontab diff for /etc/crontab."""
    # Write synthetic crontab
    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    crontab_file = etc_dir / "crontab"
    crontab_file.write_text(f"# header\n{_SCHEDULE} root {_COMMAND}\n")

    cron = _make_cron_record()
    kit = await build_install_kit(
        cron,
        host_root=tmp_path,
        public_url="https://monitor.example.com",
    )

    assert kit.fingerprint == _FINGERPRINT
    assert kit.wrapper_path == WRAPPER_PATH
    assert kit.token_file_path == TOKEN_FILE_PATH
    # STAGE-002-012: generic wrapper — fingerprint and public URL NOT baked in
    assert _FINGERPRINT not in kit.wrapper_content
    assert "https://monitor.example.com" not in kit.wrapper_content
    # The format version IS baked in (constant placeholder substitution)
    assert "1.0.0" in kit.wrapper_content

    diff = kit.crontab_diff
    assert diff.source_path == _SOURCE_PATH
    assert diff.old_line.strip().endswith(_COMMAND)
    assert build_invocation_prefix(_FINGERPRINT) in diff.new_line
    assert _COMMAND in diff.new_line
    assert diff.line_index == 1


@pytest.mark.asyncio
async def test_build_install_kit_user_crontab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_install_kit works for user crontab (crontab:alice)."""
    source_path = "crontab:alice"
    fp = compute_fingerprint(
        host=_HOST,
        source_path=source_path,
        schedule=_SCHEDULE,
        command=_COMMAND,
    )

    snapshot_dir = tmp_path / "crontab-snapshot"
    snapshot_dir.mkdir(parents=True)
    crontab_file = snapshot_dir / "alice"
    crontab_file.write_text(f"{_SCHEDULE} {_COMMAND}\n")
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(snapshot_dir))

    cron = _make_cron_record(source_path=source_path, fingerprint=fp)

    kit = await build_install_kit(
        cron,
        host_root=tmp_path,
        public_url="https://monitor.example.com",
    )
    assert kit.fingerprint == fp
    assert build_invocation_prefix(fp) in kit.crontab_diff.new_line


@pytest.mark.asyncio
async def test_build_install_kit_file_not_found_raises(tmp_path: Path) -> None:
    """build_install_kit raises CronLineNotFoundError if crontab file missing."""
    cron = _make_cron_record()
    with pytest.raises(CronLineNotFoundError, match="not found"):
        await build_install_kit(
            cron,
            host_root=tmp_path,
            public_url="https://monitor.example.com",
        )


@pytest.mark.asyncio
async def test_build_install_kit_line_not_found_raises(tmp_path: Path) -> None:
    """build_install_kit raises CronLineNotFoundError if no line matches fingerprint."""
    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    crontab_file = etc_dir / "crontab"
    crontab_file.write_text("# only comments\n")

    cron = _make_cron_record()
    with pytest.raises(CronLineNotFoundError):
        await build_install_kit(
            cron,
            host_root=tmp_path,
            public_url="https://monitor.example.com",
        )


@pytest.mark.asyncio
async def test_build_install_kit_already_wrapped_raises(tmp_path: Path) -> None:
    """build_install_kit raises AlreadyWrappedError if line is already wrapped."""
    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    wrapped_cmd = build_invocation_prefix(_FINGERPRINT) + _COMMAND
    crontab_file = etc_dir / "crontab"
    crontab_file.write_text(f"{_SCHEDULE} root {wrapped_cmd}\n")

    cron = _make_cron_record()
    with pytest.raises(AlreadyWrappedError):
        await build_install_kit(
            cron,
            host_root=tmp_path,
            public_url="https://monitor.example.com",
        )


# ---------------------------------------------------------------------------
# Discoverer round-trip: wrapped crontab → same fingerprint as unwrapped
# ---------------------------------------------------------------------------


def test_parse_one_line_unwraps_wrapped_command() -> None:
    """parse_one_line returns the INNER (unwrapped) command for a wrapped line."""
    from homelab_monitor.plugins.discoverers.cron_parser import parse_one_line  # noqa: PLC0415

    wrapped_cmd = build_invocation_prefix(_FINGERPRINT) + _COMMAND
    # System crontab line: schedule + user + wrapped_cmd
    raw_line = f"{_SCHEDULE} root {wrapped_cmd}"
    result = parse_one_line(line=raw_line, source_kind=CronSourceKind.SYSTEM_WITH_USER_FIELD)

    assert result is not None
    sched, cmd = result
    assert sched == _SCHEDULE
    assert cmd == _COMMAND  # unwrapped


def test_fingerprint_round_trip_wrapped_equals_unwrapped() -> None:
    """THE critical convergence test.

    Wrap a command, run the resulting line through parse_one_line,
    recompute fingerprint → must equal fingerprint of original unwrapped command.
    """
    from homelab_monitor.plugins.discoverers.cron_parser import parse_one_line  # noqa: PLC0415

    inner_command = "/usr/bin/backup.sh --full"
    fp_original = compute_fingerprint(
        host=_HOST, source_path=_SOURCE_PATH, schedule=_SCHEDULE, command=inner_command
    )

    # Simulate the wrapped crontab line
    wrapped_cmd = build_invocation_prefix(_FINGERPRINT) + inner_command
    raw_line = f"{_SCHEDULE} root {wrapped_cmd}"

    # Parse via discoverer (same path as cron discovery)
    result = parse_one_line(line=raw_line, source_kind=CronSourceKind.SYSTEM_WITH_USER_FIELD)
    assert result is not None
    parsed_schedule, parsed_command = result

    fp_after_wrap = compute_fingerprint(
        host=_HOST, source_path=_SOURCE_PATH, schedule=parsed_schedule, command=parsed_command
    )

    assert fp_after_wrap == fp_original, (
        f"Fingerprint mismatch after wrap: {fp_after_wrap!r} != {fp_original!r}"
    )


@pytest.mark.asyncio
async def test_build_install_kit_source_path_none_raises(tmp_path: Path) -> None:
    """build_install_kit raises WrapperInstallError when source_path is None (line 247)."""
    cron = _make_cron_record(source_path=None)
    with pytest.raises(WrapperInstallError, match="no source_path"):
        await build_install_kit(
            cron,
            host_root=tmp_path,
            public_url="https://monitor.example.com",
        )


def test_fingerprint_round_trip_user_crontab() -> None:
    """Same round-trip test for USER_CRONTAB source kind."""
    from homelab_monitor.plugins.discoverers.cron_parser import parse_one_line  # noqa: PLC0415

    source_path = "crontab:alice"
    inner_command = "/home/alice/backup.sh"
    fp_original = compute_fingerprint(
        host=_HOST, source_path=source_path, schedule=_SCHEDULE, command=inner_command
    )

    wrapped_cmd = build_invocation_prefix(_FINGERPRINT) + inner_command
    raw_line = f"{_SCHEDULE} {wrapped_cmd}"

    result = parse_one_line(line=raw_line, source_kind=CronSourceKind.USER_CRONTAB)
    assert result is not None
    parsed_schedule, parsed_command = result

    fp_after = compute_fingerprint(
        host=_HOST, source_path=source_path, schedule=parsed_schedule, command=parsed_command
    )
    assert fp_after == fp_original


# ---------------------------------------------------------------------------
# LEGACY-format support (STAGE-002-012 format-migration)
# ---------------------------------------------------------------------------


def test_find_matching_line_legacy_wrapped() -> None:
    """_find_matching_line finds a legacy-wrapped line by matching the inner fingerprint."""
    legacy_cmd = f"{WRAPPER_PATH} {WRAPPER_SEPARATOR} {_COMMAND}"
    content = f"# comment\n{_SCHEDULE} root {legacy_cmd}\n"
    _idx, _raw_line, _sched, inner, _line_is_wrapped, raw_cmd = _find_matching_line(
        content=content,
        host=_HOST,
        source_path=_SOURCE_PATH,
        fingerprint=_FINGERPRINT,
    )
    assert inner == _COMMAND
    assert is_legacy_wrapped(raw_cmd)
    assert raw_cmd == legacy_cmd


def test_rewrite_line_legacy_wrapped() -> None:
    """_rewrite_line replaces LEGACY-format segment with NEW format."""
    legacy_cmd = f"{WRAPPER_PATH} {WRAPPER_SEPARATOR} {_COMMAND}"
    raw = f"{_SCHEDULE} root {legacy_cmd}"
    new = _rewrite_line(raw, _COMMAND, _FINGERPRINT)
    # Should NOT contain the legacy `-- ` prefix
    assert f"{WRAPPER_PATH} {WRAPPER_SEPARATOR}" not in new
    # Should contain the NEW format prefix
    assert build_invocation_prefix(_FINGERPRINT) in new
    # Should end with the NEW-format wrapped command
    assert new.endswith(build_invocation_prefix(_FINGERPRINT) + _COMMAND)
    # Prefix of line (schedule + user) preserved
    assert new.startswith(f"{_SCHEDULE} root ")
    # No double-wrapper
    assert new.count(WRAPPER_PATH) == 1


@pytest.mark.asyncio
async def test_build_install_kit_legacy_wrapped_allowed(tmp_path: Path) -> None:
    """build_install_kit allows re-wrapping a LEGACY-format wrapped line (format-migration)."""
    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    legacy_cmd = f"{WRAPPER_PATH} {WRAPPER_SEPARATOR} {_COMMAND}"
    crontab_file = etc_dir / "crontab"
    crontab_file.write_text(f"{_SCHEDULE} root {legacy_cmd}\n")

    cron = _make_cron_record()
    # Should NOT raise AlreadyWrappedError (legacy re-wrap is allowed)
    kit = await build_install_kit(
        cron,
        host_root=tmp_path,
        public_url="https://monitor.example.com",
    )

    assert kit.fingerprint == _FINGERPRINT
    diff = kit.crontab_diff
    # old_line is the legacy-wrapped version
    assert legacy_cmd in diff.old_line
    # new_line is the NEW-format wrapped version
    assert build_invocation_prefix(_FINGERPRINT) in diff.new_line
    assert _COMMAND in diff.new_line
    # No double wrapper
    assert diff.new_line.count(WRAPPER_PATH) == 1


@pytest.mark.asyncio
async def test_build_install_kit_new_format_wrapped_still_raises(tmp_path: Path) -> None:
    """build_install_kit still raises AlreadyWrappedError for NEW-format wrapped (regression)."""
    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    wrapped_cmd = build_invocation_prefix(_FINGERPRINT) + _COMMAND
    crontab_file = etc_dir / "crontab"
    crontab_file.write_text(f"{_SCHEDULE} root {wrapped_cmd}\n")

    cron = _make_cron_record()
    # Should raise AlreadyWrappedError (genuinely already wrapped)
    with pytest.raises(AlreadyWrappedError):
        await build_install_kit(
            cron,
            host_root=tmp_path,
            public_url="https://monitor.example.com",
        )


@pytest.mark.asyncio
async def test_build_install_kit_unwrapped_still_works(tmp_path: Path) -> None:
    """build_install_kit works for unwrapped crons (regression)."""
    etc_dir = tmp_path / "etc"
    etc_dir.mkdir()
    crontab_file = etc_dir / "crontab"
    crontab_file.write_text(f"{_SCHEDULE} root {_COMMAND}\n")

    cron = _make_cron_record()
    kit = await build_install_kit(
        cron,
        host_root=tmp_path,
        public_url="https://monitor.example.com",
    )

    assert kit.fingerprint == _FINGERPRINT
    diff = kit.crontab_diff
    assert diff.old_line.strip().endswith(_COMMAND)
    assert build_invocation_prefix(_FINGERPRINT) in diff.new_line
